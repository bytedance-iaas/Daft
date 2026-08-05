"""rejudge 闭环(③)测试:apply_decisions 纯函数逐格 + run_rejudge 注入假重判器冒烟。"""
from __future__ import annotations

import csv
import json
import os

import pytest

from curation.pipeline.rejudge import apply_decisions, run_rejudge


def _views():
    passed = {"episodes": {"epA": {"判决": "通过", "综合软分": 0.9,
                                   "checks": {"任务成败判定": {"结果": "pass"}}}}}
    review = {"待人工裁决总数": 2,
              "episodes": {"epB": {"当前判决": "通过", "待裁决项": ["任务成败判定"]},
                           "epC": {"当前判决": "通过", "待裁决项": ["任务成败判定"]}},
              "标注-画面分歧复核队列": [
                  {"id": "epB", "label": "old-b", "caption": "cap-b"},
                  {"id": "epC", "label": "old-c", "caption": "cap-c"},
                  {"id": "epA", "label": "old-a", "caption": "cap-a"}]}
    reject = {"被拒总数": 0, "episodes": {}}
    return passed, review, reject


def test_adopt_pass_moves_to_passed_with_provenance():
    p, r, j = _views()
    s = apply_decisions(p, r, j,
                        {"epB": {"decision": "采纳建议改标", "new_label": "cap-b",
                                 "note": "n", "at": "t"}},
                        {"epB": {"passed": True, "verdict": "success", "detail": "{}"}})
    assert s["adopted_pass"] == ["epB"] and "epB" not in r["episodes"]
    e = p["episodes"]["epB"]
    assert e["判决"] == "通过(标注修正后)"
    assert e["标注修正"]["新标注"] == "cap-b" and e["标注修正"]["重判判定"] == "success"
    assert e["checks"]["任务成败判定"]["结果"] == "pass"
    assert r["待人工裁决总数"] == 1                       # 计数同步
    q = [x for x in r["标注-画面分歧复核队列"] if x["id"] == "epB"][0]
    assert q["decision"] == "采纳建议改标"


def test_adopt_fail_and_abstain_routes():
    p, r, j = _views()
    s = apply_decisions(p, r, j,
                        {"epB": {"decision": "采纳建议改标", "new_label": "x", "at": "t"},
                         "epC": {"decision": "采纳建议改标", "new_label": "y", "at": "t"}},
                        {"epB": {"passed": False, "verdict": "failure", "detail": "{}"},
                         "epC": {"passed": None, "verdict": "review_conflict", "detail": "{}"}})
    assert s["adopted_reject"] == ["epB"] and j["episodes"]["epB"]["判决"] == "拒绝"
    assert s["adopted_review"] == ["epC"] and "epC" in r["episodes"]
    assert j["被拒总数"] == 1


def test_drop_and_keep_semantics():
    p, r, j = _views()
    s = apply_decisions(p, r, j,
                        {"epA": {"decision": "弃用该条", "note": "坏", "at": "t"},
                         "epC": {"decision": "维持原标注", "at": "t"}}, {})
    assert s["dropped"] == ["epA"] and "epA" not in p["episodes"]
    assert j["episodes"]["epA"]["原因"].startswith("人工裁决弃用")
    assert s["kept"] == ["epC"] and "epC" in r["episodes"]   # 维持=只标记不搬移
    q = [x for x in r["标注-画面分歧复核队列"] if x["id"] == "epC"][0]
    assert q["decision"] == "维持原标注"


def test_adopt_without_rejudge_result_is_skipped_not_guessed():
    p, r, j = _views()
    s = apply_decisions(p, r, j,
                        {"epB": {"decision": "采纳建议改标", "new_label": "x", "at": "t"}},
                        {})                                   # 重判没跑成
    assert s["skipped"] == ["epB"] and "epB" in r["episodes"]  # 原样不动,绝不臆断


def test_run_rejudge_smoke_with_fake_rerun(tmp_path):
    """端到端冒烟(假重判器注入):裁决文件 → 三件套更新 + 报告追加 + 留档。"""
    d = tmp_path / "delivery"
    (d / "details").mkdir(parents=True)
    p, r, j = _views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (d / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False))
    (d / "report.md").write_text("# 报告\n")
    with open(d / "details" / "label_decisions.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "decision", "new_label", "note", "at"])
        w.writeheader()
        w.writerow({"episode_id": "epB", "decision": "采纳建议改标",
                    "new_label": "cap-b", "note": "", "at": "t"})

    fake = lambda inp, eid, lab: {"passed": True, "verdict": "success", "detail": "{}"}
    s = run_rejudge(str(d), "/nonexistent", {}, rerun_fn=fake)
    assert s["adopted_pass"] == ["epB"]
    out = json.loads((d / "passed.json").read_text())
    assert out["episodes"]["epB"]["标注修正"]["新标注"] == "cap-b"
    assert "标注裁决与重判" in (d / "report.md").read_text()
    assert os.path.exists(d / "details" / "rejudge_results.json")


def test_run_rejudge_no_decisions_is_noop(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    s = run_rejudge(str(d), "/x", {})
    assert "未做任何事" in s["note"]
