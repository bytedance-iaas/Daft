"""The Daemon's CPU pool: one slot per CPU worker, shared by every running task (04 §2.3, D54).

Its size is the node's cores minus two (:attr:`OrchestratorConfig.cpu_workers`, P4). A
task's plan only says how many CPU workers the task may use at most; how many it gets
is decided here, one episode at a time:

* the pipeline (:func:`episode_pipeline.run_episodes`) takes a slot before it hands a CPU
  episode to its worker and holds it until the episode is done - finished, erred, paused
  or stopped. It reconciles what it holds with the episodes actually in flight on every
  turn of its loop (:meth:`CpuPool.sync`), so the slots of a crashed worker come back with
  the episodes it lost, and it leaves the pool (:meth:`CpuPool.leave`) only after its
  workers have wound down;
* a whole-stage command (a retry, a task that predates the pipeline) takes a block of
  slots for as long as it runs (:meth:`CpuPool.block`) and passes their number to the
  CLI as ``--concurrency``.

Fair shares, so that a task started later is not kept waiting by one that filled the pool
first: a task that asked and did not get everything is *waiting* (for ``WAIT_S`` after its
last unmet request). While some task waits below its share - the pool divided by the tasks
holding or waiting - a freed slot goes to no task at or above its own share. Nothing is
taken back from a task above its share: it shrinks to it as its episodes finish. When
nobody waits, a task may use every free slot.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Callable, Hashable, Iterator

log = logging.getLogger("daemon.orchestr")

#: an unmet request keeps its task waiting this long (its loop asks again every few ms)
WAIT_S = 2.0
#: how often a whole-stage block looks again while it waits for slots
BLOCK_POLL_S = 0.1


class CpuPool:
    def __init__(self, size: int, *, clock: Callable[[], float] = time.monotonic):
        self.size = max(1, int(size))
        self._clock = clock
        self._lock = threading.Lock()
        self._held: dict[Hashable, int] = {}
        self._waiting: dict[Hashable, float] = {}
        self.used = 0
        self.peak = 0

    # -- the rule -------------------------------------------------------------------------
    def _expire(self, now: float) -> None:
        for key in [k for k, at in self._waiting.items() if now - at > WAIT_S]:
            del self._waiting[key]

    def _share(self, key: Hashable) -> int:
        members = {k for k, n in self._held.items() if n > 0} | set(self._waiting) | {key}
        return max(1, self.size // len(members))

    def acquire(self, key: Hashable, want: int) -> int:
        """Take up to ``want`` slots for ``key`` without waiting; returns how many it got."""
        if want <= 0:
            return 0
        with self._lock:
            now = self._clock()
            self._expire(now)
            held = self._held.get(key, 0)
            grant = min(want, self.size - self.used)
            if grant > 0:
                share = self._share(key)
                if any(k != key and self._held.get(k, 0) < share for k in self._waiting):
                    grant = min(grant, max(0, share - held))
            if grant < want:
                self._waiting[key] = now
            else:
                self._waiting.pop(key, None)
            if grant > 0:
                self._held[key] = held + grant
                self.used += grant
                self.peak = max(self.peak, self.used)
            return grant

    def sync(self, key: Hashable, in_flight: int) -> None:
        """``key`` holds exactly ``in_flight`` slots: the episodes it still has out."""
        with self._lock:
            held = self._held.get(key, 0)
            if in_flight != held:
                self.used += in_flight - held
                self.peak = max(self.peak, self.used)
                if in_flight > 0:
                    self._held[key] = in_flight
                else:
                    self._held.pop(key, None)

    def release(self, key: Hashable, count: int) -> None:
        with self._lock:
            held = self._held.get(key, 0)
            count = min(max(0, count), held)
            self.used -= count
            if held - count > 0:
                self._held[key] = held - count
            else:
                self._held.pop(key, None)

    def leave(self, key: Hashable) -> None:
        """Give back everything ``key`` holds and stop counting it as waiting."""
        with self._lock:
            self.used -= self._held.pop(key, 0)
            self._waiting.pop(key, None)

    def held(self, key: Hashable) -> int:
        with self._lock:
            return self._held.get(key, 0)

    def snapshot(self) -> dict:
        with self._lock:
            self._expire(self._clock())
            return {"size": self.size, "used": self.used, "peak": self.peak,
                    "held": {str(k): n for k, n in self._held.items()},
                    "waiting": sorted(str(k) for k in self._waiting)}

    # -- a whole-stage command ------------------------------------------------------------
    @contextlib.contextmanager
    def block(self, key: Hashable, want: int, *, check: Callable[[], None] = lambda: None,
              on_wait: Callable[[], None] | None = None,
              sleep: Callable[[float], None] = time.sleep) -> Iterator[int]:
        """Hold a block of slots for one command: at least one, and waits until it has its
        fair share (or ``want``, if that is less). ``check`` runs while it waits - a pause
        or stop raises there and the slots gathered so far go back. Yields the count."""
        want, got = max(1, int(want)), 0
        try:
            while True:
                got += self.acquire(key, want - got)
                with self._lock:
                    if got >= max(1, min(want, self._share(key))):
                        self._waiting.pop(key, None)      # content with what it holds
                        break
                if on_wait is not None:
                    on_wait()
                    on_wait = None
                check()
                sleep(BLOCK_POLL_S)
            yield got
        finally:
            self.release(key, got)
            with self._lock:
                self._waiting.pop(key, None)
