"""rejudge 闭环(③)测试:apply_decisions 纯函数逐格 + run_rejudge 注入假重判器冒烟。"""
from __future__ import annotations

import csv
import json
import os

import pytest

from curation.pipeline.rejudge import (apply_decisions, apply_task_verdicts,
                                       run_rejudge)


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


# ───────── 任务成败人工裁决(2026-08-06):不重判,人说了算 ─────────


def _abstain_views():
    """弃权条目的真实形态:**同时**出现在 passed(当前判决=通过,带 checks)与
    review(待裁决视图)——这是交付格式的设计,不是残留。"""
    passed = {"episodes": {
        "epX": {"判决": "通过", "综合软分": 0.8,
                "checks": {"任务成败判定": {"结果": "弃权",
                                            "detail": '{"voc": 0.5}'}}},
        "epY": {"判决": "通过", "综合软分": 0.7,
                "checks": {"任务成败判定": {"结果": "弃权"}}},
        "epZ": {"判决": "通过", "综合软分": 0.6,
                "checks": {"任务成败判定": {"结果": "弃权"}}}}}
    review = {"待人工裁决总数": 3, "episodes": {
        e: {"当前判决": "通过", "待裁决项": ["任务成败判定"],
            "弃权原因": {"任务成败判定": "渐变问询不可判"}}
        for e in ("epX", "epY", "epZ")}}
    reject = {"被拒总数": 0, "episodes": {}}
    return passed, review, reject


def test_verdict_pass_returns_to_delivery_with_provenance():
    """判成功:出待裁决队列、判决改「通过(人工裁决)」、成败检查落 pass、挂溯源;
    弃权原因不许留着(它说的是"系统判不了",现在有人判了)。"""
    p, r, j = _abstain_views()
    s = apply_task_verdicts(p, r, j, {"epX": {"verdict": "判成功", "note": "看了视频",
                                              "at": "t1"}})
    assert s["verdict_pass"] == ["epX"]
    assert "epX" not in r["episodes"] and "epX" not in j["episodes"]
    e = p["episodes"]["epX"]
    assert e["判决"] == "通过(人工裁决)" and e["综合软分"] == 0.8
    assert e["checks"]["任务成败判定"]["结果"] == "pass"
    assert e["checks"]["任务成败判定"]["detail"] == '{"voc": 0.5}'   # VLM 读数原样留着
    assert e["人工裁决"] == {"裁决": "判成功", "备注": "看了视频", "裁决时间": "t1"}
    assert "弃权原因" not in e and "待裁决项" not in e
    assert r["待人工裁决总数"] == 2                                   # 计数同步


def test_verdict_fail_moves_to_reject_and_is_taken_from_all_views():
    """判失败:三边全摘 → 只在 reject;原因写清是人工裁决,溯源在案。"""
    p, r, j = _abstain_views()
    s = apply_task_verdicts(p, r, j, {"epY": {"verdict": "判失败", "note": "没抓起来",
                                              "at": "t2"}})
    assert s["verdict_fail"] == ["epY"]
    assert "epY" not in p["episodes"] and "epY" not in r["episodes"]
    e = j["episodes"]["epY"]
    assert e["判决"] == "拒绝" and e["原因"] == "人工裁决判失败(任务未完成)"
    assert e["checks"]["任务成败判定"]["结果"] == "拒绝"
    assert e["人工裁决"]["裁决"] == "判失败" and e["人工裁决"]["裁决时间"] == "t2"
    assert j["被拒总数"] == 1 and r["待人工裁决总数"] == 2


def test_verdict_hold_keeps_item_in_queue():
    """搁置:只记一笔,条目**留在队列里**(它是"待定"不是结论)。"""
    p, r, j = _abstain_views()
    s = apply_task_verdicts(p, r, j, {"epZ": {"verdict": "搁置", "note": "看不清",
                                              "at": "t3"}})
    assert s["verdict_hold"] == ["epZ"]
    assert "epZ" in r["episodes"] and "epZ" in p["episodes"]      # 双呈现照旧
    assert r["episodes"]["epZ"]["人工裁决"]["裁决"] == "搁置"
    assert r["episodes"]["epZ"]["待裁决项"] == ["任务成败判定"]
    assert r["待人工裁决总数"] == 3                                # 一条都没走


def test_verdict_unknown_word_or_missing_episode_is_skipped():
    """裁决词不识别 / 交付里根本没这条 → 记 skipped,绝不凭空造条目。"""
    p, r, j = _abstain_views()
    s = apply_task_verdicts(p, r, j, {"epX": {"verdict": "大概成功吧", "at": "t"},
                                      "ep不存在": {"verdict": "判成功", "at": "t"}})
    assert set(s["verdict_skipped"]) == {"epX", "ep不存在"}
    assert "ep不存在" not in p["episodes"] and "ep不存在" not in j["episodes"]
    assert p["episodes"]["epX"]["判决"] == "通过"                  # 原样不动


