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

    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TypeError:
            raise
        except Exception as exc:
            log.add(step, call_kind=call_kind, **failure_info(exc))
            raise

    return wrapped


def wrap_voter(voter: Callable | None, log: IncidentLog) -> Callable | None:
    """The per-camera review voter answers ``unavail`` exactly when one of its two
    questions failed (``vlm_client.make_endstate_voter``); note that as an incident."""
    if voter is None:
        return None

    def wrapped(start_frames, end_frames, cam_label, instruction):
        vote = voter(start_frames, end_frames, cam_label, instruction)
        if vote == "unavail":
            log.add("endstate", call_kind="endstate", camera=str(cam_label).split(";")[0],
                    cause="call_failed")
        return vote

    return wrapped


def wrap_arbitration(arb_deps: dict | None, log: IncidentLog) -> dict | None:
    """The arbitration dependencies with every model call wrapped."""
    if arb_deps is None:
        return None
    out = dict(arb_deps)
    out["question_writer"] = wrap_call(arb_deps["question_writer"], log, step="arbitration",
                                       call_kind="arbitration")
    out["grounder"] = wrap_call(arb_deps["grounder"], log, step="arbitration",
                                call_kind="arbitration")
    out["judge"] = wrap_call(arb_deps["judge"], log, step="arbitration",
                             call_kind="arbitration")
    out["same_task"] = wrap_call(arb_deps["same_task"], log, step="label_guard",
                                 call_kind="llm")
    out["captioner"] = wrap_call(arb_deps["captioner"], log, step="guard_caption",
                                 call_kind="caption")
    return out


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


# ---------------------------------------------------------------- decode inside A-class code

_TLS = threading.local()


def current_decode_log() -> tuple[IncidentLog, dict] | None:
    return getattr(_TLS, "log", None)


class decode_watch:
    """Context manager: decode failures on *this thread* go to ``log``.

    For A-class code that imports the decoder itself (``caption_episodes``): the
    shell patches ``adapters.decode.decode_window`` once (:func:`install_decode_watch`)
    and calls such code for one episode at a time on one thread.
    """

    def __init__(self, log: IncidentLog, cameras: dict[str, str]):
        self.log, self.cameras = log, cameras

    def __enter__(self):
        self._prev = getattr(_TLS, "log", None)
        _TLS.log = (self.log, self.cameras)
        return self

    def __exit__(self, *exc):
        _TLS.log = self._prev
        return False


class _DecodeWatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users = 0
        self._orig: Callable | None = None

    def install(self) -> None:
        from ..adapters import decode as decode_mod

        with self._lock:
            self._users += 1
            if self._users > 1:
                return
            self._orig = decode_mod.decode_window
            orig = self._orig

            def watched(path, from_ts, to_ts, **kwargs):
                try:
                    return orig(path, from_ts, to_ts, **kwargs)
                except Exception as exc:
                    cur = current_decode_log()
                    if cur is not None:
                        log, cameras = cur
                        log.add("decode", camera=cameras.get(str(path),
                                                             str(path).rsplit("/", 1)[-1]),
                                cause=f"{type(exc).__name__}: {exc}")
                    raise

            decode_mod.decode_window = watched

    def uninstall(self) -> None:
        from ..adapters import decode as decode_mod

        with self._lock:
            self._users -= 1
            if self._users > 0 or self._orig is None:
                return
            decode_mod.decode_window = self._orig
            self._orig = None


_WATCH = _DecodeWatch()


class installed_decode_watch:
    """``with installed_decode_watch(): ...`` - the decoder patch for a stage's duration."""

    def __enter__(self):
        _WATCH.install()
        return self

    def __exit__(self, *exc):
        _WATCH.uninstall()
        return False
