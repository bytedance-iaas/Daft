"""判废护栏 ↔ 标注-画面分歧清单打通(2026-09-03):一把尺子、一条 caption、一份清单。

① 比对器改走打标审计的单对判官(PAIR_JUDGE_PROMPT):same→同一任务,其余从严;
② 护栏拦下的条目并进分歧清单 high 档,顶掉画像段对同一条的重打重判;
③ 仲裁工厂 make_intent_comparer 现在是①的薄封装(签名/默认超时不变)。
"""
from __future__ import annotations

import json

import pytest

from curation.dataset_level.audit import (
    GUARD_REASON_PREFIX,
    guard_hold_entries,
    judge_pair,
    make_pair_comparer,
    merge_guard_holds,
)


def _llm(verdict, why="w"):
    def ask(prompt):
        assert "ANNOTATION:" in prompt and "DESCRIPTION:" in prompt
        return json.dumps({"pairs": [{"i": 0, "verdict": verdict, "why": why}]})
    return ask


def test_judge_pair_roundtrip():
    assert judge_pair("put x", "place x", _llm("same", "synonym")) == ("same", "synonym")


@pytest.mark.parametrize("verdict,expect", [("same", True), ("different", False),
                                            ("unsure", False), ("garbage", False)])
def test_pair_comparer_maps_strictly(verdict, expect):
    assert make_pair_comparer(_llm(verdict))("put x", "y") is expect


def test_pair_comparer_raises_on_bad_structure():
    same = make_pair_comparer(lambda p: "not json")
    with pytest.raises(Exception):
        same("a", "b")


def test_intent_comparer_factory_wraps_pair_judge(monkeypatch):
    """仲裁工厂不再自带短提示:走 make_llm_ask → 单对判官;闸门透传。"""
    import curation.adapters.vlm_client as vc
    seen = {}

    def fake_llm_ask(endpoint, model, **kw):
        seen.update(kw)
        return _llm("different")
    monkeypatch.setattr(vc, "make_llm_ask", fake_llm_ask)
    same = vc.make_intent_comparer("http://x", "m", timeout_s=7.0, gate="G")
    assert same("a", "b") is False
    assert seen["timeout_s"] == 7.0 and seen["gate"] == "G"


def _detail(verdict, *, rules=(), label_check=None, arbitration=None):
    d = {"verdict": verdict, "rules": list(rules)}
    if label_check:
        d["label_check"] = label_check
    if arbitration:
        d["arbitration"] = arbitration
    return d


def test_guard_hold_entries_from_both_layers():
    td = {
        "ep000029": _detail("label_conflict_suspect",
                            rules=["kill_held_label_conflict",
                                   "arbitration_kill_held_label_conflict"],
                            label_check={"annotation": "Put the orange thing in the can",
                                         "caption": "pour rice", "outcome": "different"},
                            arbitration={"intent": "Put the orange thing in the can",
                                         "caption": "pour rice"}),
        "ep000030": _detail("label_conflict_suspect", rules=["kill_held_label_conflict"],
                            label_check={"annotation": "hang jacket", "caption": "fold shirt",
                                         "outcome": "different"}),
        "ep000031": _detail("arbitration_failure", rules=["arbitration_kill_double_signed"]),
        "ep000032": _detail("success"),
    }
    out = guard_hold_entries(td)
    assert [e["id"] for e in out] == ["ep000029", "ep000030"]
    assert out[0]["guard_layer"] == "仲裁层" and out[1]["guard_layer"] == "复核层"
    assert out[0]["label"].startswith("Put the orange") and out[0]["caption"] == "pour rice"
    assert all(e["reason"].startswith(GUARD_REASON_PREFIX) for e in out)
    assert guard_hold_entries({}) == []


def test_merge_guard_holds_overrides_profile_stage_entry():
    audit = {"high": [{"id": "ep000045", "label": "a", "caption": "b", "reason": "r"},
                      {"id": "ep000029", "label": "x", "caption": "画像段重打的", "reason": "r"}],
             "mid_for_review": [{"id": "ep000030", "label": "h", "caption": "c", "reason": "r"}],
             "low_caption_unstable": []}
    holds = guard_hold_entries({
        "ep000029": _detail("label_conflict_suspect",
                            label_check={"annotation": "x", "caption": "护栏那条",
                                         "outcome": "different"}),
        "ep000030": _detail("label_conflict_suspect",
                            label_check={"annotation": "h", "caption": "护栏那条2",
                                         "outcome": "different"})})
    m = merge_guard_holds(audit, holds)
    assert [e["id"] for e in m["high"]] == ["ep000029", "ep000030", "ep000045"]
    assert m["high"][0]["caption"] == "护栏那条"          # 顶掉画像段那条
    assert m["mid_for_review"] == []                        # 同 id 的中档条目剔除
    assert audit["high"][1]["caption"] == "画像段重打的"     # 不改入参


def test_merge_guard_holds_edge_cases():
    assert merge_guard_holds(None, []) is None
    audit = {"high": [], "mid_for_review": []}
    assert merge_guard_holds(audit, []) is audit
    holds = [{"id": "ep000001", "label": "l", "caption": "c", "reason": "r"}]
    m = merge_guard_holds(None, holds)                      # --only task_success:画像段没跑
    assert m["high"] == holds and m["mid_for_review"] == [] and m["low_caption_unstable"] == []
