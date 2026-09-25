"""L3: decode every frame of an episode's cameras (design doc 14 §3.4, module parameter
``decode_test``, off by default).

Decoding fails loudly (an exception) on gross damage; on a few garbled bytes FFmpeg's
decoders conceal the error and go on - silently, since PyAV keeps FFmpeg's log off. So
the log is switched on at ERROR for this process and captured per thread around each
decode: an exception is ``decode_failed`` (reject), error lines without one are
``decode_concealed`` (suspect). Frames are only decoded, never converted.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

_setup = threading.Lock()
_ready = False


def _quiet_ffmpeg_log() -> None:
    """ERROR and above, captured where decoding happens and dropped anywhere else (stderr
    carries the command's C3 events only)."""
    global _ready
    with _setup:
        if _ready:
            return
        import av.logging

        log = logging.getLogger("libav")
        log.addHandler(logging.NullHandler())
        log.propagate = False
        av.logging.set_level(av.logging.ERROR)
        _ready = True


@dataclass
class Decoded:
    frames: int = 0
    error: str | None = None                     # an exception: the decode stopped
    concealed: list[str] = field(default_factory=list)   # decoder error lines


def decode(path: str, start_s: float, end_s: float) -> Decoded:
    """Decode the frames of ``[start_s, end_s)`` of the file's first video stream.

    Invalid data is the file's (``Decoded.error``); anything else - a connection that
    dropped, a signature that expired - is the storage's, retried like the frame stage's
    decode (``REMOTE_ATTEMPTS``, re-signing each time) and then raised as ReadFailure."""
    from ...adapters.decode import REMOTE_ATTEMPTS
    from ...ingest import dsfs
    from .files import ReadFailure

    attempts = REMOTE_ATTEMPTS if dsfs.is_remote(path) else 1
    for attempt in range(1, attempts + 1):
        try:
            return _decode_once(path, start_s, end_s)
        except _Infra as e:
            if attempt == attempts:
                raise ReadFailure(str(e)) from e
    raise AssertionError("unreachable")


class _Infra(Exception):
    pass


def _data_error(e: Exception) -> bool:
    import av

    return isinstance(e, (av.error.InvalidDataError, av.error.PatchWelcomeError,
                          av.error.BugError, EOFError))


def _decode_once(path: str, start_s: float, end_s: float) -> Decoded:
    import av
    import av.logging

    from ...adapters.decode import REMOTE_OPEN_OPTIONS
    from ...ingest import dsfs

    _quiet_ffmpeg_log()
    out = Decoded()
    remote = dsfs.is_remote(path)
    src = dsfs.media_source(path) if remote else path
    with av.logging.Capture(True) as logs:
        try:
            with av.open(src, options=REMOTE_OPEN_OPTIONS if remote else None) as container:
                stream = container.streams.video[0]
                if start_s > 0:
                    container.seek(int(start_s / stream.time_base), stream=stream,
                                   any_frame=False)
                for frame in container.decode(stream):
                    if frame.pts is None:
                        out.frames += 1
                        continue
                    t = float(frame.pts * stream.time_base)
                    if t < start_s - 1e-6:
                        continue
                    if t >= end_s - 1e-6:
                        break
                    out.frames += 1
        except Exception as e:  # noqa: BLE001 - FFmpeg's errors come in many classes
            if not _data_error(e):
                raise _Infra(f"{type(e).__name__}: {e}"[:300]) from e
            out.error = f"{type(e).__name__}: {e}"[:300]
    out.concealed = [msg.strip() for level, _name, msg in logs
                     if level is not None and level <= av.logging.ERROR][:20]
    return out
