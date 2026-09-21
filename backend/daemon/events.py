"""In-process event hub behind ``GET {base}/events/tasks/{id}`` (design doc 03, section 5).

W5 (orchestration) publishes; SSE streams subscribe. The publish methods are
thread safe and never block on slow browsers, so worker threads that read CLI
stderr can call them directly.

* Every event gets ``id: <epoch>-<seq>``. ``epoch`` numbers Daemon starts (the
  id of the ``daemon.start`` audit event), ``seq`` grows by one per event within
  a start, across all tasks.
* The last ``buffer_size`` (200) events per task are kept for ``Last-Event-ID``
  replay. A reconnect with the same epoch whose events are all still buffered
  resumes after the given seq; anything else (another epoch, evicted events, a
  malformed id) starts with an ``event: reset``, after which the client reloads
  ``GET /api/v1/tasks/{id}``.
* Throttling (design doc 03, section 5): ``progress`` at most 2 per second per
  task and ``usage`` likewise - the newest value wins and is delivered once the
  interval is over, a stage's final progress goes out at once; ``log`` at most 20
  per second per task, the rest is dropped (the full log is in ``/logs``) and the
  next delivered line carries ``dropped: N``. ``state``, ``done`` and ``reset``
  are never throttled, and before a ``state`` or ``done`` goes out the task's
  pending progress and usage are sent, so the final totals precede the end.
* ``state`` / ``done`` may carry a version (:mod:`daemon.transitions` passes the
  id of the audit event written with the change, which grows in commit order):
  one older than what was already published for that task (or subtask) is
  dropped, so two threads racing never leave the stream on a stale state.
* ``state`` and ``done`` always carry ``subtask_id`` (null for the task itself)
  and ``reason`` (the new state_reason, or null), C4 1.2.
* A subscriber that falls ``max_queue`` events behind gets nothing more after the
  gap; its stream sends ``reset`` and ends.
* ``progress`` and ``usage`` carry cumulative values, so a replayed or repeated
  event never double counts; the database stays the source of truth.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger("daemon.events")

EVENT_KINDS = ("state", "progress", "log", "usage", "done", "reset")
LOG_LEVELS = ("error", "warn", "info", "debug")
#: C4 ``StageProgress`` and ``UsageTotals`` allow no other keys.
_STAGE_KEYS = ("id", "state", "done", "total", "elapsed_s", "eta_s", "note")
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
               "requests", "requests_unknown_usage")
_FINAL_STAGE_STATES = frozenset({"succeeded", "completed_with_errors", "failed", "skipped"})
_EVENT_ID_RE = re.compile(r"^([0-9]+)-([0-9]+)$")


@dataclass(frozen=True)
class SseEvent:
    seq: int
    event: str
    data: dict


def format_event(event: str, data: dict, event_id: str | None = None) -> bytes:
    """SSE wire format; JSON never contains raw newlines, so one ``data:`` line suffices."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def parse_event_id(raw: str | None) -> tuple[int, int] | None:
    m = _EVENT_ID_RE.match((raw or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def state_data(state: str, *, at: int, pause_reason: str | None = None,
               subtask_id: str | None = None, reason: str | None = None) -> dict:
    """C4 ``SseState``: ``subtask_id`` is null for the task itself, ``reason`` its state_reason."""
    return {"state": state, "pause_reason": pause_reason, "subtask_id": subtask_id or None,
            "reason": reason or None, "at": int(at)}


def done_data(state: str, *, failed_modules: Iterable[str] = (), subtask_id: str | None = None,
              reason: str | None = None) -> dict:
    """C4 ``SseDone``."""
    return {"state": state, "failed_modules": sorted(set(failed_modules)),
            "subtask_id": subtask_id or None, "reason": reason or None}


class Subscription:
    """One SSE connection's view of a task: what to replay, then what arrives live."""

    def __init__(self, task_id: str, loop: asyncio.AbstractEventLoop, max_queue: int):
        self.task_id = task_id
        self.replay: list[SseEvent] = []
        self.reset = False
        self.fresh = False
        self.head = 0
        self.overflowed = False
        self._loop = loop
        self._max_queue = max_queue
        self._items: collections.deque[SseEvent] = collections.deque()
        self._wakeup = asyncio.Event()
        self.closed = False
        self.ended = False               # the Daemon is shutting down: finish the stream

    # called by the hub, under its lock, from any thread
    def _deliver(self, ev: SseEvent) -> bool:
        if self.closed:
            return False
        if self.overflowed:
            return True                  # past a gap nothing more is queued; the stream resets
        if len(self._items) >= self._max_queue:
            self.overflowed = True
        else:
            self._items.append(ev)
        try:
            self._loop.call_soon_threadsafe(self._wakeup.set)
        except RuntimeError:            # the event loop is gone: this subscriber is dead
            self.closed = True
            return False
        return True

    def drain(self) -> list[SseEvent]:
        out = []
        while self._items:
            out.append(self._items.popleft())
        return out

    def has_items(self) -> bool:
        return bool(self._items)

    def _end(self) -> None:
        self.ended = True
        try:
            self._loop.call_soon_threadsafe(self._wakeup.set)
        except RuntimeError:
            pass

    async def wait(self, timeout: float) -> bool:
        """True when something arrived (or the queue overflowed, or the stream must end)."""
        if self._items or self.overflowed or self.ended:
            return True
        self._wakeup.clear()
        if self._items or self.overflowed or self.ended:
            return True
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout)
        except asyncio.TimeoutError:
            return bool(self._items or self.overflowed or self.ended)
        return True


@dataclass
class _TaskBuffer:
    events: collections.deque
    evicted_upto: int
    subscribers: set = field(default_factory=set)
    touched: float = 0.0


@dataclass
class _Throttle:
    """Per-task publishing state: throttles and the newest state version seen."""

    last_progress: float = float("-inf")
    last_usage: float = float("-inf")
    pending_progress: dict = field(default_factory=dict)     # stage id -> data
    pending_usage: dict | None = None
    log_tokens: float = 0.0
    log_refilled: float = float("-inf")
    log_dropped: int = 0
    versions: dict = field(default_factory=dict)             # (event, subtask id) -> version


class EventHub:
    """Per-task event buffers, subscribers and throttles (see the module docstring)."""

    def __init__(self, epoch: int, *, buffer_size: int = 200, max_tasks: int = 512,
                 progress_interval_s: float = 0.5, usage_interval_s: float = 0.5,
                 log_rate_per_s: float = 20.0, max_queue: int = 2000,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], int] | None = None):
        from .util import now_ms

        self.epoch = int(epoch)
        self.buffer_size = int(buffer_size)
        self.max_tasks = int(max_tasks)
        self.progress_interval_s = progress_interval_s
        self.usage_interval_s = usage_interval_s
        self.log_rate_per_s = log_rate_per_s
        self.max_queue = int(max_queue)
        self._clock = clock
        self._wall = wall_clock or now_ms
        self._lock = threading.RLock()
        self._seq = 0
        self._buffers: dict[str, _TaskBuffer] = {}
        self._throttles: dict[str, _Throttle] = {}
        self._discard_watermark = 0
        self._cond = threading.Condition(self._lock)
        self._flusher: threading.Thread | None = None
        self._stopping = False

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        """Start the thread that delivers throttled events once their interval is over."""
        with self._lock:
            if self._flusher is not None:
                return
            self._stopping = False
            self._flusher = threading.Thread(target=self._flush_loop, name="sse-flusher",
                                             daemon=True)
            self._flusher.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            flusher, self._flusher = self._flusher, None
        if flusher is not None:
            flusher.join(timeout=5)

    def event_id(self, seq: int) -> str:
        return f"{self.epoch}-{seq}"

    @property
    def head(self) -> int:
        with self._lock:
            return self._seq

    # -- publishing (any thread) ------------------------------------------------
    def publish_state(self, task_id: str, state: str, *, pause_reason: str | None = None,
                      at: int | None = None, subtask_id: str | None = None,
                      reason: str | None = None, version: int | None = None) -> str | None:
        """``version`` orders concurrent publishers: an older one than already sent is dropped.

        :mod:`daemon.transitions` passes the id of the audit event written with the
        state change, which grows in commit order. Returns None when dropped.
        """
        data = state_data(state, pause_reason=pause_reason,
                          at=int(at if at is not None else self._wall()),
                          subtask_id=subtask_id, reason=reason)
        return self._publish_now(task_id, "state", data, version=version, subtask_id=subtask_id)

    def publish_done(self, task_id: str, state: str, *, failed_modules: Iterable[str] = (),
                     subtask_id: str | None = None, reason: str | None = None,
                     version: int | None = None) -> str | None:
        data = done_data(state, failed_modules=failed_modules, subtask_id=subtask_id, reason=reason)
        return self._publish_now(task_id, "done", data, version=version, subtask_id=subtask_id)

    def publish_progress(self, task_id: str, stage: dict) -> str | None:
        """``stage`` is a C4 ``StageProgress`` (cumulative ``done`` / ``total``); other keys are dropped."""
        missing = [k for k in ("id", "state", "done", "total") if k not in stage]
        if missing:
            raise ValueError(f"progress needs {missing} (C4 StageProgress)")
        data = {k: stage[k] for k in _STAGE_KEYS if k in stage}
        stage_id = str(data["id"])
        final = data.get("state") in _FINAL_STAGE_STATES or (
            data.get("total") is not None and data.get("done") == data.get("total"))
        with self._lock:
            th = self._throttle(task_id)
            now = self._clock()
            if final or now - th.last_progress >= self.progress_interval_s:
                th.pending_progress.pop(stage_id, None)
                th.last_progress = now
                return self._emit(task_id, "progress", data)
            th.pending_progress[stage_id] = data
            self._cond.notify_all()
            return None

    def publish_usage(self, task_id: str, totals: dict) -> str | None:
        """``totals`` is a C4 ``UsageTotals`` for the whole task (actual ledger, cumulative)."""
        data = {k: int(totals.get(k) or 0) for k in _USAGE_KEYS}
        with self._lock:
            th = self._throttle(task_id)
            now = self._clock()
            if now - th.last_usage >= self.usage_interval_s:
                th.pending_usage = None
                th.last_usage = now
                return self._emit(task_id, "usage", data)
            th.pending_usage = data
            self._cond.notify_all()
            return None

    def publish_log(self, task_id: str, stage: str, level: str, msg: str,
                    **extra: Any) -> str | None:
        if level not in LOG_LEVELS:
            raise ValueError(f"log level must be one of {LOG_LEVELS}")
        data: dict[str, Any] = {"stage": stage, "level": level, "msg": msg}
        data.update({k: v for k, v in extra.items() if v is not None})
        with self._lock:
            th = self._throttle(task_id)
            now = self._clock()
            if th.log_refilled == float("-inf"):
                th.log_tokens, th.log_refilled = self.log_rate_per_s, now
            th.log_tokens = min(self.log_rate_per_s,
                                th.log_tokens + (now - th.log_refilled) * self.log_rate_per_s)
            th.log_refilled = now
            if th.log_tokens < 1.0:
                th.log_dropped += 1
                return None
            th.log_tokens -= 1.0
            if th.log_dropped:
                data["dropped"] = th.log_dropped
                th.log_dropped = 0
            return self._emit(task_id, "log", data)

    def publish(self, task_id: str, event: str, data: dict) -> str | None:
        """Generic entry point with the same throttling rules as the typed ones."""
        if event == "progress":
            return self.publish_progress(task_id, data)
        if event == "usage":
            return self.publish_usage(task_id, data)
        if event == "log":
            extra = {k: v for k, v in data.items() if k not in ("stage", "level", "msg")}
            return self.publish_log(task_id, data["stage"], data["level"], data["msg"], **extra)
        if event not in EVENT_KINDS:
            raise ValueError(f"unknown SSE event {event!r}")
        return self._publish_now(task_id, event, dict(data))

    def _publish_now(self, task_id: str, event: str, data: dict, *, version: int | None = None,
                     subtask_id: str | None = None) -> str | None:
        with self._lock:
            if version is not None:
                versions = self._throttle(task_id).versions
                key = (event, subtask_id or "")
                if key in versions and version <= versions[key]:
                    return None                  # a newer state already went out
                versions[key] = version
            if event in ("state", "done"):
                self._flush_task(task_id)        # throttled progress / usage first, in order
            return self._emit(task_id, event, data)

    def _flush_task(self, task_id: str) -> None:
        """Emit this task's pending progress and usage now, whatever the interval says."""
        th = self._throttles.get(task_id)
        if th is None:
            return
        now = self._clock()
        if th.pending_progress:
            pending, th.pending_progress = th.pending_progress, {}
            th.last_progress = now
            for data in pending.values():
                self._emit(task_id, "progress", data)
        if th.pending_usage is not None:
            data, th.pending_usage = th.pending_usage, None
            th.last_usage = now
            self._emit(task_id, "usage", data)

    # -- internals (hold the lock) ----------------------------------------------
    def _throttle(self, task_id: str) -> _Throttle:
        th = self._throttles.get(task_id)
        if th is None:
            th = self._throttles[task_id] = _Throttle()
        return th

    def _buffer(self, task_id: str) -> _TaskBuffer:
        buf = self._buffers.get(task_id)
        if buf is None:
            buf = self._buffers[task_id] = _TaskBuffer(
                events=collections.deque(maxlen=self.buffer_size),
                evicted_upto=self._discard_watermark, touched=self._clock())
            self._trim_buffers(keep=task_id)
        buf.touched = self._clock()
        return buf

    def _trim_buffers(self, keep: str) -> None:
        """Forget the least recently used idle buffers beyond ``max_tasks`` (never ``keep``)."""
        if len(self._buffers) <= self.max_tasks:
            return
        idle = sorted((b.touched, tid) for tid, b in self._buffers.items()
                      if not b.subscribers and tid != keep)
        for _, tid in idle[: len(self._buffers) - self.max_tasks]:
            buf = self._buffers.pop(tid)
            if buf.events:
                self._discard_watermark = max(self._discard_watermark, buf.events[-1].seq)
            self._throttles.pop(tid, None)

    def _emit(self, task_id: str, event: str, data: dict) -> str:
        self._seq += 1
        ev = SseEvent(self._seq, event, data)
        buf = self._buffer(task_id)
        if len(buf.events) == buf.events.maxlen:
            buf.evicted_upto = buf.events[0].seq
        buf.events.append(ev)
        for sub in list(buf.subscribers):
            if not sub._deliver(ev):
                buf.subscribers.discard(sub)
        return self.event_id(ev.seq)

    def _flush_due(self) -> float | None:
        """Emit pending throttled events whose interval is over; seconds until the next one."""
        now = self._clock()
        wait: float | None = None
        for task_id, th in list(self._throttles.items()):
            if th.pending_progress:
                due = th.last_progress + self.progress_interval_s
                if now >= due:
                    pending, th.pending_progress = th.pending_progress, {}
                    th.last_progress = now
                    for data in pending.values():
                        self._emit(task_id, "progress", data)
                else:
                    wait = min(wait if wait is not None else due - now, due - now)
            if th.pending_usage is not None:
                due = th.last_usage + self.usage_interval_s
                if now >= due:
                    data, th.pending_usage = th.pending_usage, None
                    th.last_usage = now
                    self._emit(task_id, "usage", data)
                else:
                    wait = min(wait if wait is not None else due - now, due - now)
        return wait

    def flush(self) -> None:
        """Deliver whatever throttled events are due now (the flusher thread calls this too)."""
        with self._lock:
            self._flush_due()

    def _flush_loop(self) -> None:
        with self._cond:
            while not self._stopping:
                wait = self._flush_due()
                self._cond.wait(timeout=wait if wait is not None else 1.0)

    # -- subscribing (event loop side) ------------------------------------------
    def subscribe(self, task_id: str, last_event_id: str | None,
                  loop: asyncio.AbstractEventLoop | None = None) -> Subscription:
        loop = loop or asyncio.get_running_loop()
        sub = Subscription(task_id, loop, self.max_queue)
        with self._lock:
            sub.head = self._seq
            buf = self._buffer(task_id)
            if last_event_id is None:
                sub.fresh = True
            else:
                parsed = parse_event_id(last_event_id)
                if parsed is None or parsed[0] != self.epoch or parsed[1] > self._seq \
                        or parsed[1] < buf.evicted_upto:
                    sub.reset = True
                else:
                    sub.replay = [ev for ev in buf.events if ev.seq > parsed[1]]
            buf.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            sub.closed = True
            buf = self._buffers.get(sub.task_id)
            if buf is not None:
                buf.subscribers.discard(sub)

    def end_streams(self) -> int:
        """Ask every open SSE stream to finish (graceful shutdown); returns how many."""
        with self._lock:
            subs = [s for buf in self._buffers.values() for s in buf.subscribers]
        for s in subs:
            s._end()
        return len(subs)

    def subscriber_count(self, task_id: str) -> int:
        with self._lock:
            buf = self._buffers.get(task_id)
            return len(buf.subscribers) if buf else 0

    def buffered(self, task_id: str) -> list[SseEvent]:
        with self._lock:
            buf = self._buffers.get(task_id)
            return list(buf.events) if buf else []
