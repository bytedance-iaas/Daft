"""Outer retry (04 §6) and adaptive concurrency (04 §7) as standalone components."""
from __future__ import annotations

import pickle
import ssl
import threading
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest
import requests

from curation.contracts import schemas
from curation.planner import (AdaptiveThrottle, CallFailed, ResizableGate, RetryPolicy, RetryStats,
                              VlmTransportError, call_with_retry, classify_failure, derive_gates)
from curation.planner.retry import parse_retry_after


def http_error(status: int, headers=None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.HTTPError(f"{status}", response=response)


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize("exc,cause,retryable", [
    (requests.exceptions.ReadTimeout("slow"), "timeout", True),
    (requests.exceptions.ConnectTimeout("slow"), "timeout", True),
    (requests.exceptions.ConnectionError("reset"), "connect_error", True),
    (requests.exceptions.ChunkedEncodingError("cut"), "connect_error", True),
    (requests.exceptions.SSLError("bad cert"), "tls_error", False),
    (http_error(429, {"Retry-After": "7"}), "rate_limited", True),
    (http_error(503), "server_error", True),
    (http_error(500), "server_error", True),
    (http_error(408), "timeout", True),
    (http_error(401), "http_error", False),
    (http_error(400), "http_error", False),
    (TimeoutError("t"), "timeout", True),
    (ConnectionRefusedError("r"), "connect_error", True),
    (ssl.SSLError("s"), "tls_error", False),
    (ValueError("parse"), "error", False),
    (KeyError("choices"), "error", False),
    (VlmTransportError("x", cause="server_error", status=502), "server_error", True),
])
def test_classify(exc, cause, retryable):
    failure = classify_failure(exc)
    assert (failure.cause, failure.retryable) == (cause, retryable)


def test_retry_after_is_read():
    assert classify_failure(http_error(429, {"Retry-After": "7"})).retry_after_s == 7.0
    now = time.time()
    date = format_datetime(datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=30),
                           usegmt=True)
    assert 25 <= parse_retry_after(date, now=now) <= 31
    assert parse_retry_after("soon") is None and parse_retry_after(None) is None
    assert parse_retry_after("-3") == 0.0

    class StatusOnly(Exception):
        status_code = 429
        headers = {"Retry-After": "2"}

    assert classify_failure(StatusOnly()).retry_after_s == 2.0
    with pytest.raises(ValueError):
        VlmTransportError("x", cause="gremlins")


# ---------------------------------------------------------------- policy

