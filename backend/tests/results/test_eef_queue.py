"""The EEF module's question on the adjudication page (C1 1.9 ``eef_check``, F5.11; design doc 25 §7.3).

The main task's story with the EEF gate added in revision 2, as a task started under registry 4.4 (its
run.json says so: 4.x's default levels, D81's legacy table): ep 0 is an episode the module sent to a person
(a card of its own), ep 4 carries it next to the task verdict, ep 8 is a reject of the module alone (an
appeal). Answers are checked against the catalog, counted as pending, executed by the CLI and change the
lists of the next revision. From 5.0 on a conflict between the channels is the only card
(``test_a_conflict_is_the_card_and_its_answer_acts``).
"""
from __future__ import annotations

import json

import pytest

from .conftest import MIN, MODULES, T0, assert_error, assert_schema
from .test_adjudication import _cards, _ok, _page

EEF = "eef_video_consistency"
WITH = (*MODULES, EEF)
WHY = "「位置」（相机 ext）CPU 判为可疑，模型多数认为一致（支持 2、反对 1）"


def _started_under(world, version: str) -> None:
    (world.run_dir / "run.json").write_text(json.dumps({"policy": {"preset": "default", "rules": [], "version": "2"},
                                                        "registry_version": version}))


@pytest.fixture
def eef_world(world):
    _started_under(world, "4.4")
    rd = world.rd
    for ep in (0, 2, 3, 4, 5, 6, 7, 8):                    # every episode that reaches the vlm stage
        verdict = {0: "abstain", 4: "abstain", 8: "fail"}.get(ep, "pass")
        reason = {"abstain": "需要人工裁决：" + WHY, "fail": "「位置」（相机 ext）CPU 与模型都认为不一致",
                  "pass": ""}[verdict]
        rd.put(EEF, ep, verdict, details={"reason": reason, "assessment_mode": "verdict",
                                          "decision": {"outcome": {"abstain": "human", "fail": "reject",
                                                                   "pass": "pass"}[verdict]}})
    rd.write_parts()
    world.revision(2, modules=WITH)
    world.switch(2)
    return world


def test_what_the_module_could_not_settle_is_a_pending_card(eef_world):
    w = eef_world
    before = _page(w)["counts"]
    cards = _cards(w, status="all")
    assert [q["line"] for q in cards[0]["questions"]] == ["eef_check"]
    q = cards[0]["questions"][0]
    assert (q["source_module"], q["reason"], q["latest_decision"]) == (EEF, WHY, None)
    assert [q["line"] for q in cards[4]["questions"]] == ["task_verdict", "eef_check"]
    appeals = _cards(w, tab="appeals", status="all")
    assert appeals[8]["questions"][0]["source_module"] == EEF
    assert appeals[8]["questions"][0]["reason"].startswith("「位置」")       # the finding's sentence
    view = w.get("/episodes/0").json()
    assert_schema("EpisodeView", view)
    assert view["review"][0] == {"module": EEF, "kind": "eef_consistency", "text": WHY}
    assert view["modules"][EEF]["details"]["decision"]["outcome"] == "human"
    assert before["pending"] == 4                            # 3, 4, 5 of the story and 0
    assert_error(w.decide((0, "eef_check", "success")), "validation_failed")
    assert_error(w.decide((3, "eef_check", "consistent")), "validation_failed")     # not asked there
    counts = _ok(w.decide((0, "eef_check", "consistent"), (4, "eef_check", "inconsistent"),
                          (4, "task_verdict", "success")))
    assert counts["pending"] == 2 and counts["unapplied"] == 2      # cards, not answers


def test_the_answers_are_executed_and_change_the_lists(eef_world):
    w = eef_world
    _ok(w.decide((0, "eef_check", "consistent"), (4, "eef_check", "inconsistent"),
                 (8, "reject_appeal", "restore")))
    sub = w.start_subtask(at=T0 + 10 * MIN)
    out = w.apply(sub)
    assert out["rerun_task_success"] == []
    w.revision(3, subtask_id=sub.id, modules=WITH)
    w.finish_subtask(sub, at=T0 + 11 * MIN)
    w.switch(3)
    assert [w.get(f"/episodes/{ep}").json()["list"] for ep in (0, 4, 8)] == ["passed", "reject", "passed"]
    assert w.get("/episodes/4").json()["reasons"] == [
        {"module": EEF, "kind": "human", "code": "unsettled", "item": "MV-4", "appealable": False,
         "text": "人工裁决判为 EEF 与视频不一致"}]
    cards = _cards(w, status="all")
    assert cards[0]["status"] == "applied" and cards[4]["status"] == "applied"
    assert _cards(w, tab="appeals", status="all")[8]["status"] == "applied"


def test_an_unsure_answer_keeps_the_card_asked(eef_world):
    w = eef_world
    _ok(w.decide((0, "eef_check", "unsure")))
    assert _cards(w)[0]["status"] == "unsure"
    assert _page(w)["counts"]["pending"] == 4 and _page(w)["counts"]["unapplied"] == 0


@pytest.fixture
def conflict_world(world):
    """Registry 5.0: ep 0 a conflict between the CPU and the model, ep 8 an opinion "inconsistent" (no card)."""
    _started_under(world, "5.0")
    rd = world.rd
    for ep in (0, 2, 3, 4, 5, 6, 7, 8):
        cell = {"subitem": "position_2d", "camera": "ext", "sources": {"cpu": {"verdict": "issue", "p": 0.95}}}
        if ep == 0:
            cell.update(p=0.95, label="inconsistent", flags=["conflict"])
            cell["sources"]["vlm_review"] = {"verdict": "ok", "p": 0.1}
        elif ep == 8:
            cell.update(p=0.8, label="inconsistent", flags=["single_source"], missing="model_no_answer")
        else:
            cell.update(p=0.05, label="consistent", flags=[], sources={"cpu": {"verdict": "ok", "p": 0.05}})
        merged = {"cells": [cell], "episode": {"label": cell["label"], "p": cell["p"], "subitem": "position_2d",
                                               "camera": "ext", "flags": cell["flags"], "reason": "x",
                                               "conflicts": int("conflict" in cell["flags"])}}
        rd.put(EEF, ep, "pass", details={"assessment_mode": "verdict", "merged": merged, "reason": "x"})
    rd.write_parts()
    world.revision(2, modules=WITH)
    world.switch(2)
    return world


def test_a_conflict_is_the_card_and_its_answer_acts(conflict_world):
    w = conflict_world
    cards = _cards(w, status="all")
    assert [q["line"] for q in cards[0]["questions"]] == ["eef_check"] and 8 not in cards
    assert 8 not in _cards(w, tab="appeals", status="all")          # an opinion rejects nothing
    assert w.get("/episodes/8").json()["list"] == "passed"
    _ok(w.decide((0, "eef_check", "inconsistent")))
    sub = w.start_subtask(at=T0 + 10 * MIN)
    w.apply(sub)
    w.revision(3, subtask_id=sub.id, modules=WITH)
    w.finish_subtask(sub, at=T0 + 11 * MIN)
    w.switch(3)
    assert w.get("/episodes/0").json()["list"] == "reject"
    assert w.get("/episodes/0").json()["reasons"] == [
        {"module": EEF, "kind": "human", "code": "conflict", "item": "MV-4", "appealable": False,
         "text": "人工裁决判为 EEF 与视频不一致"}]
