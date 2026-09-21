"""Adaptive concurrency for one VLM backend (design doc 04, section 7; P2).

It lives in the CLI process, next to the gates (the Daemon cannot reach a running
child's semaphores): when more than ``threshold`` of the requests in the last
``window_s`` seconds came back 429 or 5xx, all eight gates are halved together;
once a window is clean again they climb back additively, one ``increase_step`` of
their planned size per window. Every change is reported as a C3 ``throttle`` line
(``docs/contracts/progress.schema.json``); the Daemon logs it and raises an alert.

:class:`ResizableGate` is the semaphore it can resize while permits are out; it
has v1's ``SharedGate`` interface (``acquire(blocking, timeout)`` / ``release``)
and pickles the same way (capacity only; the lock is rebuilt on first use).
"""
from __future__ import annotations

import collections
import threading
import time
from fractions import Fraction
from typing import Any, Callable, Mapping

#: Outcomes that count against the backend: HTTP 429 and 5xx (04 §7).
THROTTLE_OUTCOMES = frozenset({"rate_limited", "server_error"})

_BUILD_LOCK = threading.Lock()


class ResizableGate:
    """A counting semaphore whose capacity can change at any time.

    Shrinking never takes back permits already out; new acquires just wait until
    fewer than ``capacity`` are in use.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._in_use = 0
        self._cond: threading.Condition | None = None

    def _condition(self) -> threading.Condition:
        if self._cond is None:
            with _BUILD_LOCK:
                if self._cond is None:
                    self._cond = threading.Condition()
        return self._cond

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use(self) -> int:
        return self._in_use

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        cond = self._condition()
        with cond:
            if not blocking:
                timeout = 0.0
            deadline = None if timeout is None else time.monotonic() + timeout
            while self._in_use >= self._capacity:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                cond.wait(remaining)
            self._in_use += 1
            return True

    def release(self) -> None:
        cond = self._condition()
        with cond:
            if self._in_use <= 0:
                raise RuntimeError("release() without acquire()")
            self._in_use -= 1
            cond.notify()

    def set_capacity(self, capacity: int) -> None:
        cond = self._condition()
        with cond:
            self._capacity = max(1, int(capacity))
            cond.notify_all()

    def __getstate__(self) -> dict[str, Any]:
        return {"capacity": self._capacity}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._capacity, self._in_use, self._cond = state["capacity"], 0, None


class AdaptiveThrottle:
    """Multiplicative decrease, additive increase over the eight gates of one backend."""

    def __init__(self, parallelism: int, gates: Mapping[str, int], *, backend: str,
                 window_s: float = 30.0, threshold: float = 0.10, min_samples: int = 20,
                 increase_step: float = 0.125,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 emit: Callable[[dict], None] | None = None,
                 on_change: Callable[[dict[str, int]], None] | None = None) -> None:
        if isinstance(parallelism, bool) or not isinstance(parallelism, int) or parallelism < 1:
            raise ValueError("parallelism must be a positive integer")
        if not gates or any(int(v) < 1 for v in gates.values()):
            raise ValueError("gates must be positive integers")
        if not 0 < threshold < 1 or window_s <= 0 or min_samples < 1 or not 0 < increase_step <= 1:
            raise ValueError("bad throttle settings")
        self.parallelism, self.backend = parallelism, backend
        self._base = {k: int(v) for k, v in gates.items()}
        self.window_s, self.threshold, self.min_samples = window_s, threshold, min_samples
        self._step = Fraction(increase_step).limit_denominator(1000)
        self._min_scale = Fraction(1, max(parallelism, *self._base.values()))
        self._scale = Fraction(1)
        self._clock, self._wall_clock = clock, wall_clock
        self._emit, self._on_change = emit, on_change
        self._samples: collections.deque[tuple[float, bool]] = collections.deque()
        self._last_change = float("-inf")
        self._lock = threading.Lock()

    @property
    def scale(self) -> Fraction:
        return self._scale

    def limit(self) -> int:
        return max(1, int(self.parallelism * self._scale))

    def gates(self) -> dict[str, int]:
        return {name: max(1, int(base * self._scale)) for name, base in self._base.items()}

    def bind(self, gates: Mapping[str, ResizableGate]) -> None:
        """Resize these semaphores on every change (and now)."""
        def resize(sizes: dict[str, int]) -> None:
            for name, gate in gates.items():
                gate.set_capacity(sizes[name])

        self._on_change = resize
        resize(self.gates())

    def record(self, outcome: str) -> dict[str, Any] | None:
        """Note one request's outcome; returns the ``throttle`` line if the gates moved.

        ``outcome`` is ``ok`` or a failure cause (``curation.planner.retry``).
        """
        now = self._clock()
        with self._lock:
            self._samples.append((now, outcome in THROTTLE_OUTCOMES))
            while self._samples and self._samples[0][0] < now - self.window_s:
                self._samples.popleft()
            if now - self._last_change < self.window_s:
                return None
            bad = sum(1 for _, flag in self._samples if flag)
            ratio = bad / len(self._samples)
            rate = f"429/5xx rate {ratio:.0%} in last {self.window_s:g}s"
            # cutting needs enough evidence; climbing back needs one full window that
            # stayed clean, however quiet (else a low-traffic backend never recovers)
            if (len(self._samples) >= self.min_samples and ratio > self.threshold
                    and self._scale > self._min_scale):
                self._scale = max(self._min_scale, self._scale / 2)
                level, reason = "warn", rate
            elif ratio <= self.threshold / 2 and self._scale < 1:
                self._scale = min(Fraction(1), self._scale + self._step)
                level, reason = "info", f"recovering: {rate}"
            else:
                return None
            self._last_change = now
            self._samples.clear()
            sizes = self.gates()
            line = {"ts": int(self._wall_clock() * 1000), "kind": "throttle", "level": level,
                    "backend": self.backend, "limit": self.limit(), "reason": reason,
                    "gates": sizes}
        if self._on_change is not None:
            self._on_change(sizes)
        if self._emit is not None:
            self._emit(line)
        return line
