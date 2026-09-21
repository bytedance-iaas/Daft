"""Merge vs single-request consistency on the example modules (doc 10, section 3.4)."""
from __future__ import annotations

import pytest

from curation.core.contract import CheckResult
from curation.planner import (MERGE_AGREEMENT_BAR, MergeLimits, default_verdict,
                              run_merge_consistency, verdict_agreement)

from . import examples as X

EPISODES = range(64)          # the batch size doc 10 suggests


def test_merge_matches_single_on_64_episodes():
    """Acceptance: agreement >= 98 % (a deterministic model gives 100 %), with fallbacks on the way."""
    fake = X.FakeVlm(garble={(5, "table"), (17, "grasp")}, drop={(33, "table")}, not_json={48})
    report, single, merged = run_merge_consistency(X.units_for(EPISODES), fake, frames=X.frames,
                                                   model="fake-vlm")
    assert report.total == 128 and report.rate == 1.0 and report.passes()
    assert MERGE_AGREEMENT_BAR == 0.98
    receipts = [o.receipt for o in merged.outcomes]
    assert receipts.count("fallback") == 5 and receipts.count("merged") == 123
    assert len([r for r in fake.requests if r.call_kind == "merged"]) == 64
    assert {o.receipt for o in single.outcomes} == {"single"}
    assert report.to_json()["passes"] is True


def test_consistency_holds_when_groups_are_split():
    report, _, merged = run_merge_consistency(X.units_for(range(8)), X.FakeVlm(), frames=X.frames,
                                              limits=MergeLimits(max_units=1))
    assert report.rate == 1.0 and {o.receipt for o in merged.outcomes} == {"split"}


@pytest.mark.parametrize("flips,passes", [(0, True), (2, True), (3, False)])
def test_the_bar_catches_a_model_that_answers_differently_when_merged(flips, passes):
    flipped = [(ep, "grasp") for ep in EPISODES
               if X.FakeVlm.truth(ep, "grasp") in ("yes", "no")][:flips]
    report, _, _ = run_merge_consistency(X.units_for(EPISODES), X.FakeVlm(flip=flipped),
                                         frames=X.frames)
    assert report.total - report.agree == flips
    assert report.passes() is passes                              # 126/128 = 98.4 %, 125/128 = 97.7 %
    expected = []
    for ep, _ in flipped:
        alone = "pass" if X.FakeVlm.truth(ep, "grasp") == "yes" else "fail"
        expected.append(((ep, "example_grasp", ""), alone, "fail" if alone == "pass" else "pass"))
    assert report.disagreements == expected


def test_errors_count_as_disagreement():
    # the single run goes first and meets the one 429 (no outer retry by default)
    fake = X.FakeVlm(transient={4: 1})
    report, single, merged = run_merge_consistency(X.units_for([3, 4]), fake, frames=X.frames)
    assert [o.unit.key for o in single.outcomes if not o.ok] == [(4, "example_grasp", "")]
    assert all(o.ok for o in merged.outcomes)
    assert report.disagreements[0][:2] == ((4, "example_grasp", ""), "error")


def test_verdicts_and_agreement_helpers():
    assert default_verdict(CheckResult("x", passed=True)) == "pass"
    assert default_verdict(CheckResult("x", passed=False)) == "fail"
    assert default_verdict(CheckResult("x", passed=None)) == "abstain"
    assert default_verdict(CheckResult("x", passed=None, score=0.4)) == "scored"
    assert default_verdict({"passed": None, "score": None}) == "abstain"
    report = verdict_agreement({1: "pass", 2: "fail"}, {1: "pass", 3: "fail"})
    assert (report.total, report.agree) == (3, 1) and not report.passes()
    assert verdict_agreement({}, {}).rate == 1.0
