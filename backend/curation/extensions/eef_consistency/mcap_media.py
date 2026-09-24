"""The frames of an mcap image topic as a local video (design 12, F5.13).

An mcap dataset has no video files: a camera is a topic of compressed image messages inside the
episode's ``.mcap`` file (``media.uri`` names the file, ``media.topic`` the topic). Its frames are
the topic's messages in log_time order, numbered from 0 - what ``video_frame_index`` counts. They are
written once into a local mp4 without re-encoding (JPEG into an mjpeg mp4, H.264 Annex-B re-muxed:
v1's ``ingest/mcap_reader`` helpers, the same as the funnel's modules read) on a uniform timeline of
``RATE`` frames per second, so decoding numbers them by position; the message times are not needed
here - the trajectory's own timeline and ``video_frame_index`` carry the timing.

The videos of a call live in one temporary directory; :func:`drop` removes those of a file once its
episode is judged, :func:`close` everything.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading

#: the synthetic frame rate of a written video (any constant: frame i is at i / RATE)
RATE = 30.0


class TopicError(RuntimeError):
    """The topic cannot be read as a video (missing, empty, an encoding v1's muxers do not take)."""


_lock = threading.Lock()
_key_locks: dict[tuple, threading.Lock] = {}
_videos: dict[tuple, str] = {}
_dir: str | None = None


def _root() -> str:
    global _dir
    if _dir is None or not os.path.isdir(_dir):
        _dir = tempfile.mkdtemp(prefix="eef-mcap-")
    return _dir


def _identity(path: str) -> tuple:
    st = os.stat(path)
    return os.path.abspath(path), int(st.st_size), int(st.st_mtime_ns)


def read_topic(path: str, topic: str) -> list[tuple[str, bytes]]:
    """``[(format, bytes)]`` of the topic's messages in log_time order."""
    from ...ingest import mcap_reader as MR

    make_reader = MR._mcap_reader_mod()
    factories: dict = {}
    decoders: dict = {}
    out: list[tuple[str, bytes]] = []
    seen = False
    with open(path, "rb") as fh:
        reader = make_reader(fh)
        for schema, channel, message in reader.iter_messages(topics=[topic], log_time_order=True):
            seen = True
            frame = MR._as_frame(MR._decode(channel, schema, message, factories, decoders))
            if frame is not None:
                out.append(frame)
    if not out:
        raise TopicError(f"{os.path.basename(path)}: topic {topic} " +
                         ("has no image frames" if seen else "is not in the file"))
    return out


def write_video(path: str, topic: str, out_path: str) -> int:
    """The topic's frames into ``out_path`` (no re-encoding); returns how many messages it had."""
    from ...ingest import mcap_reader as MR

    frames = read_topic(path, topic)
    codecs = {MR._codec_of(fmt, b) for fmt, b in frames}
    times = [i / RATE for i in range(len(frames))]
    blobs = [b for _, b in frames]
    try:
        if codecs == {"jpeg"}:
            MR.mux_jpeg_frames(blobs, times, out_path, RATE)
        elif codecs == {"h264"}:
            MR._mux_annexb(blobs, times, out_path, RATE)
        else:
            raise TopicError(f"{os.path.basename(path)}: topic {topic} is {sorted(codecs)}; "
                             "only JPEG and H.264 Annex-B frames are read")
    except MR.NotADatasetError as exc:
        raise TopicError(str(exc)) from exc
    return len(frames)


def video(path: str, topic: str) -> str:
    """The local video of ``topic`` in the mcap file ``path``, written on first use."""
    key = (*_identity(path), topic)
    with _lock:
        hit = _videos.get(key)
        if hit is not None and os.path.isfile(hit):
            return hit
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        with _lock:
            hit = _videos.get(key)
        if hit is not None and os.path.isfile(hit):
            return hit
        name = hashlib.sha256(repr(key).encode()).hexdigest()[:24] + ".mp4"
        out = os.path.join(_root(), name)
        write_video(path, topic, out)
        with _lock:
            _videos[key] = out
        return out


def drop(path: str) -> None:
    """Forget and delete the videos made from the mcap file ``path`` (its episode is done)."""
    where = os.path.abspath(path)
    with _lock:
        gone = [k for k in _videos if k[0] == where]
        files = [_videos.pop(k) for k in gone]
        for k in gone:
            _key_locks.pop(k, None)
    for f in files:
        try:
            os.unlink(f)
        except OSError:
            pass


def close() -> None:
    """Delete every video made in this process."""
    global _dir
    with _lock:
        _videos.clear()
        _key_locks.clear()
        d, _dir = _dir, None
    if d is not None:
        shutil.rmtree(d, ignore_errors=True)
