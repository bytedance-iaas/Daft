"""The verdict policy (design doc 17 §4, D58): findings -> blocking / review / info."""
from __future__ import annotations

import json

import pytest

from curation.contracts import modules as registry
from curation.pipeline import policy as P


def _f(code: str, item: str | None = None, severity: str = "high") -> dict:
    return {"code": code, "item": item, "severity": severity, "message_zh": "x"}


def test_the_default_policy_is_the_registry_default_levels():
    pol = P.Policy.of("default")
    for spec in registry.MODULES:
        for c in spec.codes:
            assert pol.level(spec.id, _f(c.code, c.item, c.severity)) == c.level, (spec.id, c.code)
    # P18: a retired soft score is reported only; today's gates block; suspects are asked
    assert pol.level("motion_quality", _f("smoothness_low", "ACT-1")) == "info"
    assert pol.level("timestamp_check", _f("gap", "STRM-3")) == "blocking"
    assert pol.level("data_integrity", _f("count_mismatch", "FILE-5")) == "review"
    assert pol.review_line("data_integrity", _f("count_mismatch")) == "integrity_check"
    assert pol.appealable("task_success", _f("failure")) and not pol.appealable("timestamp_check", _f("gap"))


def test_report_only_rejects_nothing_and_asks_nobody():
    pol = P.Policy.of("report_only")
    assert {pol.level(s.id, _f(c.code, c.item, c.severity)) for s in registry.MODULES for c in s.codes} == {"info"}


def test_rules_match_in_order_and_cannot_invent_a_question():
    pol = P.Policy.of("custom", [
        {"match": {"module": "motion_quality", "code": "spike"}, "level": "blocking"},
        {"match": {"item": "ACT-1"}, "level": "review"},          # smoothness_low has no review line
        {"match": {"severity": "low", "level": "review"}, "level": "info"},
    ])
    assert pol.level("motion_quality", _f("spike", "ACT-2")) == "blocking"
    assert pol.level("motion_quality", _f("smoothness_low", "ACT-1")) == "info"     # nobody to ask
    assert pol.level("data_integrity", _f("cut_off", "FILE-3", "low")) == "info"
    assert pol.level("data_integrity", _f("count_mismatch", "FILE-5", "medium")) == "review"
    assert pol.level("unknown_module", _f("x")) == "info"


def test_bad_policies_are_refused():
    with pytest.raises(P.PolicyError):
        P.Policy.of("no_such_preset")
    with pytest.raises(P.PolicyError):
        P.Policy.of("x", [{"match": {"colour": "red"}, "level": "info"}])
    with pytest.raises(P.PolicyError):
        P.Policy.of("x", [{"match": {}, "level": "fatal"}])


def test_frozen_in_the_run_directory(tmp_path):
    assert P.load(str(tmp_path)).preset == "default"                   # no run.json yet
    pol = P.from_task_params({"policy": {"preset": "report_only"}})
    (tmp_path / "run.json").write_text(json.dumps({"policy": pol.to_json()}))
    again = P.load(str(tmp_path))
    assert again == pol and again.to_json()["version"] == P.POLICY_VERSION
    assert P.from_task_params({}).preset == "default"
