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


def test_unchanged_adoption_skipped_no_rerun(tmp_path):
    """同一裁决(裁决时间+新标注未变)已落库:不再调 rerun,交付原样,记 unchanged。"""
    import json

    from curation.pipeline.rejudge import run_rejudge
    det = tmp_path / "details"
    det.mkdir()
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000001,采纳建议改标,new text,,2026-08-06 10:00:00\n", encoding="utf-8")
    (tmp_path / "passed.json").write_text(json.dumps({"episodes": {"ep000001": {
        "判决": "通过(标注修正后)",
        "标注修正": {"裁决时间": "2026-08-06 10:00:00", "新标注": "new text"}}}},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "review.json").write_text('{"episodes": {}}', encoding="utf-8")
    (tmp_path / "reject.json").write_text('{"episodes": {}}', encoding="utf-8")

    calls = []
    summary = run_rejudge(str(tmp_path), "/unused", {},
                          rerun_fn=lambda *a: calls.append(a))
    assert summary["unchanged"] == ["ep000001"] and calls == []
    got = json.loads((tmp_path / "passed.json").read_text(encoding="utf-8"))
    assert got["episodes"]["ep000001"]["判决"] == "通过(标注修正后)"


def test_changed_adoption_reruns_in_parallel(tmp_path):
    """裁决时间变了(改判)→ 重判照跑;多条采纳并行执行且结果不串号。"""
    import json

    from curation.pipeline.rejudge import run_rejudge
    det = tmp_path / "details"
    det.mkdir()
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000001,采纳建议改标,label A,,2026-08-06 11:00:00\n"
        "ep000002,采纳建议改标,label B,,2026-08-06 11:00:05\n", encoding="utf-8")
    for n in ("passed", "review", "reject"):
        (tmp_path / f"{n}.json").write_text('{"episodes": {}}', encoding="utf-8")

    def fake_rerun(input_dir, eid, new_label):
        return {"passed": True, "verdict": f"success:{new_label}"}

    summary = run_rejudge(str(tmp_path), "/unused", {}, rerun_fn=fake_rerun)
    assert set(summary["adopted_pass"]) == {"ep000001", "ep000002"}
    got = json.loads((tmp_path / "passed.json").read_text(encoding="utf-8"))
    assert got["episodes"]["ep000001"]["标注修正"]["重判判定"] == "success:label A"
    assert got["episodes"]["ep000002"]["标注修正"]["重判判定"] == "success:label B"


