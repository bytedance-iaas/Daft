"""How a v2 command talks to the model: hedging on or off, the outer retry, token usage.

The CLI's discipline (design doc 02 §1, 04 §6): no retry and no hedging unless
asked for (``--retry N`` / ``--hedge``). v1's model clients always hedge and
build their requests inside ``adapters/vlm_client.py``, which may not change
beyond the usage side channel (doc 10 §2.2). So for the duration of a command
:class:`installed` swaps two module attributes of ``vlm_client`` that its
factories look up at call time, and restores them afterwards:

* ``hedged_request`` - one logical call with images. ``--hedge`` hands it to
  v1's own ``hedged_request`` unchanged (a second shot at the timeout line, one
  serial resend on 5xx / connection errors, one more after two timeouts);
  without it the call is one HTTP request, sent under the same gate and with
  the same latency row. Either way the outer retry (``--retry N``, 1 s / 2 s /
  4 s, at least ``Retry-After`` on 429) wraps it: timeouts, connection errors,
  5xx and 429 are tried again, anything else is not.
* ``make_llm_ask`` - v1's text client tries four times (1/2/4 s) and never
  lets its gate below 2. Here a text call is one HTTP request under a gate of
  exactly ``max_in_flight``, with v1's request body, latency row and answer
  checks; the outer retry stands in for the built-in tries, so ``--retry 3``
  is v1's behaviour.

When a call finally fails, the exception (or the error response the factory
will raise on) carries ``curation_failure = {"cause", "attempts"}``, which the
per-episode incident wrappers (:mod:`.incidents`) put into the record (D33).
A client that swallows the failure (the review voter answers ``unavail``) is
covered by :class:`failures`: the failures of the calls made from the current
thread, and from the pool v1's clients fan out to (``_map_concurrent``, carried
over while a policy is installed).

Token usage: ``vlm_client`` hands every HTTP request it sends to a sink
(:func:`vlm_client.set_usage_sink`); here it is booked on W6's two ledgers
under the command's module (``autolabel``, ``task_success``, ``skill_profile``)
and the request's latency tag as ``call_kind``, and every line is emitted as a
C3 ``usage`` event and appended to the run directory's ``usage.jsonl``.

Nothing here changes a request body, so the VLM call graph stays v1's - with
one opt-in exception: ``--vlm-reasoning-effort <level>`` (``reasoning_effort``)
adds that field to every chat request while the policy is installed. Without
it nothing is added (v1 never sent a thinking parameter; parity runs without).
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


_TL = threading.local()


class failures:
    """``with failures() as seen:`` - the model calls that finally failed while the
    block ran, made from this thread or from v1's fan-out pool it started."""

    def __enter__(self) -> list[dict]:
        self._prev = getattr(_TL, "sink", None)
        self.seen: list[dict] = []
        _TL.sink = self.seen
        return self.seen

    def __exit__(self, *exc):
        _TL.sink = self._prev
        return False


def _mark(obj, failure: Failure, attempts: int, tag: str) -> None:
    info = {"cause": failure.cause, "attempts": attempts, "call_kind": tag,
            "status": failure.status}
    sink = getattr(_TL, "sink", None)
    if sink is not None:
        sink.append(dict(info))
    try:
        obj.curation_failure = info
    except Exception:  # noqa: BLE001 - some exception types refuse attributes
        pass


def policy_map_concurrent(fn, items, max_concurrency):
    """``vlm_client._map_concurrent`` with the caller's :class:`failures` sink carried
    into the worker threads (same pool size, same order)."""
    orig = _ACTIVE["orig"]["_map_concurrent"]
    sink = getattr(_TL, "sink", None)
    if sink is None:
        return orig(fn, items, max_concurrency)

    def carried(item):
        prev = getattr(_TL, "sink", None)
        _TL.sink = sink
        try:
            return fn(item)
        finally:
            _TL.sink = prev

    return orig(carried, items, max_concurrency)


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


