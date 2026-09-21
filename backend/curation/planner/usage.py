"""Token usage: parse the model's ``usage`` and keep two ledgers (04 §5, 01 §2.6).

* **actual**: one entry per request. A merged request is one entry with
  ``call_kind = merged``; its module is the ``+``-joined ids of the modules it
  carried. Task totals are sums over this ledger only.
* **attributed**: the same requests split over modules, to answer "which module
  spends the most". A merged request's ``prompt_tokens`` and ``cached_tokens``
  are split by each module's share of the prompt characters, its
  ``completion_tokens`` and ``reasoning_tokens`` by each module's share of the
  answer text (largest remainder, exact integers). Its request counts cannot be
  split: each is credited to one module, drawn in proportion to the prompt share
  from a hash of the request (same result on every run, fair over many). So for
  every call kind and model the attributed ledger sums exactly to the actual one.
  The two are never added together.

A request that returned no ``usage`` (timeout, lost hedge, interrupted) is never
estimated: it only counts in ``requests_unknown_usage``, and ``requests`` counts
the requests whose usage is known.

Every entry leaves the CLI as a C3 ``usage`` line on stderr; lines are
increments and the Daemon adds them up per (subtask, module, call kind, model,
ledger) (``docs/contracts/progress.schema.json``).
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .merge import MERGED_CALL_KIND, SINGLE_CALL_KINDS, check_single_call_kind

LEDGERS = ("actual", "attributed")
#: The kinds v1 knows plus ``merged``; a new module may book under its own kind (C3 1.1).
USAGE_CALL_KINDS = SINGLE_CALL_KINDS + (MERGED_CALL_KIND,)
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens")
COUNT_FIELDS = ("requests", "requests_unknown_usage")
FIELDS = COUNT_FIELDS + TOKEN_FIELDS
#: Which weight splits which field of a merged request.
PROMPT_WEIGHTED = ("requests", "requests_unknown_usage", "prompt_tokens", "cached_tokens")
COMPLETION_WEIGHTED = ("completion_tokens", "reasoning_tokens")


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def parse_usage(usage: Any) -> Usage | None:
    """An OpenAI-compatible ``usage`` object -> :class:`Usage`, or ``None`` if unusable.

    ``prompt_tokens`` and ``completion_tokens`` must be non-negative integers;
    ``completion_tokens_details.reasoning_tokens`` and
    ``prompt_tokens_details.cached_tokens`` count as 0 when absent or null.
    """
    if not isinstance(usage, Mapping):
        return None
    prompt, completion = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
    if prompt is None or completion is None:
        return None
    ctd, ptd = usage.get("completion_tokens_details"), usage.get("prompt_tokens_details")
    reasoning = _count(ctd.get("reasoning_tokens")) if isinstance(ctd, Mapping) else None
    cached = _count(ptd.get("cached_tokens")) if isinstance(ptd, Mapping) else None
    return Usage(prompt, completion, reasoning or 0, cached or 0)


def usage_from_response(payload: Any) -> Usage | None:
    """``usage`` of a whole chat-completions response body."""
    return parse_usage(payload.get("usage")) if isinstance(payload, Mapping) else None


def apportion(total: int, weights: Sequence[int]) -> list[int]:
    """Split ``total`` in proportion to ``weights`` into integers that sum to it exactly.

    Largest remainder: everyone gets the floor of their exact share, the units
    left over go to the largest remainders, ties to the earlier weight. All-zero
    weights split evenly. Deterministic for the same input.
    """
    if not weights:
        raise ValueError("no weights to apportion over")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError(f"total must be a non-negative integer, got {total!r}")
    if any(isinstance(w, bool) or not isinstance(w, int) or w < 0 for w in weights):
        raise ValueError(f"weights must be non-negative integers, got {list(weights)!r}")
    weights = list(weights) if sum(weights) else [1] * len(weights)
    whole = sum(weights)
    shares = [total * w // whole for w in weights]
    remainders = [total * w % whole for w in weights]
    for i in sorted(range(len(weights)), key=lambda i: (-remainders[i], i))[:total - sum(shares)]:
        shares[i] += 1
    return shares


def draw(total: int, weights: Sequence[int], seed: str) -> list[int]:
    """Give each of ``total`` whole units to one weight, drawn in proportion to the weights.

    Used for request counts: one merged request cannot be split, so it is credited
    to one module. The draw is a hash of ``seed`` (the request's identity), so it
    is the same on every run and in any order, and fair over many requests.
    """
    if not weights or total < 0 or any(w < 0 for w in weights):
        raise ValueError("draw() needs weights and non-negative numbers")
    weights = list(weights) if sum(weights) else [1] * len(weights)
    whole, out = sum(weights), [0] * len(weights)
    for k in range(total):
        point = int.from_bytes(hashlib.sha256(f"{seed}#{k}".encode()).digest()[:8], "big") % whole
        for i, w in enumerate(weights):
            point -= w
            if point < 0:
                out[i] += 1
                break
    return out


def merged_module_label(modules: Sequence[str]) -> str:
    """The module an actual-ledger entry is filed under: the id, or ``a+b`` when merged."""
    return "+".join(sorted(set(modules)))


def _key(line: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (line.get("ledger", "actual"), line["module"], line["call_kind"], line["model"])


class UsageAccumulator:
    """Adds up C3 ``usage`` lines per (ledger, module, call kind, model).

    This is the Daemon's side of the protocol, and the ledger's own memory.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str, str, str], dict[str, int]] = {}

    def add(self, line: Mapping[str, Any]) -> None:
        if line.get("kind", "usage") != "usage":
            raise ValueError(f"not a usage line: {line!r}")
        key = _key(line)
        if key[0] not in LEDGERS:
            raise ValueError(f"unknown ledger {key[0]!r}")
        with self._lock:
            bucket = self._buckets.setdefault(key, dict.fromkeys(FIELDS, 0))
            for name in FIELDS:
                bucket[name] += int(line.get(name, 0) or 0)

    def rows(self, ledger: str = "actual") -> list[dict[str, Any]]:
        with self._lock:
            return [{"ledger": lg, "module": m, "call_kind": ck, "model": mo, **dict(v)}
                    for (lg, m, ck, mo), v in sorted(self._buckets.items()) if lg == ledger]

    def totals(self, ledger: str = "actual", **match: str) -> dict[str, int]:
        """Sum of one ledger, optionally only rows whose ``module`` / ``call_kind`` / ``model`` match."""
        out = dict.fromkeys(FIELDS, 0)
        for row in self.rows(ledger):
            if all(row[k] == v for k, v in match.items()):
                for name in FIELDS:
                    out[name] += row[name]
        return out

    def task_totals(self) -> dict[str, int]:
        """What a task reports as its token use: the actual ledger, never both (01 §2.6)."""
        return self.totals("actual")

    def balanced(self) -> bool:
        """The attributed ledger sums to the actual one for every call kind and model."""
        pairs = {(r["call_kind"], r["model"]) for lg in LEDGERS for r in self.rows(lg)}
        return all(self.totals("actual", call_kind=ck, model=mo)
                   == self.totals("attributed", call_kind=ck, model=mo) for ck, mo in pairs)