def test_backoff_is_1_2_4_and_honours_retry_after():
    policy = RetryPolicy()
    assert [policy.delay(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert policy.delay(1, retry_after_s=5.0) == 5.0
    assert policy.delay(3, retry_after_s=0.5) == 4.0
    assert policy.delay(1, retry_after_s=3600) == 60.0
    assert RetryPolicy().max_retries == 3
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)


def test_call_with_retry_rescues_and_counts():
    outcomes = iter([http_error(503), requests.exceptions.ReadTimeout("t"), "done"])

    def flaky():
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    stats, waits, failures, successes = RetryStats(), [], [], []
    assert call_with_retry(flaky, RetryPolicy(max_retries=3), sleep=waits.append, stats=stats,
                           on_failure=lambda f, a: failures.append((f.cause, a)),
                           on_success=successes.append) == "done"
    assert waits == [1.0, 2.0] and failures == [("server_error", 1), ("timeout", 2)]
    assert successes == [3]
    assert stats.to_json() == {"calls": 1, "retries": 2, "rescued": 1, "exhausted": 0, "failed": 0}


def test_call_with_retry_gives_up():
    def always_429():
        raise http_error(429, {"Retry-After": "3"})

    stats, waits = RetryStats(), []
    with pytest.raises(CallFailed) as info:
        call_with_retry(always_429, RetryPolicy(max_retries=2), sleep=waits.append, stats=stats)
    assert waits == [3.0, 3.0]
    assert info.value.exhausted and info.value.attempts == 3
    assert info.value.incident("probe", "probe") == {"step": "probe", "call_kind": "probe",
                                                     "cause": "rate_limited", "attempts": 3}
    assert stats.to_json()["exhausted"] == 1


def test_non_retryable_and_default_policy():
    def denied():
        raise http_error(403)

    with pytest.raises(CallFailed) as info:
        call_with_retry(denied, RetryPolicy(max_retries=5), sleep=lambda s: None)
    assert info.value.attempts == 1 and not info.value.exhausted

    calls = []

    def slow():
        calls.append(1)
        raise requests.exceptions.ReadTimeout("t")

    with pytest.raises(CallFailed):
        call_with_retry(slow)                                   # the CLI default: no outer retry
    assert len(calls) == 1


# ---------------------------------------------------------------- ResizableGate

def test_resizable_gate_behaves_like_a_semaphore():
    gate = ResizableGate(2)
    assert gate.acquire() and gate.acquire()
    assert not gate.acquire(blocking=False) and not gate.acquire(timeout=0.01)
    gate.release()
    assert gate.acquire(timeout=0.01)
    gate.set_capacity(1)                                        # shrinking keeps permits already out
    assert gate.in_use == 2
    gate.release()
    assert not gate.acquire(blocking=False)
    gate.release()
    assert gate.acquire(blocking=False)
    gate.release()
    with pytest.raises(RuntimeError):
        gate.release()


def test_resizable_gate_growing_wakes_waiters():
    gate = ResizableGate(1)
    gate.acquire()
    got = []
    waiter = threading.Thread(target=lambda: got.append(gate.acquire(timeout=5)))
    waiter.start()
    time.sleep(0.05)
    gate.set_capacity(2)
    waiter.join(5)
    assert got == [True] and gate.in_use == 2


def test_resizable_gate_pickles_without_its_lock():
    gate = ResizableGate(3)
    gate.acquire()
    clone = pickle.loads(pickle.dumps(gate))
    assert clone.capacity == 3 and clone.in_use == 0 and clone.acquire(blocking=False)


# ---------------------------------------------------------------- AdaptiveThrottle

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def throttle(clock, lines, **kw):
    return AdaptiveThrottle(64, derive_gates(64), backend="ark-prod", clock=clock,
                            wall_clock=lambda: 1758300002.0, emit=lines.append, **kw)


def test_halves_all_gates_when_429s_pile_up():
    clock, lines = Clock(), []
    t = throttle(clock, lines)
    for i in range(20):
        t.record("rate_limited" if i % 5 == 0 else "ok")          # 20 % bad
        clock.t += 0.1
    assert len(lines) == 1
    line = lines[0]
    assert line == {"ts": 1758300002000, "kind": "throttle", "level": "warn", "backend": "ark-prod",
                    "limit": 32, "reason": "429/5xx rate 20% in last 30s",
                    "gates": {k: max(1, v // 2) for k, v in derive_gates(64).items()}}
    schemas.validate("progress.schema.json", line)
    assert t.gates()["probe"] == 32 and t.limit() == 32


def test_one_move_per_window_then_additive_recovery():
    clock, lines = Clock(), []
    t = throttle(clock, lines, min_samples=10)
    for _ in range(10):
        t.record("server_error")
    assert t.limit() == 32
    for _ in range(50):                                          # still bad but within the window
        t.record("server_error")
    assert t.limit() == 32 and len(lines) == 1
    clock.t += 31
    for _ in range(10):
        t.record("server_error")
    assert t.limit() == 16
    for step in range(12):                                       # clean windows: +1/8 each
        clock.t += 31
        for _ in range(10):
            t.record("ok")
    assert t.limit() == 64 and t.gates() == derive_gates(64)
    kinds = [l["level"] for l in lines]
    assert kinds[:2] == ["warn", "warn"] and set(kinds[2:]) == {"info"}
    assert [l["limit"] for l in lines[2:]] == [24, 32, 40, 48, 56, 64]
    for line in lines:
        schemas.validate("progress.schema.json", line)


def test_timeouts_do_not_throttle_and_few_samples_do_not_either():
    clock, lines = Clock(), []
    t = throttle(clock, lines)
    for _ in range(40):
        t.record("timeout")
    for _ in range(4):                                           # 4 of 44 = 9 %, under the bar
        t.record("rate_limited")
    assert lines == [] and t.limit() == 64
    t.record("rate_limited")                                     # 5 of 45 = 11 %
    assert len(lines) == 1 and t.limit() == 32
    lines.clear()
    t2 = throttle(clock, lines)
    for _ in range(19):
        t2.record("rate_limited")
    assert lines == []


def test_a_quiet_backend_still_recovers():
    clock, lines = Clock(), []
    t = throttle(clock, lines)
    for _ in range(20):
        t.record("server_error")
    assert t.limit() == 32
    clock.t += 31
    t.record("ok")                                               # one request in a whole window
    assert t.limit() == 40 and lines[-1]["level"] == "info"
    clock.t += 31
    t.record("server_error")                                     # one error alone cuts nothing
    assert t.limit() == 40


def test_old_samples_leave_the_window():
    clock, lines = Clock(), []
    t = throttle(clock, lines)
    for _ in range(19):
        t.record("rate_limited")
    clock.t += 31
    for _ in range(20):
        t.record("ok")
    assert lines == []


def test_never_below_one_and_bound_gates_follow():
    clock, lines = Clock(), []
    t = throttle(clock, lines, min_samples=1)
    gates = {name: ResizableGate(size) for name, size in derive_gates(64).items()}
    t.bind(gates)
    for _ in range(12):
        t.record("rate_limited")
        clock.t += 31
    assert t.limit() == 1 and all(v == 1 for v in t.gates().values())
    assert all(g.capacity == 1 for g in gates.values())
    with pytest.raises(ValueError):
        AdaptiveThrottle(0, {"probe": 1}, backend="x")
    with pytest.raises(ValueError):
        AdaptiveThrottle(8, {"probe": 1}, backend="x", threshold=1.5)
