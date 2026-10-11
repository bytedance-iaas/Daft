"""The EEF module's question on the adjudication page (C1 ``eef_check``, design doc 25 §7.3, D81).

The main task's story with the EEF module added in revision 2: its opinions reject nothing, and a conflict
between its two channels is its only card - ep 0 a card of its own, ep 4 carrying it next to the task verdict;
ep 8 is an opinion "inconsistent" (no card, no reject). Answers are checked against the catalog, counted as
pending, executed by the CLI and change the lists of the next revision.
"""
from __future__ import annotations

import pytest

from .conftest import MIN, MODULES, T0, assert_error, assert_schema
from .test_adjudication import _cards, _ok, _page

EEF = "eef_video_consistency"
WITH = (*MODULES, EEF)


@pytest.fixture
def eef_world(world):
    rd = world.rd
    for ep in (0, 2, 3, 4, 5, 6, 7, 8):
        cell = {"subitem": "position_2d", "camera": "ext", "sources": {"cpu": {"verdict": "issue", "p": 0.95}}}
        if ep in (0, 4):
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


def test_a_conflict_is_a_pending_card(eef_world):
    w = eef_world
    before = _page(w)["counts"]
    cards = _cards(w, status="all")
    assert [q["line"] for q in cards[0]["questions"]] == ["eef_check"] and 8 not in cards
    q = cards[0]["questions"][0]
    assert q["source_module"] == EEF and q["reason"] and q["latest_decision"] is None
    assert (q["codes"], q["items"]) == (["conflict"], ["MV-4"])
    assert [q["line"] for q in cards[4]["questions"]] == ["task_verdict", "eef_check"]
    assert 8 not in _cards(w, tab="appeals", status="all")          # an opinion rejects nothing
    assert w.get("/episodes/8").json()["list"] == "passed"
    view = w.get("/episodes/0").json()
    assert_schema("EpisodeView", view)
    assert [(i["module"], i["kind"]) for i in view["review"]] == [(EEF, "eef_consistency")]
    assert before["pending"] == 4                            # 3, 4, 5 of the story and 0
    assert_error(w.decide((0, "eef_check", "success")), "validation_failed")
    assert_error(w.decide((3, "eef_check", "consistent")), "validation_failed")     # not asked there
    counts = _ok(w.decide((0, "eef_check", "consistent"), (4, "eef_check", "inconsistent"),
                          (4, "task_verdict", "success")))
    assert counts["pending"] == 2 and counts["unapplied"] == 2      # cards, not answers


def test_the_answers_are_executed_and_change_the_lists(eef_world):
    w = eef_world
    _ok(w.decide((0, "eef_check", "consistent"), (4, "eef_check", "inconsistent")))
    sub = w.start_subtask(at=T0 + 10 * MIN)
    out = w.apply(sub)
    assert out["rerun_task_success"] == []
    w.revision(3, subtask_id=sub.id, modules=WITH)
    w.finish_subtask(sub, at=T0 + 11 * MIN)
    w.switch(3)
    assert [w.get(f"/episodes/{ep}").json()["list"] for ep in (0, 4, 8)] == ["passed", "reject", "passed"]
    assert w.get("/episodes/4").json()["reasons"] == [
        {"module": EEF, "kind": "human", "code": "conflict", "item": "MV-4", "appealable": False,
         "text": "人工裁决判为 EEF 与视频不一致"}]
    cards = _cards(w, status="all")
    assert cards[0]["status"] == "applied" and cards[4]["status"] == "applied"   # 4 is rejected: nothing more to ask
    assert 4 not in _cards(w, tab="appeals", status="all")           # a person's conclusion is final


def test_an_unsure_answer_keeps_the_card_asked(eef_world):
    w = eef_world
    _ok(w.decide((0, "eef_check", "unsure")))
    assert _cards(w)[0]["status"] == "unsure"
    assert _page(w)["counts"]["pending"] == 4 and _page(w)["counts"]["unapplied"] == 0
