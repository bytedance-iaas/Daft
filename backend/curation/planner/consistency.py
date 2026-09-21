"""Merge vs single-request consistency check (design doc 10, section 3.4).

A module may be merged only after this passes on its own data: the same
episodes, the same model, no reasoning parameter; A = every unit sent alone,
B = merged; at least 98 % of the verdicts agree and the disagreements are
looked at by a person for systematic bias. Failing it means switching merging
off (``vlm.merge.enabled = false``): merging is an optimisation, not a feature.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterable, Mapping

from .executor import MergeExecutor, MergeRun, VlmRequest
from .merge import FramePolicy, MergeLimits, MergeStrategy, MergeUnit

MERGE_AGREEMENT_BAR = 0.98


def default_verdict(result: Any) -> str:
    """pass / fail / abstain / scored from a result with ``passed`` and ``score``.

    Works for v1's ``CheckResult`` and for plain mappings; the same rule as the
    result-record contract.
    """
    get = result.get if isinstance(result, Mapping) else (lambda k: getattr(result, k, None))
    passed, score = get("passed"), get("score")
    if passed is True:
        return "pass"
    if passed is False:
        return "fail"
    return "scored" if score is not None else "abstain"


@dataclass
class AgreementReport:
    total: int
    agree: int
    disagreements: list[tuple[Hashable, str | None, str | None]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.agree / self.total if self.total else 1.0

    def passes(self, bar: float = MERGE_AGREEMENT_BAR) -> bool:
        return self.rate >= bar

    def to_json(self) -> dict[str, Any]:
        return {"total": self.total, "agree": self.agree, "rate": round(self.rate, 4),
                "bar": MERGE_AGREEMENT_BAR, "passes": self.passes(),
                "disagreements": [{"key": list(k) if isinstance(k, tuple) else k,
                                   "single": a, "merged": b} for k, a, b in self.disagreements]}


def verdict_agreement(single: Mapping[Hashable, str], merged: Mapping[Hashable, str]) -> AgreementReport:
    """Compare two verdict maps; a key present on one side only is a disagreement."""
    keys = sorted(set(single) | set(merged), key=repr)
    disagreements = [(k, single.get(k), merged.get(k)) for k in keys
                     if single.get(k) != merged.get(k)]
    return AgreementReport(len(keys), len(keys) - len(disagreements), disagreements)


def run_verdicts(run: MergeRun, verdict_of: Callable[[Any], str] = default_verdict) -> dict:
    return {o.unit.key: ("error" if o.error else verdict_of(o.result)) for o in run.outcomes}


def run_merge_consistency(units: Iterable[MergeUnit], send: Callable[[VlmRequest], Any], *,
                          verdict_of: Callable[[Any], str] = default_verdict,
                          frames: Callable[[int, FramePolicy], Iterable[Any]] | None = None,
                          limits: MergeLimits | None = None,
                          strategy: MergeStrategy | None = None,
                          model: str = "") -> tuple[AgreementReport, MergeRun, MergeRun]:
    """Send the same units alone and merged; returns the report and both runs."""
    units = list(units)
    common = dict(frames=frames, limits=limits, model=model)
    single = MergeExecutor(send, enabled=False, **common).run(units)
    merged = MergeExecutor(send, strategy=strategy, enabled=True, **common).run(units)
    return verdict_agreement(run_verdicts(single, verdict_of),
                             run_verdicts(merged, verdict_of)), single, merged