class SingleLlmAsk:
    """One attempt of v1's ``make_llm_ask`` client (``adapters/vlm_client.py``).

    Same URL, headers (resolved when the client is built), request body, gate
    discipline, latency row and answer checks; one request per call. Holds only
    picklable state, like v1's clients.
    """

    def __init__(self, endpoint: str, model: str, timeout_s: float | None = None,
                 max_tokens: int = 8192, api_key_env: str | None = None,
                 max_in_flight: int = 2, gate=None):
        from ..adapters import vlm_client

        self.url = endpoint.rstrip("/") + "/chat/completions"
        self.model, self.max_tokens = model, max_tokens
        self.timeout_s = (vlm_client.DEFAULT_TIMEOUTS_S["llm"] if timeout_s is None
                          else timeout_s)
        self.headers = vlm_client.auth_headers(api_key_env)
        self.gate = gate if gate is not None else vlm_client.SharedGate(
            max(1, int(max_in_flight)))

    def __call__(self, prompt_text: str) -> str:
        import requests

        from ..adapters import vlm_client

        payload = {"model": self.model, "temperature": 0.0, "max_tokens": self.max_tokens,
                   "messages": [{"role": "user", "content": prompt_text}]}
        if not self.gate.acquire(timeout=vlm_client.GATE_WAIT_FUSE_S):
            raise requests.exceptions.Timeout("VLM 等待并发闸门超时")
        started = time.time()
        ok, fail_kind, sent = False, "connect_error", None
        try:
            r = sent = requests.post(self.url, json=payload, headers=self.headers,
                                     timeout=self.timeout_s)
            fail_kind = "http_error"
            r.raise_for_status()
            ok, fail_kind = True, ""
        except requests.exceptions.Timeout:
            fail_kind = "timeout"
            raise
        finally:
            self.gate.release()
            vlm_client.latency_record("llm", time.time() - started, ok, started_at=started,
                                      call_id=uuid.uuid4().hex[:12], attempt=0,
                                      fail_kind=fail_kind)
            vlm_client.usage_note("llm", sent)
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError(
                f"LLM 输出被截断 (finish_reason=length, max_tokens={self.max_tokens}, "
                f"model={self.model}); 请提高输出 token 上限或减少归纳输入量。")
        content = choice["message"].get("content")
        if not isinstance(content, str) or not vlm_client.strip_reasoning(content).strip():
            raise ValueError(
                f"LLM 未返回有效文本 (finish_reason={choice.get('finish_reason')!r}, "
                f"model={self.model})")
        return vlm_client.strip_reasoning(content)


class RetryingLlmAsk:
    """A text client with the outer retry (``--retry N``, 1/2/4 s) around it."""

    def __init__(self, inner: Callable[[str], str]):
        self.inner = inner

    def __call__(self, prompt_text: str) -> str:
        policy = _ACTIVE["policy"] or TransportPolicy()
        policy.stats._bump(calls=1)
        attempt = 0
        while True:
            attempt += 1
            try:
                out = self.inner(prompt_text)
            except Exception as exc:
                failure = classify_failure(exc)
                _outcome(policy, "llm", failure.cause)
                if failure.retryable and attempt - 1 < policy.retry.max_retries:
                    policy.stats._bump(retries=1)
                    policy.sleep(policy.retry.delay(attempt, failure.retry_after_s))
                    continue
                policy.stats._bump(**({"exhausted": 1} if failure.retryable
                                      else {"failed": 1}))
                _mark(exc, failure, attempt, "llm")
                raise
            _outcome(policy, "llm", "ok")
            if attempt > 1:
                policy.stats._bump(rescued=1)
            return out


def policy_make_llm_ask(*args, **kwargs):
    """``vlm_client.make_llm_ask`` while a policy is installed: one request per try."""
    return RetryingLlmAsk(SingleLlmAsk(*args, **kwargs))


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

            _ACTIVE["orig"] = {"hedged_request": vlm_client.hedged_request,
                               "make_llm_ask": vlm_client.make_llm_ask,
                               "_map_concurrent": vlm_client._map_concurrent,
                               "post": requests.post}
            _ACTIVE["policy"] = self.policy
            vlm_client.hedged_request = policy_hedged_request
            vlm_client.make_llm_ask = policy_make_llm_ask
            vlm_client._map_concurrent = policy_map_concurrent
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
                vlm_client.make_llm_ask = orig["make_llm_ask"]
                vlm_client._map_concurrent = orig["_map_concurrent"]
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
                 persist: Callable[[dict], None] | None = None):
        self.module, self.model = module, model
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
                           shares={self.module: (1, 1)}, usage=usage)

    def totals(self) -> dict:
        return self.ledger.task_totals()
