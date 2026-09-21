"""Outer logical retry, ``vlm_retry`` (design doc 04, section 6).

v1's client already has two inner mechanisms, kept as they are: hedging for
requests with images (``hedged_request``: a second shot at T, one serial resend
on 5xx or a connection error, one more try after two timeouts, 4xx including 429
handed back untouched) and 1/2/4 s retries for text calls (``llm_ask``). The outer
retry wraps a whole logical call and only starts after the inner layer gave up:

* retried: timeouts (and HTTP 408), connection errors, HTTP 5xx, HTTP 429;
* waits 1 s, 2 s, 4 s, ... between tries; on 429 at least ``Retry-After``;
* default 3 retries (task parameter ``vlm_retry``); the CLI default is 0.

Anything else (other 4xx, TLS errors, a response that cannot be read) is not
retried. When the retries run out the call fails with :class:`CallFailed`; the
check shell records the episode as ``verdict = error`` with the incident (D33).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, TypeVar

T = TypeVar("T")

#: Failure causes; the first four are the ones the outer retry retries.
RETRYABLE_CAUSES = frozenset({"timeout", "connect_error", "server_error", "rate_limited"})
CAUSES = tuple(sorted(RETRYABLE_CAUSES)) + ("http_error", "tls_error", "error")


class VlmTransportError(Exception):
    """What a transport adapter may raise; ``requests`` exceptions are understood too."""

    def __init__(self, message: str, *, cause: str, status: int | None = None,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        if cause not in CAUSES:
            raise ValueError(f"cause must be one of {CAUSES}, got {cause!r}")
        self.cause, self.status, self.retry_after_s = cause, status, retry_after_s


@dataclass(frozen=True)
class Failure:
    cause: str
    retryable: bool
    status: int | None = None
    retry_after_s: float | None = None
    detail: str = ""


def parse_retry_after(value: Any, now: float | None = None) -> float | None:
    """``Retry-After`` in seconds: a number of seconds or an HTTP date."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    return max(0.0, seconds)


def _status_failure(status: int, headers: Any, detail: str) -> Failure:
    if status == 429:
        after = parse_retry_after(headers.get("Retry-After")) if hasattr(headers, "get") else None
        return Failure("rate_limited", True, status, after, detail)
    if status == 408:
        return Failure("timeout", True, status, None, detail)
    if 500 <= status < 600:
        return Failure("server_error", True, status, None, detail)
    return Failure("http_error", False, status, None, detail)


def classify_failure(exc: BaseException) -> Failure:
    """Map an exception from a transport to a :class:`Failure`."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, VlmTransportError):
        return Failure(exc.cause, exc.cause in RETRYABLE_CAUSES, exc.status, exc.retry_after_s,
                       detail)
    try:
        import requests
    except ImportError:                                     # pragma: no cover
        requests = None
    if requests is not None:
        rex = requests.exceptions
        if isinstance(exc, rex.SSLError):
            return Failure("tls_error", False, detail=detail)
        if isinstance(exc, rex.Timeout):
            return Failure("timeout", True, detail=detail)
        if isinstance(exc, (rex.ConnectionError, rex.ChunkedEncodingError)):
            return Failure("connect_error", True, detail=detail)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return _status_failure(status, getattr(response, "headers", {}) or {}, detail)
    for name in ("status_code", "status"):
        status = getattr(exc, name, None)
        if isinstance(status, int) and not isinstance(status, bool):
            return _status_failure(status, getattr(exc, "headers", {}) or {}, detail)
    import ssl

    if isinstance(exc, ssl.SSLError):
        return Failure("tls_error", False, detail=detail)
    if isinstance(exc, TimeoutError):
        return Failure("timeout", True, detail=detail)
    if isinstance(exc, ConnectionError):
        return Failure("connect_error", True, detail=detail)
    return Failure("error", False, detail=detail)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3             # task parameter vlm_retry (0-5); the CLI default is 0
    base_delay_s: float = 1.0        # 1 s, 2 s, 4 s, ...
    max_retry_after_s: float = 60.0  # a server asking for longer is not waited for beyond this

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) \
                or self.max_retries < 0:
            raise ValueError(f"max_retries must be a non-negative integer, got {self.max_retries!r}")
        if self.base_delay_s < 0 or self.max_retry_after_s < 0:
            raise ValueError("delays cannot be negative")

    def delay(self, retry_number: int, retry_after_s: float | None = None) -> float:
        """Wait before retry ``retry_number`` (1-based)."""
        backoff = self.base_delay_s * 2 ** (retry_number - 1)
        if retry_after_s is None:
            return backoff
        return max(backoff, min(retry_after_s, self.max_retry_after_s))


class RetryStats:
    """Outer retries for the performance profile: tries, rescues, give-ups (06 §6.1)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = self.retries = self.rescued = self.exhausted = self.failed = 0

    def _bump(self, **delta: int) -> None:
        with self._lock:
            for name, value in delta.items():
                setattr(self, name, getattr(self, name) + value)

    def to_json(self) -> dict[str, int]:
        with self._lock:
            return {"calls": self.calls, "retries": self.retries, "rescued": self.rescued,
                    "exhausted": self.exhausted, "failed": self.failed}


class CallFailed(Exception):
    """A logical call that failed for good; ``exhausted`` when retries ran out."""

    def __init__(self, failure: Failure, attempts: int, exhausted: bool) -> None:
        super().__init__(failure.detail or failure.cause)
        self.failure, self.attempts, self.exhausted = failure, attempts, exhausted

    def incident(self, step: str, call_kind: str) -> dict[str, Any]:
        """The ``error.incidents[]`` entry of a result record (common.schema ``incident``)."""
        return {"step": step, "call_kind": call_kind, "cause": self.failure.cause,
                "attempts": self.attempts}


def call_with_retry(fn: Callable[[], T], policy: RetryPolicy | None = None, *,
                    classify: Callable[[BaseException], Failure] = classify_failure,
                    sleep: Callable[[float], None] = time.sleep,
                    stats: RetryStats | None = None,
                    on_failure: Callable[[Failure, int], None] | None = None,
                    on_success: Callable[[int], None] | None = None) -> T:
    """Run one logical call with the outer retry; raise :class:`CallFailed` when it fails.

    ``on_failure(failure, attempt)`` and ``on_success(attempt)`` see every try (the
    adaptive throttle and the unknown-usage count listen here).
    """
    policy = policy or RetryPolicy(max_retries=0)
    stats = stats or RetryStats()
    stats._bump(calls=1)
    attempt = 0
    while True:
        attempt += 1
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            failure = classify(exc)
            if on_failure is not None:
                on_failure(failure, attempt)
            if failure.retryable and attempt - 1 < policy.max_retries:
                stats._bump(retries=1)
                sleep(policy.delay(attempt, failure.retry_after_s))
                continue
            if failure.retryable:
                stats._bump(exhausted=1)
            else:
                stats._bump(failed=1)
            raise CallFailed(failure, attempt, exhausted=failure.retryable) from exc
        if on_success is not None:
            on_success(attempt)
        if attempt > 1:
            stats._bump(rescued=1)
        return result
