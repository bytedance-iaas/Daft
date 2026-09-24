"""The EEF module's question on the adjudication page (C1 1.9 ``eef_check``, F5.11).

The main task's story with the EEF gate added in revision 2: ep 0 is an episode the module sent
to a person (a card of its own), ep 4 carries it next to the label conflict, ep 8 is a reject of
the module alone (an appeal). Answers are checked against the catalog, counted as pending,
executed by the CLI and change the lists of the next revision.
"""
from __future__ import annotations

import pytest

from .conftest import MIN, MODULES, T0, assert_error, assert_schema
from .test_adjudication import _cards, _ok, _page

EEF = "eef_video_consistency"
WITH = (*MODULES, EEF)
WHY = "「位置」（相机 ext）CPU 判为可疑，模型多数认为一致（支持 2、反对 1）"


@pytest.fixture
def eef_world(world):
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
    assert [q["line"] for q in cards[4]["questions"]] == ["label", "eef_check"]
    appeals = _cards(w, tab="appeals", status="all")
    assert appeals[8]["questions"][0]["source_module"] == EEF
    assert appeals[8]["questions"][0]["reason"].startswith("未通过「EEF–视频一致性」")
    view = w.get("/episodes/0").json()
    assert_schema("EpisodeView", view)
    assert view["review"][0] == {"module": EEF, "kind": "eef_consistency", "text": WHY}
    assert view["modules"][EEF]["details"]["decision"]["outcome"] == "human"
    assert before["pending"] == 4                            # 3, 4, 5 of the story and 0
    assert_error(w.decide((0, "eef_check", "success")), "validation_failed")
    assert_error(w.decide((3, "eef_check", "consistent")), "validation_failed")     # not asked there
    counts = _ok(w.decide((0, "eef_check", "consistent"), (4, "eef_check", "inconsistent"),
                          (4, "label", "keep_label")))
    assert counts["pending"] == 2 and counts["unapplied"] == 2


def test_the_answers_are_executed_and_change_the_lists(eef_world):
    w = eef_world
    _ok(w.decide((0, "eef_check", "consistent"), (4, "eef_check", "inconsistent"), (4, "label", "keep_label"),
                 (8, "reject_appeal", "restore")))
    sub = w.start_subtask(at=T0 + 10 * MIN)
    out = w.apply(sub)
    assert out["rerun_task_success"] == [] and out["profile_resync"] == [0, 4, 8]
    w.revision(3, subtask_id=sub.id, modules=WITH)
    w.finish_subtask(sub, at=T0 + 11 * MIN)
    w.switch(3)
    assert [w.get(f"/episodes/{ep}").json()["list"] for ep in (0, 4, 8)] == ["passed", "reject", "passed"]
    assert w.get("/episodes/4").json()["reasons"] == [
        {"module": EEF, "kind": "human", "text": "人工裁决判为 EEF 与视频不一致"}]
    cards = _cards(w, status="all")
    assert cards[0]["status"] == "applied" and cards[4]["status"] == "applied"
    assert _cards(w, tab="appeals", status="all")[8]["status"] == "applied"


def test_an_unsure_answer_keeps_the_card_asked(eef_world):
    w = eef_world
    _ok(w.decide((0, "eef_check", "unsure")))
    assert _cards(w)[0]["status"] == "unsure"
    assert _page(w)["counts"]["pending"] == 4 and _page(w)["counts"]["unapplied"] == 0