def test_duplicate_entry_purged_across_files(tmp_path):
    """已裁决条目在 review 里的无溯源残留 → 定向清掉;**未裁决**的弃权条目
    双呈现(passed+review)是交付格式设计,必须原样保留(2026-08-06 全局清扫
    误清 14 条的教训:只许定向,不许通杀)。"""
    import json

    from curation.pipeline.rejudge import run_rejudge
    det = tmp_path / "details"
    det.mkdir()
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000001,采纳建议改标,fixed label,,2026-08-06 10:00:00\n", encoding="utf-8")
    (tmp_path / "passed.json").write_text(json.dumps({"episodes": {"ep000001": {
        "判决": "通过(标注修正后)",
        "标注修正": {"裁决时间": "2026-08-06 10:00:00", "新标注": "fixed label"}}}},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "review.json").write_text(json.dumps({
        "待人工裁决总数": 2,
        "episodes": {"ep000001": {"当前判决": "通过", "待裁决项": ["任务成败判定"]},
                     "ep000009": {"当前判决": "通过", "待裁决项": ["任务成败判定"]}}},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reject.json").write_text('{"episodes": {}}', encoding="utf-8")

    summary = run_rejudge(str(tmp_path), "/unused", {},
                          rerun_fn=lambda *a: (_ for _ in ()).throw(AssertionError("不应重判")))
    assert summary["unchanged"] == ["ep000001"]
    rev = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert "ep000001" not in rev["episodes"], "review 里的僵尸副本未被清扫"
    assert "ep000009" in rev["episodes"], "未裁决的正常待裁决条目被误清(设计上双呈现)"
    assert rev["待人工裁决总数"] == 1
    psd = json.loads((tmp_path / "passed.json").read_text(encoding="utf-8"))
    assert psd["episodes"]["ep000001"]["判决"] == "通过(标注修正后)"


def test_export_parquet_synced_on_decisions(tmp_path):
    """出数据闭环:采纳改标 → parquet 的 instruction 换新+溯源标「人工裁决改标」;
    弃用 → 整行剔除(裁决只改报告不改数据 = 交出去还是脏数据,2026-08-06 堵上)。"""
    daft = __import__("pytest").importorskip("daft")
    import json

    from curation.pipeline.rejudge import run_rejudge
    det = tmp_path / "details"
    det.mkdir()
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000001,采纳建议改标,corrected label,,2026-08-06 12:00:00\n"
        "ep000002,弃用该条,,,2026-08-06 12:00:05\n", encoding="utf-8")
    for n in ("passed", "review", "reject"):
        (tmp_path / f"{n}.json").write_text(json.dumps({"episodes": {
            "ep000001": {"判决": "通过"}, "ep000002": {"判决": "通过"},
            "ep000003": {"判决": "通过"}}} if n == "passed" else {"episodes": {}},
            ensure_ascii=False), encoding="utf-8")
    daft.from_pydict({
        "episode_id": ["ep000001", "ep000002", "ep000003"],
        "instruction": ["old label", "whatever", "untouched"],
        "instruction_source": ["原始标注", "原始标注", "原始标注"],
    }).write_parquet(str(tmp_path / "episodes_parquet"))

    run_rejudge(str(tmp_path), "/unused", {},
                rerun_fn=lambda i, e, nl: {"passed": True, "verdict": "success"})
    got = daft.read_parquet(str(tmp_path / "episodes_parquet")).to_pydict()
    by = dict(zip(got["episode_id"], zip(got["instruction"], got["instruction_source"])))
    assert "ep000002" not in by, "弃用条目仍在交付数据里"
    assert by["ep000001"] == ("corrected label", "人工裁决改标")
    assert by["ep000003"] == ("untouched", "原始标注")


def test_v2_lerobot_curated_reexported_on_decisions(tmp_path):
    """v2 源的 LeRobot 包在裁决后自动重导出(纯拷贝,便宜):弃用条目消失、
    改标条目的任务表换新;v3 才留"重跑 run"的提醒。"""
    pytest = __import__("pytest")
    pytest.importorskip("pandas")
    daft = pytest.importorskip("daft")
    import json

    from curation.pipeline.rejudge import run_rejudge
    from curation.tests.test_lerobot_v2_export import _write_v2_dataset
    src = tmp_path / "src_ds"
    _write_v2_dataset(str(src))                       # 4 条最小 v2 数据集
    delivery = tmp_path / "dlv"
    det = delivery / "details"
    det.mkdir(parents=True)
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000001,采纳建议改标,corrected task text,,2026-08-06 13:00:00\n"
        "ep000002,弃用该条,,,2026-08-06 13:00:05\n", encoding="utf-8")
    for n in ("passed", "review", "reject"):
        (delivery / f"{n}.json").write_text(json.dumps({"episodes": {
            f"ep{i:06d}": {"判决": "通过"} for i in range(4)}}
            if n == "passed" else {"episodes": {}}, ensure_ascii=False),
            encoding="utf-8")
    daft.from_pydict({
        "episode_id": [f"ep{i:06d}" for i in range(4)],
        "instruction": ["a", "b", "c", "d"],
        "instruction_source": ["原始标注"] * 4,
    }).write_parquet(str(delivery / "episodes_parquet"))
    # 预置一个假的旧 lerobot_curated(重导出应整体替换它)
    (delivery / "lerobot_curated").mkdir()
    (delivery / "lerobot_curated" / "stale").write_text("old")

    run_rejudge(str(delivery), str(src), {},
                rerun_fn=lambda i, e, nl: {"passed": True, "verdict": "success"})
    out = delivery / "lerobot_curated"
    assert not (out / "stale").exists(), "旧包未被替换"
    eps = [json.loads(l) for l in
           (out / "meta" / "episodes.jsonl").read_text().splitlines()]
    assert len(eps) == 3, "弃用条目仍在 LeRobot 包里"
    tasks = (out / "meta" / "tasks.jsonl").read_text()
    assert "corrected task text" in tasks, "改标未进任务表"
