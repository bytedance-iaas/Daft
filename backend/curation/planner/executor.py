"""Merge executor: sends a stage's units through an injected transport (04 §4.2).

Where each of the three steps happens:

* **proposal** - the planner writes which modules may share a request into
  ``plan.json`` (``stages[].merge``); ``check --plan-stage`` hands it over;
* **execution** - here, inside the ``check`` process: the units that are ready
  now are grouped (:mod:`curation.planner.merge`) and sent. A unit still waiting
  for a prerequisite (the task text from autolabel) is simply not passed in yet;
  it goes in a later :meth:`MergeExecutor.run` and is never waited for;
* **receipt** - every unit records how it went out: ``merged``, ``split`` (its
  group broke a limit), ``fallback`` (its part of a merged answer did not parse,
  so it was re-sent alone) or ``single``. The performance profile counts savings
  from these receipts, never from estimates.

A merged request that fails for good (the outer retry gave up, or the response
body carried no answer) is not re-sent unit by unit: each unit it carried
records the execution error (D33), keeping its receipt. An answer that arrived
but does not parse, wholly or in part, makes only the affected units fall back
to single requests.

The executor knows nothing about HTTP. ``send(request)`` gets a
:class:`VlmRequest` and returns a :class:`VlmResponse` or an OpenAI-style body
(``{"choices": [{"message": {"content": ...}}], "usage": {...}}``), or raises;
the check process will pass an adapter over v1's ``hedged_request``. Around it
the executor adds the outer retry (:mod:`curation.planner.retry`) and the usage
ledgers (:mod:`curation.planner.usage`).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .merge import (MERGED_CALL_KIND, FramePolicy, MergeGroup, MergeLimits, MergeStrategy,
                    MergeUnit, NoMerge, PerEpisodeMultiModule, compose_merged_prompt,
                    split_answer, task_key)
from .retry import CallFailed, Failure, RetryPolicy, RetryStats, call_with_retry
from .usage import UsageLedger, parse_usage

RECEIPTS = ("merged", "split", "fallback", "single")


@dataclass(frozen=True)
class VlmRequest:
    """One request, transport-neutral: the text, the frames and what it carries."""

    episode_index: int                    # the (first) episode it is about
    call_kind: str                        # the unit's latency tag, or "merged"
    modules: tuple[str, ...]              # module of each task, in task order
    text: str                             # the whole prompt text
    images: tuple[Any, ...] = ()          # frames from the frames provider, opaque here
    frame_policy: FramePolicy | None = None
    keys: tuple[str, ...] = ()            # task_1 .. task_N of a merged request
    max_tokens: int | None = None         # output budget; None = the transport's default
    episodes: tuple[int, ...] = ()        # every episode whose frames it carries

    @property
    def merged(self) -> bool:
        return self.call_kind == MERGED_CALL_KIND


@dataclass(frozen=True)
class VlmResponse:
    content: str
    usage: Mapping[str, Any] | None = None
    #: Further HTTP requests behind this logical call that returned no usage
    #: (a lost hedge, an inner resend); counted, never estimated (01 §2.6).
    unknown_usage_requests: int = 0

    @classmethod
    def from_openai(cls, body: Mapping[str, Any]) -> "VlmResponse":
        content = body["choices"][0]["message"].get("content")
        return cls("" if content is None else str(content), body.get("usage"))


def as_response(value: Any) -> VlmResponse:
    if isinstance(value, VlmResponse):
        return value
    if isinstance(value, Mapping):
        return VlmResponse.from_openai(value)
    if isinstance(value, str):
        return VlmResponse(value)
    raise TypeError(f"send() returned {type(value).__name__}; expected a VlmResponse, "
                    "a chat-completions body or a string")


@dataclass
class UnitOutcome:
    unit: MergeUnit
    receipt: str                          # merged | split | fallback | single
    result: Any = None                    # what the module's parser returned
    error: dict[str, Any] | None = None   # an execution-error incident (D33)
    answer: str | None = None             # the text the parser got
    requests: int = 0                     # requests that carried this unit
    fallback_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class SentRequest:
    request: VlmRequest
    attempts: int
    ok: bool


@dataclass
class MergeRun:
    outcomes: list[UnitOutcome]
    sent: list[SentRequest] = field(default_factory=list)

    def by_key(self) -> dict[tuple[int, str, str], UnitOutcome]:
        return {o.unit.key: o for o in self.outcomes}

    def receipts(self) -> dict[str, dict[str, int]]:
        """Per module, the ``merge`` block of ``check --json`` (check.schema.json).

        ``requests`` counts the requests that carried the module's units; a merged
        request counts once in each module it carried, so the task's request total
        comes from the actual usage ledger, not from adding these up.
        """
        out: dict[str, dict[str, int]] = {}
        for o in self.outcomes:
            entry = out.setdefault(o.unit.module_id, _empty_receipt())
            if o.receipt in ("merged", "split", "fallback"):
                entry[f"{o.receipt}_units"] += 1
        for sent in self.sent:
            for module in set(sent.request.modules):
                out.setdefault(module, _empty_receipt())["requests"] += 1
        return out


def _empty_receipt() -> dict[str, int]:
    return {"requests": 0, "merged_units": 0, "split_units": 0, "fallback_units": 0}


class MergeReceipts:
    """Adds receipts up over the runs of one ``check`` part."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_module: dict[str, dict[str, int]] = {}

    def add(self, run: MergeRun) -> None:
        with self._lock:
            for module, counts in run.receipts().items():
                entry = self._by_module.setdefault(module, _empty_receipt())
                for name, value in counts.items():
                    entry[name] += value

    def for_module(self, module_id: str) -> dict[str, int]:
        with self._lock:
            return dict(self._by_module.get(module_id, _empty_receipt()))

    def to_json(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {m: dict(v) for m, v in sorted(self._by_module.items())}


class MergeExecutor:
    """Groups ready units, sends them, splits answers back, falls back unit by unit.

    ``strategy`` defaults to :class:`PerEpisodeMultiModule`; ``enabled=False`` is the
    kill switch (everything goes out alone). ``frames(episode_index, policy)``
    returns the images for one episode under one policy; it is called once per
    episode and policy in a run, and a merged request and its fallbacks reuse the
    result. ``retry`` is the outer retry (``None`` = no retry, the CLI default).
    ``on_attempt(call_kind, outcome)`` sees every try, ``ok`` or a failure cause,
    for the adaptive throttle.
    """

    def __init__(self, send: Callable[[VlmRequest], Any], *,
                 strategy: MergeStrategy | None = None,
                 limits: MergeLimits | None = None,
                 enabled: bool = True,
                 frames: Callable[[int, FramePolicy], Iterable[Any]] | None = None,
                 model: str = "",
                 ledger: UsageLedger | None = None,
                 retry: RetryPolicy | None = None,
                 retry_stats: RetryStats | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 on_attempt: Callable[[str, str], None] | None = None) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        self._send = send
        self.strategy = strategy or PerEpisodeMultiModule()
        self.limits = limits or MergeLimits()
        self.enabled = enabled
        self._frames = frames
        self.model = model
        self.ledger = ledger or UsageLedger()
        self.retry = retry or RetryPolicy(max_retries=0)
        self.retry_stats = retry_stats or RetryStats()
        self._sleep = sleep
        self._on_attempt = on_attempt

    # ------------------------------------------------------------------ public
    def run(self, units: Iterable[MergeUnit]) -> MergeRun:
        units = list(units)
        keys = [u.key for u in units]
        if len(set(keys)) != len(keys):
            raise ValueError("the same unit (episode, module, question) was passed twice")
        strategy = self.strategy if self.enabled else NoMerge()
        groups = strategy.group(units, self.limits)
        grouped = [u.key for g in groups for u in g.units]
        if sorted(grouped) != sorted(keys):
            raise RuntimeError(f"strategy {strategy.name} lost or duplicated units")
        state = _RunState()
        compose = getattr(strategy, "compose", None) or compose_merged_prompt
        for group in groups:
            if len(group.units) == 1:
                self._single(group.units[0], group.receipt, state)
            else:
                self._merged(group, state, compose)
        return MergeRun([state.outcomes[k] for k in keys], state.sent)

    # ------------------------------------------------------------------ sending
    def _images(self, episodes: Sequence[int], policy: FramePolicy,
                state: "_RunState") -> tuple[Any, ...]:
        if self._frames is None:
            return ()
        out: list[Any] = []
        for episode in episodes:
            key = (episode, policy)
            if key not in state.frames:
                state.frames[key] = tuple(self._frames(episode, policy))
            out.extend(state.frames[key])
        return tuple(out)

    def _transmit(self, request: VlmRequest, prompt_shares: Mapping[str, int],
                  state: "_RunState"):
        """Send with the outer retry; failed tries count as requests without usage."""
        unknown = {m: (w, 0) for m, w in prompt_shares.items()}

        def on_failure(failure: Failure, attempt: int) -> None:
            self.ledger.record(model=self.model, call_kind=request.call_kind, shares=unknown,
                               usage=None, seed=f"{_identity(request)}|try{attempt}")
            if self._on_attempt is not None:
                self._on_attempt(request.call_kind, failure.cause)

        def on_success(attempt: int) -> None:
            if self._on_attempt is not None:
                self._on_attempt(request.call_kind, "ok")

        attempts = {"n": 0}

        def once() -> VlmResponse:
            attempts["n"] += 1
            return as_response(self._send(request))

        try:
            response = call_with_retry(once, self.retry, sleep=self._sleep,
                                       stats=self.retry_stats, on_failure=on_failure,
                                       on_success=on_success)
        except CallFailed as failed:
            state.sent.append(SentRequest(request, failed.attempts, ok=False))
            return None, failed.incident(step=request.call_kind, call_kind=request.call_kind)
        state.sent.append(SentRequest(request, attempts["n"], ok=True))
        return response, None

    def _account(self, request: VlmRequest, shares: Mapping[str, tuple[int, int]],
                 response: VlmResponse) -> None:
        seed = _identity(request)
        self.ledger.record(model=self.model, call_kind=request.call_kind, shares=shares,
                           usage=parse_usage(response.usage), seed=seed)
        extra = response.unknown_usage_requests
        if extra:
            self.ledger.record(model=self.model, call_kind=request.call_kind,
                               shares={m: (w, 0) for m, (w, _) in shares.items()},
                               usage=None, count=int(extra), seed=f"{seed}|extra")

    def _single(self, unit: MergeUnit, receipt: str, state: "_RunState", *,
                carried_before: int = 0, fallback_reason: str | None = None) -> None:
        request = VlmRequest(unit.episode_index, unit.call_kind, (unit.module_id,),
                             unit.prompt_part,
                             self._images((unit.episode_index,), unit.frame_policy, state),
                             unit.frame_policy, (), unit.max_tokens, (unit.episode_index,))
        outcome = UnitOutcome(unit, receipt, requests=carried_before + 1,
                              fallback_reason=fallback_reason)
        state.outcomes[unit.key] = outcome
        response, incident = self._transmit(request, {unit.module_id: len(unit.prompt_part)},
                                            state)
        if incident is not None:
            outcome.error = {"kind": "execution", "incidents": [incident]}
            return
        self._account(request, {unit.module_id: (len(unit.prompt_part), len(response.content))},
                      response)
        outcome.answer = response.content
        try:
            outcome.result = unit.parser(response.content)
        except Exception as exc:  # noqa: BLE001 - a module's parser failing is an execution error
            outcome.error = {"kind": "execution", "incidents": [
                {"step": "parse", "call_kind": unit.call_kind,
                 "cause": f"{type(exc).__name__}: {exc}"}]}

    def _merged(self, group: MergeGroup, state: "_RunState",
                compose: Callable[[Sequence[MergeUnit], int | None], str]) -> None:
        units, policy = group.units, group.units[0].frame_policy
        images = self._images(group.episodes, policy, state)
        per_episode = policy.max_images()
        n_images = len(images) or (per_episode * len(group.episodes) if per_episode else None)
        keys = tuple(task_key(i) for i in range(1, len(units) + 1))
        request = VlmRequest(group.episodes[0], MERGED_CALL_KIND,
                             tuple(u.module_id for u in units), compose(units, n_images), images,
                             policy, keys, self.limits.output_budget(units), group.episodes)
        prompt_w: dict[str, int] = {}
        for unit in units:
            prompt_w[unit.module_id] = prompt_w.get(unit.module_id, 0) + len(unit.prompt_part)
        response, incident = self._transmit(request, prompt_w, state)
        if incident is not None:
            for unit in units:
                state.outcomes[unit.key] = UnitOutcome(
                    unit, group.receipt, requests=1,
                    error={"kind": "execution", "incidents": [incident]})
            return
        sections = split_answer(response.content, keys)
        whole_failed = sections is None
        sections = sections or {}
        completion_w: dict[str, int] = dict.fromkeys(prompt_w, 0)
        for key, unit in zip(keys, units):
            completion_w[unit.module_id] += len(sections.get(key, ""))
        self._account(request, {m: (prompt_w[m], completion_w[m]) for m in prompt_w}, response)

        fallback: list[tuple[MergeUnit, str]] = []
        for key, unit in zip(keys, units):
            if whole_failed:
                fallback.append((unit, "answer is not a JSON object"))
                continue
            if key not in sections:
                fallback.append((unit, f"no {key} in the answer"))
                continue
            try:
                result = unit.parser(sections[key])
            except Exception as exc:  # noqa: BLE001 - only this unit falls back
                fallback.append((unit, f"{key}: {type(exc).__name__}: {exc}"))
                continue
            state.outcomes[unit.key] = UnitOutcome(unit, group.receipt, result=result,
                                                   answer=sections[key], requests=1)
        for unit, reason in fallback:
            self._single(unit, "fallback", state, carried_before=1, fallback_reason=reason)


def _identity(request: VlmRequest) -> str:
    """A stable name for a request, the seed that credits its request count to a module."""
    episodes = ",".join(map(str, request.episodes or (request.episode_index,)))
    return f"{episodes}|{request.call_kind}|{','.join(request.modules)}"


class _RunState:
    def __init__(self) -> None:
        self.frames: dict[tuple[int, FramePolicy], tuple[Any, ...]] = {}
        self.outcomes: dict[tuple[int, str, str], UnitOutcome] = {}
        self.sent: list[SentRequest] = []


def merge_units_for(specs: Sequence[Any], episode_index: int,
                    context: Mapping[str, Any] | None = None) -> list[MergeUnit]:
    """The units the registry entries in ``specs`` declare for one episode."""
    out: list[MergeUnit] = []
    for spec in specs:
        if getattr(spec, "merge_units", None) is not None:
            out.extend(spec.merge_units(episode_index, context or {}))
    return out
