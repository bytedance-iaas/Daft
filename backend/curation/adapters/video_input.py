"""Continuous episode clips for video-capable Chat APIs.

Always cut [from_ts, to_ts): LeRobot v3 files can contain several episodes.
With ``fps`` set, frames are decimated onto a uniform grid anchored at the window
start (the server samples the sent video at the same rate, so the frames the
model reads are unchanged); kept frames keep their original timestamps. Only the
model server's video fps is configurable, and both sides share that one setting.
"""
from __future__ import annotations

import base64
import hashlib
import math
import tempfile
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class VideoClip:
    camera: str
    url: str
    sha256: str
    start_s: float
    end_s: float
    frames: int
    byte_size: int

    def metadata(self) -> dict:
        return {k: getattr(self, k) for k in
                ("camera", "sha256", "start_s", "end_s", "frames", "byte_size")}


def _encode(camera: str, source: str, start: float, end: float, *,
            max_side: int, max_bytes: int, fps: float | None = None, options=None) -> VideoClip:
    import av

    time_base = Fraction(1, 90000)
    # Decimation grid anchored at the first frame of the window; the server samples
    # the encoded clip from its t=0 at the same fps, so kept frames are what it reads.
    step = None if fps is None else 1.0 / float(fps)
    next_grid = 0
    first = None
    last_end = None
    count = 0
    # Disk-backed: do not retain every decoded frame in memory.
    with tempfile.TemporaryFile(suffix=".mp4") as output:
        with av.open(source, options=options) as inp, av.open(output, "w", format="mp4") as out:
            if not inp.streams.video:
                raise ValueError(f"{camera}: no video stream")
            stream = inp.streams.video[0]
            inp.seek(int(start / stream.time_base), stream=stream, any_frame=False)
            encoder = None
            for frame in inp.decode(stream):
                if frame.pts is None:
                    raise ValueError(f"{camera}: video frame has no timestamp")
                t = float(frame.pts * stream.time_base)
                if t < start - 1e-6:
                    continue
                if t >= end - 1e-6:
                    break
                if first is None:
                    first = t
                    scale = min(1.0, max_side / max(frame.width, frame.height))
                    width = max(2, int(frame.width * scale) // 2 * 2)
                    height = max(2, int(frame.height * scale) // 2 * 2)
                    encoder = out.add_stream(
                        "libx264",
                        rate=Fraction(str(fps)) if fps is not None else (stream.average_rate or 30))
                    encoder.width, encoder.height = width, height
                    encoder.pix_fmt = "yuv420p"
                    encoder.time_base = time_base
                    encoder.codec_context.time_base = time_base
                    encoder.options = {"crf": "20", "preset": "fast", "threads": "1"}
                if step is not None:
                    grid = int((t - first) / step + 1e-6)
                    if grid < next_grid:
                        # Completeness still tracks every decoded frame, not just kept ones.
                        duration = float(getattr(frame, "duration", 0) or 0) * float(frame.time_base)
                        if duration <= 0:
                            duration = 1 / float(stream.average_rate or 30)
                        last_end = t + duration
                        continue
                    next_grid = grid + 1
                duration = float(getattr(frame, "duration", 0) or 0) * float(frame.time_base)
                if duration <= 0:
                    duration = 1 / float(stream.average_rate or 30)
                last_end = t + duration
                encoded_frame = frame.reformat(width=encoder.width, height=encoder.height,
                                               format="yuv420p")
                encoded_frame.pts = round((t - first) / float(time_base))
                encoded_frame.time_base = time_base
                for packet in encoder.encode(encoded_frame):
                    out.mux(packet)
                count += 1
                if output.tell() > max_bytes:
                    raise ValueError(f"{camera}: episode clip exceeds video byte limit ({max_bytes})")
            if encoder is None:
                raise ValueError(f"{camera}: episode window contains no video frames")
            if last_end < end - max(1e-4, duration * 0.1):
                raise ValueError(f"{camera}: video ends before the requested episode window")
            for packet in encoder.encode():
                out.mux(packet)
        output.seek(0)
        raw = output.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{camera}: episode clip exceeds video byte limit ({max_bytes})")
    return VideoClip(camera, "data:video/mp4;base64," + base64.b64encode(raw).decode("ascii"),
                     hashlib.sha256(raw).hexdigest(), max(0.0, first - start), end - start,
                     count, len(raw))


def prepare_videos(video: dict, *, max_side: int = 720,
                   max_bytes: int = 32 * 1024 * 1024, max_cams: int = 4,
                   fps: float | None = None) -> list[VideoClip]:
    """Prepare labelled continuous clips, failing explicitly on missing camera media.

    HTTP/TOS inputs use the same fresh-signature retry policy as frame decoding.
    The byte limit applies to the whole request, not independently to each camera.
    ``fps`` decimates the local encode to the same rate the request asks the server
    to sample at (video_content's range, 0.2..5) — same frames read, fewer bytes.
    """
    from .decode import REMOTE_ATTEMPTS, REMOTE_OPEN_OPTIONS
    from ..ingest import dsfs

    if not video:
        raise ValueError("video input has no cameras")
    if max_side < 2 or max_bytes <= 0 or max_cams < 1:
        raise ValueError("invalid video preprocessing limits")
    if fps is not None and (isinstance(fps, bool) or not math.isfinite(fps) or not 0.2 <= fps <= 5):
        raise ValueError("video fps must be between 0.2 and 5")
    clips = []
    remaining = max_bytes
    for camera in sorted(video)[:max_cams]:
        item = video[camera]
        start, end = float(item["from_ts"]), float(item["to_ts"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"{camera}: invalid episode video window")
        path = str(item["path"])
        remote = dsfs.is_remote(path) or path.startswith(("https://", "http://"))
        for attempt in range(REMOTE_ATTEMPTS if remote else 1):
            try:
                clip = _encode(str(camera), dsfs.media_source(path), start, end,
                               max_side=max_side, max_bytes=remaining, fps=fps,
                               options=REMOTE_OPEN_OPTIONS if remote else None)
                break
            except ValueError:
                raise
            except Exception:
                if not remote or attempt == REMOTE_ATTEMPTS - 1:
                    raise
        remaining -= clip.byte_size
        clips.append(clip)
    return clips


def video_content(clips: list[VideoClip], *, fps: float = 5.0) -> list[dict]:
    """Chat Completions video_url protocol (Ark fps range: 0.2..5)."""
    if not clips:
        raise ValueError("video request must contain at least one clip")
    if not math.isfinite(fps) or not 0.2 <= fps <= 5:
        raise ValueError("video fps must be between 0.2 and 5")
    content = []
    for clip in clips:
        content.extend([
            {"type": "text", "text": (
                f"Camera: {clip.camera}. Same episode, chronological video. "
                f"Video time 0 corresponds to episode time {clip.start_s:.6f}s; "
                f"episode window ends at {clip.end_s:.6f}s. "
                "Report evidence times in episode-relative seconds.")},
            {"type": "video_url", "video_url": {"url": clip.url, "fps": fps}},
        ])
    return content


def encode_rendered_video(camera: str, frames, *, fps: float, end_s: float,
                           max_bytes: int = 32 * 1024 * 1024) -> VideoClip:
    """Encode an iterator of (episode PTS, RGB image), e.g. a continuous EEF overlay.

    No temporal sampling: every supplied source frame is encoded at its timestamp.
    """
    import av

    tb = Fraction(1, 90000)
    first, last, count = None, None, 0
    with tempfile.TemporaryFile(suffix=".mp4") as output:
        with av.open(output, "w", format="mp4") as out:
            stream = None
            for t, rgb in frames:
                if not math.isfinite(t) or (last is not None and t <= last):
                    raise ValueError("rendered video timestamps must increase")
                if first is None:
                    first = t
                    stream = out.add_stream("libx264", rate=Fraction(str(fps)))
                    stream.width, stream.height = rgb.shape[1] // 2 * 2, rgb.shape[0] // 2 * 2
                    stream.pix_fmt = "yuv420p"
                    stream.time_base = stream.codec_context.time_base = tb
                    stream.options = {"crf": "20", "preset": "fast", "threads": "1"}
                frame = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(
                    width=stream.width, height=stream.height, format="yuv420p")
                frame.pts, frame.time_base = round((t - first) / float(tb)), tb
                for packet in stream.encode(frame):
                    out.mux(packet)
                count += 1
                last = t
                if output.tell() > max_bytes:
                    raise ValueError("rendered video exceeds video byte limit")
            if stream is None:
                raise ValueError("rendered video has no frames")
            for packet in stream.encode():
                out.mux(packet)
        output.seek(0)
        raw = output.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("rendered video exceeds video byte limit")
    return VideoClip(camera, "data:video/mp4;base64," + base64.b64encode(raw).decode("ascii"),
                     hashlib.sha256(raw).hexdigest(), first, end_s, count, len(raw))