def test_two_decision_lines_do_not_overwrite_each_others_provenance():
    """同一条 episode 先被改标重判、后被人工判成败:两个溯源键并存,谁也不抹谁。"""
    p, r, j = _views()
    apply_decisions(p, r, j,
                    {"epB": {"decision": "采纳建议改标", "new_label": "x", "at": "t1"}},
                    {"epB": {"passed": None, "verdict": "重判仍弃权", "detail": "{}"}})
    assert "epB" in r["episodes"]                                  # 重判后仍弃权
    apply_task_verdicts(p, r, j, {"epB": {"verdict": "判成功", "at": "t2"}})
    e = p["episodes"]["epB"]
    assert e["标注修正"]["新标注"] == "x" and e["人工裁决"]["裁决"] == "判成功"
    assert e["checks"]["任务成败判定"]["结果"] == "pass"


def _write_verdict_delivery(tmp_path, rows, views=None):
    """交付三件套 + details/task_verdicts.csv 的最小 fixture。"""
    (tmp_path / "details").mkdir(exist_ok=True)
    p, r, j = views or _abstain_views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
    with open(tmp_path / "details" / "task_verdicts.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "verdict", "note", "at"])
        w.writeheader()
        w.writerows(rows)
    return tmp_path


def test_run_rejudge_consumes_task_verdicts_without_vlm(tmp_path):
    """端到端:只有成败裁决(没有标注裁决)也要照跑,且**一次 VLM 都不许调**。"""
    d = _write_verdict_delivery(tmp_path, [
        {"episode_id": "epX", "verdict": "判成功", "note": "", "at": "t1"},
        {"episode_id": "epY", "verdict": "判失败", "note": "", "at": "t2"},
        {"episode_id": "epZ", "verdict": "搁置", "note": "", "at": "t3"}])
    (d / "report.md").write_text("# 报告\n", encoding="utf-8")

    def _boom(*a):
        raise AssertionError("成败裁决不该触发重判")

    s = run_rejudge(str(d), "/unused", {}, rerun_fn=_boom)
    assert s["verdict_pass"] == ["epX"] and s["verdict_fail"] == ["epY"]
    assert s["verdict_hold"] == ["epZ"]
    psd = json.loads((d / "passed.json").read_text(encoding="utf-8"))
    rev = json.loads((d / "review.json").read_text(encoding="utf-8"))
    rej = json.loads((d / "reject.json").read_text(encoding="utf-8"))
    assert psd["episodes"]["epX"]["判决"] == "通过(人工裁决)"
    assert set(rev["episodes"]) == {"epZ"} and rev["待人工裁决总数"] == 1
    assert rej["episodes"]["epY"]["原因"].startswith("人工裁决判失败")
    md = (d / "report.md").read_text(encoding="utf-8")
    assert "任务成败人工裁决" in md and "剩余待人工裁决:1 条" in md
    res = json.loads((d / "details" / "rejudge_results.json").read_text(encoding="utf-8"))
    assert res["task_verdicts"] == {"epX": "判成功", "epY": "判失败", "epZ": "搁置"}


def test_run_rejudge_task_verdict_is_idempotent(tmp_path):
    """同一条裁决(裁决词+裁决时间没变)已落库 → 第二趟跳过,记 unchanged。"""
    d = _write_verdict_delivery(tmp_path, [
        {"episode_id": "epX", "verdict": "判成功", "note": "", "at": "t1"}])
    first = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert first["verdict_pass"] == ["epX"]
    second = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert second["unchanged"] == ["epX"] and second["verdict_pass"] == []
    psd = json.loads((d / "passed.json").read_text(encoding="utf-8"))
    assert psd["episodes"]["epX"]["判决"] == "通过(人工裁决)"
    # 改判(裁决时间变了)→ 重新应用
    with open(d / "details" / "task_verdicts.csv", "a", newline="",
              encoding="utf-8") as f:
        csv.writer(f).writerow(["epX", "判失败", "", "t9"])
    third = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert third["verdict_fail"] == ["epX"]
    rej = json.loads((d / "reject.json").read_text(encoding="utf-8"))
    assert "epX" in rej["episodes"]


def test_verdict_fail_drops_row_from_delivered_parquet(tmp_path):
    """出数据闭环:人工判失败的条目必须从 episodes_parquet 里剔除——
    只改报告不改数据,交出去还是脏数据。"""
    daft = __import__("pytest").importorskip("daft")
    d = _write_verdict_delivery(tmp_path, [
        {"episode_id": "epY", "verdict": "判失败", "note": "", "at": "t2"},
        {"episode_id": "epX", "verdict": "判成功", "note": "", "at": "t1"}])
    daft.from_pydict({
        "episode_id": ["epX", "epY", "epZ"],
        "instruction": ["a", "b", "c"],
        "instruction_source": ["原始标注"] * 3,
    }).write_parquet(str(d / "episodes_parquet"))

    run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    got = daft.read_parquet(str(d / "episodes_parquet")).to_pydict()
    assert "epY" not in got["episode_id"], "人工判失败的条目仍在交付数据里"
    assert set(got["episode_id"]) == {"epX", "epZ"}
    # 判成功的条目标注不许被动过(成败裁决不改 instruction)
    by = dict(zip(got["episode_id"], got["instruction"]))
    assert by["epX"] == "a"
