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


def test_report_only_asks_nobody_and_rejects_only_on_data_integrity():
    pol = P.Policy.of("report_only")
    levels = {(s.id, c.code): pol.level(s.id, _f(c.code, c.item, c.severity)) for s in registry.MODULES for c in s.codes}
    # an empty, cut or unreadable file still rejects (requester 2026-10-05): nothing after it can use it
    integrity = next(s for s in registry.MODULES if s.id == "data_integrity")
    for c in integrity.codes:
        assert levels[("data_integrity", c.code)] == ("blocking" if c.level == "blocking" else "info"), c.code
    assert levels[("data_integrity", "file_empty")] == "blocking"
    # everything else is reported only, and nobody is asked
    assert {lv for (m, _), lv in levels.items() if m != "data_integrity"} == {"info"}
    assert "review" not in levels.values()


def test_a_frozen_version_1_report_only_keeps_its_own_rules():
    # a task started before 2026-10-05 froze report_only as one rule: everything info
    old = P.Policy.from_json({"preset": "report_only", "version": "1", "rules": [{"match": {"level": "any"}, "level": "info"}]})
    assert old.level("data_integrity", _f("file_empty", "FILE-1", "high")) == "info"
    assert P.Policy.of("report_only").to_json()["version"] == "2"


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


def test_a_task_keeps_the_default_levels_it_started_with(tmp_path):
    """Design doc 25 §7.3: 5.0 made the EEF module's ``inconsistent`` info; a task frozen with its modules' default
    levels keeps them whatever a later registry says, and a run frozen before 5.0 (no defaults in run.json) keeps
    4.x's - an older task's next revision does not quietly bring its EEF rejects back."""
    eef = {"code": "inconsistent", "item": "MV-4", "severity": "high"}
    now = P.Policy.of().with_defaults(["eef_video_consistency", "dedup"])
    table = now.to_json()["defaults"]
    assert table["eef_video_consistency"]["inconsistent"] == "info" and table["eef_video_consistency"]["conflict"] == "review"
    assert set(table) == {"eef_video_consistency", "dedup"} and now.level("eef_video_consistency", eef) == "info"
    # what was frozen wins over the registry, even where they differ
    frozen = {**now.to_json(), "defaults": {"eef_video_consistency": {"inconsistent": "blocking"}}}
    assert P.Policy.from_json(frozen, "5.0").level("eef_video_consistency", eef) == "blocking"
    # a run of 4.x without frozen defaults: the default it had
    old = {"preset": "default", "rules": [], "version": "2"}
    for ver, want in (("4.4", "blocking"), ("4.0", "blocking"), ("5.0", "info"), (None, "info")):
        assert P.Policy.from_json(old, ver).level("eef_video_consistency", eef) == want, ver
    (tmp_path / "run.json").write_text(json.dumps({"policy": old, "registry_version": "4.2"}))
    assert P.load(str(tmp_path)).level("eef_video_consistency", eef) == "blocking"
    # rules still decide first: report_only makes it info even on an old run
    ro = P.Policy.from_json({"preset": "report_only", "rules": P.PRESETS["report_only"], "version": "2"}, "4.2")
    assert ro.level("eef_video_consistency", eef) == "info"
