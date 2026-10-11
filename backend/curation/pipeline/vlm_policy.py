"""How a command talks to the model: hedging on or off, the outer retry, token usage.

The CLI's discipline (design doc 02 §1, 04 §6): no retry and no hedging unless asked for (``--retry N`` /
``--hedge``). The model clients send through ``vlm_client.hedged_request``; for the duration of a command
:class:`installed` swaps that module attribute (the factories look it up at call time) and restores it afterwards.
``--hedge`` hands a logical call to the original ``hedged_request`` unchanged (a second shot at the timeout line, one
serial resend on 5xx / connection errors, one more after two timeouts); without it the call is one HTTP request, sent
under the same gate and with the same latency row. Either way the outer retry (``--retry N``, 1 s / 2 s / 4 s, at
least ``Retry-After`` on 429) wraps it: timeouts, connection errors, 5xx and 429 are tried again, anything else is not.

When a call finally fails, the exception (or the error response the factory will raise on) carries
``curation_failure = {"cause", "attempts"}``, which the per-episode incident wrappers (:mod:`.incidents`) put into
the record (D33).

Token usage: ``vlm_client`` hands every HTTP request it sends to a sink (:func:`vlm_client.set_usage_sink`); here it
is booked on W6's two ledgers under the command's module (``task_success``, the EEF module) and the request's latency
tag as ``call_kind``, and every line is emitted as a C3 ``usage`` event and appended to the run directory's
``usage.jsonl``. ``--vlm-reasoning-effort <level>`` adds ``reasoning_effort`` to every chat request while the policy
is installed; without it nothing is added.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from ..planner.retry import (Failure, RetryPolicy, RetryStats, _status_failure,
                             classify_failure)
from ..planner.usage import UsageLedger, parse_usage


@dataclass
class TransportPolicy:
    hedge: bool = False
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_retries=0))
    stats: RetryStats = field(default_factory=RetryStats)
    sleep: Callable[[float], None] = time.sleep
    #: called with (tag, "ok" | failure cause) for every logical call (the throttle's feed)
    on_outcome: Callable[[str, str], None] | None = None
    #: sent as ``reasoning_effort`` in every chat request when set (the Daemon checks
    #: it against the model); None sends nothing
    reasoning_effort: str | None = None


_ACTIVE: dict = {"policy": None, "orig": None}


def _with_reasoning_effort(post, effort: str):
    """``requests.post`` adding ``reasoning_effort`` to chat-completion bodies. v1's
    clients look ``requests.post`` up when they send, so every model request gets it."""

    def post_with_effort(url, *args, **kwargs):
        payload = kwargs.get("json")
        if isinstance(payload, dict) and "reasoning_effort" not in payload \
                and str(url).rstrip("/").endswith("/chat/completions"):
            kwargs["json"] = {**payload, "reasoning_effort": effort}
        return post(url, *args, **kwargs)

    return post_with_effort
_LOCK = threading.Lock()


def active() -> TransportPolicy | None:
    return _ACTIVE["policy"]


def _mark(obj, failure: Failure, attempts: int, tag: str) -> None:
    info = {"cause": failure.cause, "attempts": attempts, "call_kind": tag,
            "status": failure.status}
    try:
        obj.curation_failure = info
    except Exception:  # noqa: BLE001 - some exception types refuse attributes
        pass


def _single_request(send, *, tag: str, timeout_s: float, gate=None):
    """One logical call = one HTTP request (``--hedge`` not given).

    Same gate discipline as v1 (the permit is held while the request is in flight)
    and the same 7-column latency row, so the performance profile reads alike.
    """
    import requests as _rq

    from ..adapters import vlm_client

    if gate is not None and not gate.acquire(timeout=vlm_client.GATE_WAIT_FUSE_S):
        raise _rq.exceptions.Timeout(
            f"{tag}: 等待并发闸门许可超过保险丝 {vlm_client.GATE_WAIT_FUSE_S:.0f}s(疑似闸门泄漏)")
    call_id = uuid.uuid4().hex[:12]
    t1 = time.time()
    ok, kind, resp = False, "connect_error", None
    try:
        resp = send(timeout_s)
        ok = bool(resp.ok)
        kind = "" if ok else "http_error"
        return resp
    except Exception as e:
        if isinstance(e, _rq.exceptions.Timeout):
            kind = "timeout"
        raise
    finally:
        if gate is not None:
            gate.release()
        vlm_client.latency_record(tag, time.time() - t1, ok, started_at=t1,
                                  call_id=call_id, attempt=0, fail_kind=kind)
        vlm_client.usage_note(tag, resp)


def policy_hedged_request(send, *, tag: str, timeout_s: float, gate=None):
    """``vlm_client.hedged_request`` while a policy is installed (see module docstring)."""
    policy = _ACTIVE["policy"] or TransportPolicy(hedge=True)
    orig = _ACTIVE["orig"]["hedged_request"] if _ACTIVE["orig"] else None
    policy.stats._bump(calls=1)
    attempt = 0
    while True:
        attempt += 1
        try:
            if policy.hedge and orig is not None:
                resp = orig(send, tag=tag, timeout_s=timeout_s, gate=gate)
            else:
                resp = _single_request(send, tag=tag, timeout_s=timeout_s, gate=gate)
        except Exception as exc:
            failure = classify_failure(exc)
            _outcome(policy, tag, failure.cause)
            if failure.retryable and attempt - 1 < policy.retry.max_retries:
                policy.stats._bump(retries=1)
                policy.sleep(policy.retry.delay(attempt, failure.retry_after_s))
                continue
            policy.stats._bump(**({"exhausted": 1} if failure.retryable else {"failed": 1}))
            _mark(exc, failure, attempt, tag)
            raise
        status = getattr(resp, "status_code", 200)
        if resp is not None and not getattr(resp, "ok", True):
            failure = _status_failure(int(status), getattr(resp, "headers", {}) or {},
                                      f"HTTP {status}")
            _outcome(policy, tag, failure.cause)
            if failure.retryable and attempt - 1 < policy.retry.max_retries:
                policy.stats._bump(retries=1)
                policy.sleep(policy.retry.delay(attempt, failure.retry_after_s))
                continue
            policy.stats._bump(**({"exhausted": 1} if failure.retryable else {"failed": 1}))
            _mark(resp, failure, attempt, tag)
            return resp
        _outcome(policy, tag, "ok")
        if attempt > 1:
            policy.stats._bump(rescued=1)
        return resp


def _outcome(policy: TransportPolicy, tag: str, outcome: str) -> None:
    if policy.on_outcome is not None:
        try:
            policy.on_outcome(tag, outcome)
        except Exception:  # noqa: BLE001
            pass


class installed:
    """``with installed(policy, usage=booker): ...`` - swap the transport for a command."""

    def __init__(self, policy: TransportPolicy, usage: "UsageBooker | None" = None):
        self.policy, self.usage = policy, usage

    def __enter__(self):
        from ..adapters import vlm_client

        with _LOCK:
            if _ACTIVE["policy"] is not None:
                raise RuntimeError("a transport policy is already installed")
            import requests

            _ACTIVE["orig"] = {"hedged_request": vlm_client.hedged_request, "post": requests.post}
            _ACTIVE["policy"] = self.policy
            vlm_client.hedged_request = policy_hedged_request
            if self.policy.reasoning_effort:
                requests.post = _with_reasoning_effort(requests.post,
                                                       str(self.policy.reasoning_effort))
            if self.usage is not None:
                vlm_client.set_usage_sink(self.usage.note)
        return self

    def __exit__(self, *exc):
        from ..adapters import vlm_client

        with _LOCK:
            orig = _ACTIVE["orig"] or {}
            if orig:
                import requests

                vlm_client.hedged_request = orig["hedged_request"]
                requests.post = orig["post"]
            vlm_client.set_usage_sink(None)
            _ACTIVE["policy"] = None
            _ACTIVE["orig"] = None
        return False


# ---------------------------------------------------------------- usage

class UsageBooker:
    """Books every sent request on W6's ledgers under one module (C3 ``usage`` lines).

    ``emit`` gets each line (the CLI writes it to stderr); ``persist`` too (the run
    directory's ``usage.jsonl``, which ``report`` sums). A request whose response
    carries no readable ``usage`` counts in ``requests_unknown_usage``, never estimated.
    """

    def __init__(self, module: str, model: str, *, emit: Callable[[dict], None] | None = None,
                 persist: Callable[[dict], None] | None = None, by_tag: dict[str, str] | None = None):
        self.module, self.model = module, model
        self.by_tag = dict(by_tag or {})       # a call kind of another module in the same session (D49: EEF)
        self._persist = persist

        def out(line: dict) -> None:
            if emit is not None:
                emit(line)
            if persist is not None:
                persist(line)

        self.ledger = UsageLedger(emit=out)

    def note(self, tag: str, response) -> None:
        usage = None
        if response is not None and getattr(response, "ok", False):
            try:
                usage = parse_usage((response.json() or {}).get("usage"))
            except Exception:  # noqa: BLE001 - an unreadable body has no usage
                usage = None
        self.ledger.record(model=self.model, call_kind=str(tag or "probe"),
                           shares={self.by_tag.get(str(tag), self.module): (1, 1)}, usage=usage)

    def totals(self) -> dict:
        return self.ledger.task_totals()
