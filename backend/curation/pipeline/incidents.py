"""Execution incidents of one episode (D33; design doc 02 §3.5).

"Error" is taken broadly: while one module judges one episode, any model call
that still fails after every retry, and any camera that fails to decode, makes
the record ``verdict = error`` - even when v1's fallback logic still reaches a
conclusion (a review that rescues a failed score, arbitration that stays
abstained, judging with one camera fewer). The A-class code that absorbs those
failures does not change; instead the shell hands it wrapped callables that
note the failure here and re-raise it untouched, so the algorithm sees exactly
what it saw before.

Each wrapper is a closure over one episode's :class:`IncidentLog`, so it works
from whatever thread the algorithm happens to call it on (core runs camera
votes and arbitration votes on its own thread pools).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class IncidentLog:
    """The incidents of one episode (``error.incidents`` of its result record)."""

    def __init__(self) -> None:
        self._items: list[dict] = []
        self._lock = threading.Lock()

    def add(self, step: str, *, cause: str | None = None, camera: str | None = None,
            call_kind: str | None = None, attempts: int | None = None) -> None:
        item: dict[str, Any] = {"step": str(step)}
        if call_kind:
            item["call_kind"] = str(call_kind)
        if camera:
            item["camera"] = str(camera)
        if cause:
            item["cause"] = str(cause)[:300]
        if attempts is not None:
            item["attempts"] = int(attempts)
        with self._lock:
            self._items.append(item)

    def extend(self, items) -> None:
        with self._lock:
            self._items.extend(dict(i) for i in items)

    def items(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._items]

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._items)


#: what a decoder says about a video that is itself corrupt (design doc 28 §2.1, D93): reading it again gives the
#: same answer, so it is a finding of the video, not an execution error. FFmpeg's AVERROR_INVALIDDATA (PyAV's
#: ``InvalidDataError``) and a container without its index or without a video stream.
CORRUPT_VIDEO_MARKERS = ("InvalidDataError", "Invalid data found when processing input", "moov atom not found",
                         "no video stream")


def corrupt_video(incident: dict) -> bool:
    """Whether an incident is a decoder's verdict on a corrupt video (D93) rather than an execution error."""
    cause = str(incident.get("cause") or "")
    return any(m in cause for m in CORRUPT_VIDEO_MARKERS)


def failure_info(exc: BaseException) -> dict:
    """``cause`` / ``attempts`` of a failed call, as the transport policy marked it."""
    info = getattr(exc, "curation_failure", None)
    if info is None:
        resp = getattr(exc, "response", None)
        info = getattr(resp, "curation_failure", None) if resp is not None else None
    if info is not None:
        return {k: info[k] for k in ("cause", "attempts") if info.get(k) is not None}
    from ..planner.retry import classify_failure

    failure = classify_failure(exc)
    cause = failure.cause if failure.cause != "error" else type(exc).__name__
    return {"cause": cause}


def wrap_call(fn: Callable | None, log: IncidentLog, *, step: str,
              call_kind: str) -> Callable | None:
    """A model-call callable that notes a failure before re-raising it.

    ``TypeError`` is passed through unnoted: core probes an injected callable's
    signature that way (``question_writer(intent, task_type=...)``).
    """
    if fn is None:
        return None

    from functools import wraps

    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TypeError:
            raise
        except Exception as exc:
            log.add(step, call_kind=call_kind, **failure_info(exc))
            raise

    return wrapped


def camera_names(video: dict | None) -> dict[str, str]:
    """``{video path: short camera name}`` of an episode's video pointers."""
    out: dict[str, str] = {}
    for cam, v in (video or {}).items():
        if isinstance(v, dict) and v.get("path"):
            out[str(v["path"])] = str(cam).split(".")[-1]
    return out


def wrap_decode(decode: Callable, log: IncidentLog, cameras: dict[str, str]) -> Callable:
    """A frame decoder that notes a camera whose decoding raised (D33) and re-raises."""

    def wrapped(path, from_ts, to_ts, **kwargs):
        try:
            return decode(path, from_ts, to_ts, **kwargs)
        except Exception as exc:
            log.add("decode", camera=cameras.get(str(path), str(path).rsplit("/", 1)[-1]),
                    cause=f"{type(exc).__name__}: {exc}")
            raise

    return wrapped