class UsageLedger:
    """Turns each request's usage into C3 lines for both ledgers and keeps the sums.

    ``emit`` receives every line as it is made (the CLI writes it to stderr; with
    several threads recording, ``emit`` must write whole lines atomically);
    ``clock`` returns epoch seconds. Thread-safe.
    """

    def __init__(self, *, emit: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._emit = emit
        self._clock = clock
        self.accumulator = UsageAccumulator()

    def record(self, *, model: str, call_kind: str,
               shares: Mapping[str, tuple[int, int]], usage: Usage | None,
               count: int = 1, seed: str | None = None) -> list[dict[str, Any]]:
        """Account ``count`` requests of one shape; returns the lines made.

        ``shares`` maps each module the request carried to its (prompt weight,
        completion weight); a single request carries one module. ``usage=None``
        means the request came back without usage (``count`` may then be more than
        one, for example failed attempts); known usage belongs to exactly one request.
        ``seed`` identifies the request: when given, the request counts of a merged
        request are credited by :func:`draw` instead of by the largest remainder.
        """
        if not isinstance(model, str):
            raise ValueError("model must be a string")
        if call_kind != MERGED_CALL_KIND:
            check_single_call_kind(call_kind)
        modules = sorted(shares)
        if not modules or any(not isinstance(m, str) or not m for m in modules):
            raise ValueError("a request carries at least one named module")
        if call_kind != MERGED_CALL_KIND and len(modules) != 1:
            raise ValueError(f"a single {call_kind} request carries one module, got {modules}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        if usage is not None and count != 1:
            raise ValueError("known usage belongs to exactly one request")

        if usage is None:
            actual = {"requests": 0, "requests_unknown_usage": count, **dict.fromkeys(TOKEN_FIELDS, 0)}
        else:
            actual = {"requests": 1, "requests_unknown_usage": 0,
                      **{name: getattr(usage, name) for name in TOKEN_FIELDS}}
        ts = int(self._clock() * 1000)

        def line(ledger: str, module: str, values: Mapping[str, int]) -> dict[str, Any]:
            return {"ts": ts, "kind": "usage", "ledger": ledger, "model": model,
                    "module": module, "call_kind": call_kind, **{k: values[k] for k in FIELDS}}

        lines = [line("actual", merged_module_label(modules), actual)]
        if len(modules) == 1:
            lines.append(line("attributed", modules[0], actual))
        else:
            prompt_w = [int(shares[m][0]) for m in modules]
            completion_w = [int(shares[m][1]) for m in modules]
            if not any(completion_w):
                completion_w = prompt_w           # no answer segments: fall back to the prompt share
            split = {name: apportion(actual[name], prompt_w) for name in PROMPT_WEIGHTED}
            split.update({name: apportion(actual[name], completion_w) for name in COMPLETION_WEIGHTED})
            if seed is not None:
                for name in COUNT_FIELDS:
                    split[name] = draw(actual[name], prompt_w, f"{seed}|{name}")
            for i, module in enumerate(modules):
                lines.append(line("attributed", module, {k: split[k][i] for k in FIELDS}))
        for item in lines:
            self.accumulator.add(item)
            if self._emit is not None:
                self._emit(item)
        return lines

    def record_single(self, *, module: str, call_kind: str, model: str,
                      response: Any = None, usage: Usage | None = None) -> list[dict[str, Any]]:
        """One non-merged request; pass the response body or an already parsed usage."""
        if usage is None and response is not None:
            usage = usage_from_response(response)
        return self.record(model=model, call_kind=call_kind, shares={module: (1, 1)}, usage=usage)

    # read side, delegated to the accumulator
    def rows(self, ledger: str = "actual") -> list[dict[str, Any]]:
        return self.accumulator.rows(ledger)

    def totals(self, ledger: str = "actual", **match: str) -> dict[str, int]:
        return self.accumulator.totals(ledger, **match)

    def task_totals(self) -> dict[str, int]:
        return self.accumulator.task_totals()

    def balanced(self) -> bool:
        return self.accumulator.balanced()
