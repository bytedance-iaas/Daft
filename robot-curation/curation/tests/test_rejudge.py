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
    改标条目的任务表换新;v3 同样自动重导出(视频重编码),见下一条用例。"""
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
    """拿不准:只记一笔,条目**留在队列里**(它是"待定"不是结论)。
    ⚠️ 输入故意用老值「搁置」:2026-08-19 改名后老交付的 CSV 里还是它,
    读不动等于把人已经做过的判断悄悄抹掉。存下来的应是现行词。"""
    p, r, j = _abstain_views()
    s = apply_task_verdicts(p, r, j, {"epZ": {"verdict": "搁置", "note": "看不清",
                                              "at": "t3"}})
    assert s["verdict_hold"] == ["epZ"]
    assert "epZ" in r["episodes"] and "epZ" in p["episodes"]      # 双呈现照旧
    assert r["episodes"]["epZ"]["人工裁决"]["裁决"] == "拿不准", \
        "老值「搁置」没被归一成现行词"
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
    assert res["task_verdicts"] == {"epX": "判成功", "epY": "判失败",
                                    "epZ": "拿不准"}


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


# ───────── 改标 + 人工成败结论 → 跳过 VLM 重判(2026-08-16 有意例外)─────────
#
# 铁律「改标必须重判」防的是机器自产自证(caption 改标 → 机器自己确认自己),
# 不是防人:用户在合并卡片上采纳改标的同时给了成败结论,他正看着视频,他就是
# ground truth —— 让机器去复核人刚给的结论,是把断路器装反了。以下测试钉三件事:
# ① 有人工结论就一次 VLM 都不许调;② 溯源两处都写人工(标注修正 + 人工裁决),
# 绝不伪装成机器判的;③ 只改标、没给结论(含搁置)→ 完全维持今天的重判行为。


def _write_both_lines_delivery(tmp_path, label_rows, verdict_rows, views=None):
    """标注裁决 + 成败裁决两张 CSV 同时在场的最小交付 fixture。"""
    d = _write_verdict_delivery(tmp_path, verdict_rows, views=views)
    with open(d / "details" / "label_decisions.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "decision", "new_label",
                                          "note", "at"])
        w.writeheader()
        w.writerows(label_rows)
    return d


def test_apply_decisions_human_concluded_writes_relabel_prov_in_place():
    """纯函数层:human_concluded 的采纳改标不搬移、不要求 rejudged 结果,只在
    现存条目上落「标注修正」溯源(写明重判被跳过);搬移由 apply_task_verdicts
    按人的结论做,两个溯源键并存。"""
    p, r, j = _views()
    s = apply_decisions(p, r, j,
                        {"epB": {"decision": "采纳建议改标", "new_label": "fixed",
                                 "at": "t1"}},
                        {}, human_concluded={"epB"})
    assert s["adopted_human"] == ["epB"] and s["skipped"] == []
    e = r["episodes"]["epB"]                      # 没搬移:还在 review 原地
    prov = e["标注修正"]
    assert prov["新标注"] == "fixed" and prov["裁决时间"] == "t1"
    assert "跳过" in prov["重判"]                  # 明写重判被跳过
    assert "重判判定" not in prov, "伪装成机器判的了"
    s2 = apply_task_verdicts(p, r, j, {"epB": {"verdict": "判成功", "at": "t2"}})
    assert s2["verdict_pass"] == ["epB"]
    e2 = p["episodes"]["epB"]
    assert e2["判决"] == "通过(人工裁决)"
    assert e2["标注修正"]["新标注"] == "fixed"     # 改标溯源随搬移带走
    assert e2["人工裁决"]["裁决"] == "判成功"      # 成败溯源 = 人工,两处都在


def test_run_rejudge_adopt_with_human_verdict_never_calls_vlm(tmp_path):
    """端到端:改标 + 人工判成功 → 重判函数一次都不许被调;交付条目同时带
    「标注修正」(重判=跳过)与「人工裁决」两处人工溯源。"""
    d = _write_both_lines_delivery(
        tmp_path,
        [{"episode_id": "epX", "decision": "采纳建议改标",
          "new_label": "put the cup in the sink", "note": "", "at": "t1"}],
        [{"episode_id": "epX", "verdict": "判成功", "note": "亲眼看的", "at": "t2"}])
    (d / "report.md").write_text("# 报告\n", encoding="utf-8")
    calls = []
    s = run_rejudge(str(d), "/unused", {},
                    rerun_fn=lambda *a: calls.append(a))
    assert calls == [], "人工已给成败结论,VLM 重判仍被调用了"
    assert s["adopted_human"] == ["epX"] and s["verdict_pass"] == ["epX"]
    psd = json.loads((d / "passed.json").read_text(encoding="utf-8"))
    e = psd["episodes"]["epX"]
    assert e["判决"] == "通过(人工裁决)"
    assert e["标注修正"]["新标注"] == "put the cup in the sink"
    assert "跳过" in e["标注修正"]["重判"] and "重判判定" not in e["标注修正"]
    assert e["人工裁决"]["裁决"] == "判成功"
    assert "epX" not in json.loads((d / "review.json").read_text(encoding="utf-8"))["episodes"]
    assert "跳过重判" in (d / "report.md").read_text(encoding="utf-8")


def test_run_rejudge_adopt_with_human_fail_goes_to_reject_without_vlm(tmp_path):
    """改标 + 人工判失败:同样不调 VLM,条目进 reject 且两处人工溯源都在 ——
    改标履历不许因为判失败被抹掉(交付里要看得出这条的标注被人动过)。"""
    d = _write_both_lines_delivery(
        tmp_path,
        [{"episode_id": "epY", "decision": "采纳建议改标",
          "new_label": "wipe the table", "note": "", "at": "t1"}],
        [{"episode_id": "epY", "verdict": "判失败", "note": "", "at": "t2"}])

    def _boom(*a):
        raise AssertionError("人工已给成败结论,不该触发重判")

    s = run_rejudge(str(d), "/unused", {}, rerun_fn=_boom)
    assert s["adopted_human"] == ["epY"] and s["verdict_fail"] == ["epY"]
    rej = json.loads((d / "reject.json").read_text(encoding="utf-8"))
    e = rej["episodes"]["epY"]
    assert e["原因"].startswith("人工裁决判失败")
    assert e["标注修正"]["新标注"] == "wipe the table"
    assert e["人工裁决"]["裁决"] == "判失败"


def test_run_rejudge_adopt_with_hold_still_reruns(tmp_path):
    """拿不准是「待定」不是结论:改标 + 拿不准 → **完全维持今天的行为**,照旧调
    VLM 按新标注重判(这条防的是把例外扩大化 —— 例外只认人真给了结论)。"""
    d = _write_both_lines_delivery(
        tmp_path,
        [{"episode_id": "epZ", "decision": "采纳建议改标",
          "new_label": "fold the towel", "note": "", "at": "t1"}],
        [{"episode_id": "epZ", "verdict": "搁置", "note": "", "at": "t2"}])
    calls = []

    def fake(inp, eid, lab):
        calls.append((eid, lab))
        return {"passed": True, "verdict": "success", "detail": "{}"}

    s = run_rejudge(str(d), "/unused", {}, rerun_fn=fake)
    assert calls == [("epZ", "fold the towel")], "拿不准不是结论,重判不该被跳过"
    assert s["adopted_pass"] == ["epZ"] and s["adopted_human"] == []


def test_run_rejudge_closing_reports_second_round(tmp_path):
    """收尾报账:改标重判后仍判不出的条目掉回「待你裁决」队列,rejudge 结束时
    必须明说条数 —— 不说,用户下次打开界面才自己发现还有第二轮。"""
    det = tmp_path / "details"
    det.mkdir()
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "epB,采纳建议改标,new text,,t1\n", encoding="utf-8")
    p, r, j = _views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
    fake = lambda inp, eid, lab: {"passed": None, "verdict": "重判仍弃权",
                                  "detail": "{}"}
    s = run_rejudge(str(tmp_path), "/unused", {}, rerun_fn=fake)
    assert s["adopted_review"] == ["epB"]
    assert "本轮处理 1 条" in s["closing"]
    assert "1 条重判后仍判不出" in s["closing"] and "待你裁决" in s["closing"]
    # 没有第二轮时也要报账,但不许喊狼来了
    s2 = run_rejudge(str(tmp_path), "/unused", {},
                     rerun_fn=lambda *a: {"passed": True, "verdict": "s",
                                          "detail": "{}"})
    assert "本轮处理" in s2["closing"] and "仍判不出,已进入" not in s2["closing"]


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


def test_parquet_sync_leaves_no_staging_dirs_in_delivery(tmp_path):
    """重判同步交付数据集之后,交付目录里不许留下任何临时目录。

    episodes_parquet 的换新走"同级 staging 写好再原子换位"(先删后写曾是丢整份
    数据集的隐患,见 test_delivery_write_safety):staging / 移到一边的旧目录都
    落在交付目录里,忘了清,客户 ls 交付目录就会把半成品当交付物拷走。
    """
    daft = pytest.importorskip("daft")
    d = _write_verdict_delivery(tmp_path, [
        {"episode_id": "epY", "verdict": "判失败", "note": "", "at": "t2"}])
    daft.from_pydict({
        "episode_id": ["epX", "epY"],
        "instruction": ["a", "b"],
        "instruction_source": ["原始标注"] * 2,
    }).write_parquet(str(d / "episodes_parquet"))

    run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    leftovers = [n for n in os.listdir(d) if n.startswith(".curation-")]
    assert leftovers == [], f"交付目录里残留了临时目录:{leftovers}"
    got = daft.read_parquet(str(d / "episodes_parquet")).to_pydict()
    assert got["episode_id"] == ["epX"]


# ───────── 被拒复议(2026-08-11):语义判定的杀可复议,物理硬门不可 ─────────


def _reject_views():
    """真实形态:三条被拒条目 —— 两条语义判定杀(可复议),一条时间戳残段(终局)。"""
    passed = {"episodes": {"epOK": {"判决": "通过", "综合软分": 0.9, "checks": {}}}}
    review = {"待人工裁决总数": 0, "episodes": {}}
    reject = {"被拒总数": 3, "episodes": {
        "epK1": {"判决": "拒绝", "原因": "未通过「任务成败判定」:全程没有把杯子拿起来",
                 "综合软分": 0.8,
                 "checks": {"任务成败判定": {"结果": "拒绝",
                                             "detail": '{"voc": 0.2}'}}},
        # 老交付的记法(硬门违规: 「X」)也必须认得,否则老交付一条都复议不了
        "epK2": {"判决": "拒绝", "原因": "硬门违规: 「任务成败判定」", "综合软分": 0.7,
                 "checks": {"任务成败判定": {"结果": "拒绝"}}},
        "epPhys": {"判决": "拒绝", "原因": "未通过「时间戳检查」:0.47 秒的采集残段",
                   "综合软分": 0.6,
                   "checks": {"时间戳检查": {"结果": "拒绝"}}}}}
    return passed, review, reject


def test_appeal_restore_flips_reject_to_pass_with_provenance():
    """捞回:出拒绝清单、判决改「通过(人工复议)」、成败检查落 pass、溯源在案;
    VLM 当时的读数(detail)必须原样留着——复盘时那是"机器为什么杀它"的唯一线索。"""
    from curation.pipeline.rejudge import apply_reject_appeals
    p, r, j = _reject_views()
    s = apply_reject_appeals(p, r, j, {"epK1": {"appeal": "捞回", "note": "看了视频,拿起来了",
                                                "at": "t1"}})
    assert s["appeal_restored"] == ["epK1"] and "epK1" not in j["episodes"]
    e = p["episodes"]["epK1"]
    assert e["判决"] == "通过(人工复议)" and e["综合软分"] == 0.8
    assert e["checks"]["任务成败判定"]["结果"] == "pass"
    assert e["checks"]["任务成败判定"]["detail"] == '{"voc": 0.2}'
    assert e["人工复议"]["复议结论"] == "捞回" and e["人工复议"]["备注"] == "看了视频,拿起来了"
    assert e["人工复议"]["复议时间"] == "t1"
    assert e["人工复议"]["verdict_source"] == "human_review"   # 下游按它区分人判/机判
    assert j["被拒总数"] == 2                                   # 计数同步


def test_appeal_restore_keeps_readings_and_other_open_questions():
    """两件在 droid-200 ep000023 上实测栽过的事:

    ① 同一条 episode 在 review 里还有一份**稀疏副本**(记的是另一个维度的弃权,
       那条是同步测不准)。三边摘除时先命中的是那份,拿它当底 → 综合软分和 VLM
       读数(六项 checks)全丢,捞回后交付里只剩一个"结果": "pass";
    ② 那份副本记的未决问题与复议无关,跟着一起销案 = 一个待人工问题无声消失。
    """
    from curation.pipeline.rejudge import apply_reject_appeals
    p, r, j = _reject_views()
    j["episodes"]["epK1"]["checks"]["视频-动作同步"] = {"结果": "弃权"}
    r["episodes"]["epK1"] = {"当前判决": "拒绝", "待裁决项": ["视频-动作同步"],
                             "弃权原因": {"视频-动作同步": "信号不足,测不准"}}
    r["待人工裁决总数"] = 1
    apply_reject_appeals(p, r, j, {"epK1": {"appeal": "捞回", "at": "t1"}})
    e = p["episodes"]["epK1"]
    assert e["综合软分"] == 0.8                                   # 软分没丢
    assert e["checks"]["任务成败判定"]["detail"] == '{"voc": 0.2}'  # 读数没丢
    assert "视频-动作同步" in e["checks"]                          # 别的维度读数也在
    assert r["episodes"]["epK1"]["待裁决项"] == ["视频-动作同步"]   # 未决问题仍挂着
    assert r["episodes"]["epK1"]["当前判决"] == "通过"             # 判决已翻,视图跟上
    assert r["待人工裁决总数"] == 1


def test_appeal_uphold_records_only_and_never_flips():
    """维持拒绝:只记档,判决/原因一个字不改(它是审计留痕,不是改判)。"""
    from curation.pipeline.rejudge import apply_reject_appeals
    p, r, j = _reject_views()
    s = apply_reject_appeals(p, r, j, {"epK2": {"appeal": "维持拒绝", "note": "确实没完成",
                                                "at": "t2"}})
    assert s["appeal_upheld"] == ["epK2"] and s["appeal_restored"] == []
    e = j["episodes"]["epK2"]
    assert e["判决"] == "拒绝" and e["原因"] == "硬门违规: 「任务成败判定」"
    assert e["人工复议"]["复议结论"] == "维持拒绝"
    assert "epK2" not in p["episodes"] and j["被拒总数"] == 3


def test_appeal_refuses_physical_hard_gates_even_if_csv_says_restore():
    """范围钉死:时间戳/残段这类物理与结构硬门的拒绝**捞不回来**——界面本就不给
    入口,裁决 CSV 是能被手改的文件,闭环这一侧必须再挡一次。"""
    from curation.pipeline.rejudge import apply_reject_appeals
    p, r, j = _reject_views()
    s = apply_reject_appeals(p, r, j, {"epPhys": {"appeal": "捞回", "at": "t"},
                                       "ep不存在": {"appeal": "捞回", "at": "t"},
                                       "epK1": {"appeal": "大概能用吧", "at": "t"}})
    assert set(s["appeal_skipped"]) == {"epPhys", "ep不存在", "epK1"}
    assert s["appeal_restored"] == []
    assert j["episodes"]["epPhys"]["判决"] == "拒绝"          # 原样不动
    assert "人工复议" not in j["episodes"]["epPhys"]
    assert "ep不存在" not in p["episodes"]                    # 绝不凭空造条目


def test_appeal_reject_reason_scope_is_pinned():
    """准入判据逐格钉死:同时踩了物理硬门的条目**不算**语义杀(证据不因人的意见消失)。"""
    from curation.dataset_level.decisions import is_task_success_reject
    assert is_task_success_reject("未通过「任务成败判定」:没拿起来")
    assert is_task_success_reject("硬门违规: 「任务成败判定」")
    assert is_task_success_reject("hard fail: task_success")
    assert not is_task_success_reject("未通过「时间戳检查」:0.47 秒残段")
    assert not is_task_success_reject("未通过「任务成败判定」:没拿起来;"
                                      "未通过「视频-动作同步」:整体错位 0.4 秒")
    assert not is_task_success_reject("未通过「运动学极限」:关节 3 超限")
    assert not is_task_success_reject("与 ep000007 字节级完全重复")
    assert not is_task_success_reject("人工裁决判失败(任务未完成)")
    assert not is_task_success_reject("") and not is_task_success_reject(None)


def _write_appeal_delivery(tmp_path, rows, views=None):
    """交付三件套 + details/reject_appeals.csv 的最小 fixture。"""
    (tmp_path / "details").mkdir(exist_ok=True)
    p, r, j = views or _reject_views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
    with open(tmp_path / "details" / "reject_appeals.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["episode_id", "appeal", "note", "at"])
        w.writeheader()
        w.writerows(rows)
    return tmp_path


def test_run_rejudge_consumes_appeals_and_writes_report_section(tmp_path):
    """端到端:只有复议草稿也要照跑,**一次 VLM 都不许调**;报告出「人工复议捞回」
    小节并逐条列明,留档记下本轮真正应用的复议。"""
    d = _write_appeal_delivery(tmp_path, [
        {"episode_id": "epK1", "appeal": "捞回", "note": "拿起来了", "at": "t1"},
        {"episode_id": "epK2", "appeal": "维持拒绝", "note": "", "at": "t2"}])
    (d / "report.md").write_text("# 报告\n", encoding="utf-8")

    def _boom(*a):
        raise AssertionError("复议不该触发重判")

    s = run_rejudge(str(d), "/unused", {}, rerun_fn=_boom)
    assert s["appeal_restored"] == ["epK1"] and s["appeal_upheld"] == ["epK2"]
    psd = json.loads((d / "passed.json").read_text(encoding="utf-8"))
    rej = json.loads((d / "reject.json").read_text(encoding="utf-8"))
    assert psd["episodes"]["epK1"]["判决"] == "通过(人工复议)"
    assert "epK1" not in rej["episodes"] and set(rej["episodes"]) == {"epK2", "epPhys"}
    md = (d / "report.md").read_text(encoding="utf-8")
    assert "人工复议捞回 1 条" in md and "维持拒绝 1 条" in md
    assert "epK1:人工复议判为可用" in md
    res = json.loads((d / "details" / "rejudge_results.json").read_text(encoding="utf-8"))
    assert res["reject_appeals"] == {"epK1": "捞回", "epK2": "维持拒绝"}


def test_run_rejudge_appeal_is_idempotent(tmp_path):
    """同一条复议(结论+时间没变)已落库 → 第二趟跳过,报告不再重数一遍。"""
    d = _write_appeal_delivery(tmp_path, [
        {"episode_id": "epK2", "appeal": "维持拒绝", "note": "", "at": "t2"}])
    first = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert first["appeal_upheld"] == ["epK2"]
    second = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert second["unchanged"] == ["epK2"] and second["appeal_upheld"] == []
    # 改判(复议时间变了)→ 重新应用
    with open(d / "details" / "reject_appeals.csv", "a", newline="",
              encoding="utf-8") as f:
        csv.writer(f).writerow(["epK2", "捞回", "复看后改判", "t9"])
    third = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert third["appeal_restored"] == ["epK2"]


def test_legacy_delivery_without_appeal_file_still_runs(tmp_path):
    """老交付根本没有 reject_appeals.csv:照常按另两条线跑完,不许炸。"""
    d = _write_verdict_delivery(tmp_path, [
        {"episode_id": "epX", "verdict": "判成功", "note": "", "at": "t1"}])
    assert not (d / "details" / "reject_appeals.csv").exists()
    s = run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert s["verdict_pass"] == ["epX"] and s["appeal_restored"] == []


def test_appeal_restores_row_into_delivered_dataset(tmp_path):
    """出数据闭环(反向):被拒条目**从来没进过** episodes_parquet,捞回要回源
    把这一行加回来,LeRobot 包跟着重导出——只翻报告不补数据,客户拿到的还是少一条。"""
    pytest = __import__("pytest")
    pytest.importorskip("pandas")
    daft = pytest.importorskip("daft")
    from curation.tests.test_lerobot_v2_export import _write_v2_dataset
    src = tmp_path / "src_ds"
    _write_v2_dataset(str(src))                       # 4 条最小 v2 数据集
    d = tmp_path / "dlv"
    d.mkdir()
    views = ({"episodes": {f"ep{i:06d}": {"判决": "通过"} for i in (0, 2, 3)}},
             {"episodes": {}},
             {"被拒总数": 1, "episodes": {"ep000001": {
                 "判决": "拒绝", "原因": "未通过「任务成败判定」:没完成",
                 "checks": {"任务成败判定": {"结果": "拒绝"}}}}})
    _write_appeal_delivery(d, [{"episode_id": "ep000001", "appeal": "捞回",
                                "note": "人看是成功的", "at": "t1"}], views=views)
    daft.from_pydict({
        "episode_id": ["ep000000", "ep000002", "ep000003"],
        "instruction": ["a", "c", "d"],
        "instruction_source": ["原始标注"] * 3,
    }).write_parquet(str(d / "episodes_parquet"))
    (d / "lerobot_curated").mkdir()
    (d / "lerobot_curated" / "stale").write_text("old")

    run_rejudge(str(d), str(src), {}, rerun_fn=None)
    got = daft.read_parquet(str(d / "episodes_parquet")).to_pydict()
    assert "ep000001" in got["episode_id"], "捞回的条目没补回交付数据集"
    assert got["episode_id"] == sorted(got["episode_id"])       # 补进来仍按号有序
    by = dict(zip(got["episode_id"], got["instruction_source"]))
    # 补标只按源数据如实写:质检时的自产 caption 这里拿不到,不许现编一句
    assert by["ep000001"] in ("原始标注", "无")
    eps = [json.loads(x) for x in
           (d / "lerobot_curated" / "meta" / "episodes.jsonl").read_text().splitlines()]
    assert len(eps) == 4, "LeRobot 包没跟着把捞回的条目导出来"


# ───────── 技能画像同步(2026-08-16 标注优先方针③)─────────
#
# 防的事故(droid-200-new):26 条被错 caption 归错格子,人工裁决完(维持原标注/
# 采纳改标/弃用)画像却纹丝不动——技能分布里继续数着已被剔除的数据、错格子的
# 条目继续错,报告在骗人。这组测试钉死:三种裁决 + 判失败/捞回对画像的动作,
# 以及**绝不重跑 caption / 绝不重新归纳体系**(纯文本对既有体系再分配)。

_WIPE = "wipe the table with the yellow towel"
_FOLD = "fold the yellow towel"
_CUP = "pick up the cup"


def _profile_fixture():
    def sub(eps, caps):
        return {"count": len(eps), "pct": 33.33, "episodes": eps,
                "captions_top": caps, "raw_labels_top": []}
    return {"n_episodes": 3, "n_families": 3,
            "families": {
                "wiping": {"count": 1, "pct": 33.33, "criterion": "cyclic wipe",
                           "subskills": {"wipe-surface": sub(["ep000001"], [_WIPE])}},
                "folding": {"count": 1, "pct": 33.33,
                            "subskills": {"fold-cloth": sub(["ep000002"], [_FOLD])}},
                "grasping": {"count": 1, "pct": 33.33,
                             "subskills": {"pick-up": sub(["ep000003"], [_CUP])}}},
            "undersampled": [], "guideline": "G"}


def _write_profile_delivery(tmp_path):
    """带两级画像的交付:ep000002 有标注但被错 caption 归进 folding(老口径)。"""
    det = tmp_path / "details"
    det.mkdir(parents=True, exist_ok=True)
    passed = {"skills": _profile_fixture(), "episodes": {
        "ep000001": {"判决": "通过", "综合软分": 0.9,
                     "checks": {"任务成败判定": {"结果": "pass"}}},
        "ep000002": {"判决": "通过", "综合软分": 0.8,
                     "checks": {"任务成败判定": {"结果": "pass"}}},
        "ep000003": {"判决": "通过", "综合软分": 0.7,
                     "checks": {"任务成败判定": {"结果": "pass"}}}}}
    review = {"待人工裁决总数": 0, "episodes": {},
              "标注-画面分歧复核队列": [
                  {"id": "ep000002", "label": _WIPE, "caption": _FOLD}]}
    reject = {"被拒总数": 1, "episodes": {
        "ep000009": {"判决": "拒绝", "原因": "未通过「任务成败判定」:没擦",
                     "综合软分": 0.5,
                     "checks": {"任务成败判定": {"结果": "拒绝"}}}}}
    for name, data in [("passed", passed), ("review", review), ("reject", reject)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
    with open(det / "skill_assignment.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["episode_id", "family", "subskill", "caption"])   # 老四列交付
        w.writerows([["ep000001", "wiping", "wipe-surface", _WIPE],
                     ["ep000002", "folding", "fold-cloth", _FOLD],
                     ["ep000003", "grasping", "pick-up", _CUP]])
    (det / "captions.json").write_text(json.dumps(
        {"ep000001": _WIPE, "ep000002": _FOLD, "ep000003": _CUP}), encoding="utf-8")
    (det / "task_details.json").write_text(json.dumps({"episodes": {
        "ep000002": {"episode_id": "ep000002", "instruction": _WIPE,
                     "instruction_source": "原始标注"},
        "ep000009": {"episode_id": "ep000009", "instruction": _WIPE,
                     "instruction_source": "原始标注"},
    }}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _csv_by_ep(tmp_path):
    with open(tmp_path / "details" / "skill_assignment.csv", newline="",
              encoding="utf-8") as f:
        return {r["episode_id"]: r for r in csv.DictReader(f)}


def _profile_episodes(tmp_path):
    prof = json.loads((tmp_path / "passed.json").read_text(encoding="utf-8"))["skills"]
    return {e: (fn, sn) for fn, fam in prof["families"].items()
            for sn, s in fam["subskills"].items() for e in s["episodes"]}


def test_rejudge_keep_decision_regroups_by_original_label_idempotently(tmp_path,
                                                                       monkeypatch):
    """维持原标注 → 按**原始标注**重归类(把老交付错 caption 归的搬回正确格子);
    幂等:再跑一次结果逐字节相同;全程不重跑 caption、不重新归纳体系。"""
    import curation.dataset_level.caption as C
    import curation.dataset_level.taxonomy as T

    def boom(*a, **k):
        raise AssertionError("rejudge 画像同步不许重跑 caption / 重新归纳体系")

    monkeypatch.setattr(T, "induce_taxonomy", boom)
    monkeypatch.setattr(T, "refine_taxonomy", boom)
    monkeypatch.setattr(C, "caption_episodes", boom)
    d = _write_profile_delivery(tmp_path)
    (d / "details" / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000002,维持原标注,,,2026-08-16 10:00:00\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {})
    assert "ep000002" in s["profile_sync"]["reassigned"]
    assert _profile_episodes(d)["ep000002"] == ("wiping", "wipe-surface")
    by = _csv_by_ep(d)
    assert by["ep000002"]["family"] == "wiping"
    assert by["ep000002"]["grouping_text"] == _WIPE
    assert by["ep000002"]["grouping_text_source"] == "原始标注"
    assert by["ep000002"]["caption"] == _FOLD          # caption 照旧陈列,不被抹掉
    # 幂等:同一裁决再消化一遍,画像与 CSV 逐字节不变
    csv1 = (d / "details" / "skill_assignment.csv").read_text(encoding="utf-8")
    skills1 = json.loads((d / "passed.json").read_text(encoding="utf-8"))["skills"]
    run_rejudge(str(d), "/unused", {})
    assert (d / "details" / "skill_assignment.csv").read_text(encoding="utf-8") == csv1
    assert json.loads((d / "passed.json").read_text(
        encoding="utf-8"))["skills"] == skills1


def test_rejudge_adopt_relabel_regroups_with_new_label(tmp_path):
    """采纳改标(重判通过)→ 用**新标注**重归类;成败线判定照旧由重判决定。"""
    d = _write_profile_delivery(tmp_path)
    (d / "details" / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        f"ep000002,采纳建议改标,{_WIPE},,2026-08-16 10:00:00\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {},
                    rerun_fn=lambda i, e, nl: {"passed": True, "verdict": "success",
                                               "detail": "{}"})
    assert s["adopted_pass"] == ["ep000002"]
    assert _profile_episodes(d)["ep000002"] == ("wiping", "wipe-surface")
    by = _csv_by_ep(d)
    assert by["ep000002"]["grouping_text"] == _WIPE
    assert by["ep000002"]["grouping_text_source"] == "原始标注"


def test_rejudge_adopt_relabel_rejudged_fail_removed_from_profile(tmp_path):
    """采纳改标但重判仍失败 → 条目进 reject,同款从画像与 CSV 移除
    (画像描述的是"我们交付的那批数据",Q1 判据)。"""
    d = _write_profile_delivery(tmp_path)
    (d / "details" / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        f"ep000002,采纳建议改标,{_WIPE},,2026-08-16 10:00:00\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {},
                    rerun_fn=lambda i, e, nl: {"passed": False, "verdict": "failure",
                                               "detail": "{}"})
    assert s["adopted_reject"] == ["ep000002"]
    assert "ep000002" not in _profile_episodes(d)
    assert "ep000002" not in _csv_by_ep(d)


def test_rejudge_drop_and_verdict_fail_removed_untouched_intact(tmp_path):
    """弃用该条 + 成败裁决判失败 → 双双移出画像与 CSV(已被剔出交付,继续数着
    = 技能分布是事实错误);未被裁决条目的成败判定字段逐字节不变(护栏二)。"""
    d = _write_profile_delivery(tmp_path)
    before = json.loads((d / "passed.json").read_text(
        encoding="utf-8"))["episodes"]["ep000001"]
    (d / "details" / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000002,弃用该条,,,2026-08-16 10:00:00\n", encoding="utf-8")
    (d / "details" / "task_verdicts.csv").write_text(
        "episode_id,verdict,note,at\n"
        "ep000003,判失败,,2026-08-16 10:00:01\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {})
    assert set(s["profile_sync"]["removed"]) == {"ep000002", "ep000003"}
    eps = _profile_episodes(d)
    assert "ep000002" not in eps and "ep000003" not in eps
    by = _csv_by_ep(d)
    assert "ep000002" not in by and "ep000003" not in by and "ep000001" in by
    after = json.loads((d / "passed.json").read_text(
        encoding="utf-8"))["episodes"]["ep000001"]
    assert after == before                     # 未裁决条目的判定一个字没动


def test_rejudge_appeal_restore_regroups_by_label_never_recaptions(tmp_path):
    """复议捞回 → 有原始标注按标注归类补回画像;**绝不为它重跑 VLM caption**
    (被拒条目当初没进画像、没有 caption——一条捞回触发一次 VLM 是烧钱设计)。"""
    d = _write_profile_delivery(tmp_path)
    (d / "details" / "reject_appeals.csv").write_text(
        "episode_id,appeal,note,at\n"
        "ep000009,捞回,,2026-08-16 10:00:00\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {})
    assert s["appeal_restored"] == ["ep000009"]
    assert "ep000009" in s["profile_sync"]["restored"]
    assert _profile_episodes(d)["ep000009"] == ("wiping", "wipe-surface")
    assert _csv_by_ep(d)["ep000009"]["grouping_text_source"] == "原始标注"


def test_rejudge_appeal_restore_without_label_stays_unassigned(tmp_path):
    """复议捞回但取不到原始标注(task_details 里没这条)→ 留「未归类」如实报数,
    不猜、不重跑 caption。"""
    d = _write_profile_delivery(tmp_path)
    det = d / "details"
    td = json.loads((det / "task_details.json").read_text(encoding="utf-8"))
    td["episodes"].pop("ep000009")
    (det / "task_details.json").write_text(json.dumps(td, ensure_ascii=False),
                                           encoding="utf-8")
    (det / "reject_appeals.csv").write_text(
        "episode_id,appeal,note,at\n"
        "ep000009,捞回,,2026-08-16 10:00:00\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {})
    assert "ep000009" in s["profile_sync"]["unassigned"]
    assert _profile_episodes(d)["ep000009"][0] == "未归类"


def test_rejudge_profile_sync_degrades_without_profile(tmp_path):
    """老交付没画像/CSV → 画像同步如实注明跳过(体系不重新归纳),裁决本体照常。"""
    d = tmp_path
    (d / "details").mkdir()
    p, r, j = _views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (d / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                        encoding="utf-8")
    (d / "details" / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "epB,维持原标注,,,2026-08-16 10:00:00\n", encoding="utf-8")
    s = run_rejudge(str(d), "/unused", {})
    assert s["kept"] == ["epB"]
    assert "note" in s["profile_sync"]         # 注明未同步,而不是硬造画像


def test_rejudge_lands_passed_json_exactly_once(tmp_path, monkeypatch):
    """★ 一次重判只许落一遍 passed.json。

    2026-08-14 事故:延时入账那段排在落盘**之后**,于是同一个 passed.json 两秒内
    被写了两遍;当时的发布方式是就地截断,第二遍撞进第一遍的中间态,文件最后停在
    138719 字节全 `\0`,整份交付作废。发布已改成原子改名(见
    test_delivery_write_safety),但"同一个产物一次只落一遍"是纪律本身,不靠
    下游兜底 —— 靠下游兜底的东西,下次换个写法就又漏了。
    """
    from curation.pipeline import rejudge as rj

    d = _write_appeal_delivery(tmp_path, [
        {"episode_id": "epK1", "appeal": "捞回", "note": "拿起来了", "at": "t1"}])
    (d / "report.md").write_text("# 报告\n", encoding="utf-8")

    lands: list[str] = []
    real = rj.write_json
    monkeypatch.setattr(rj, "write_json",
                        lambda dst, *a, **k: (lands.append(os.path.basename(dst)),
                                              real(dst, *a, **k))[1])
    run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    assert lands.count("passed.json") == 1, f"passed.json 落了 {lands.count('passed.json')} 遍:{lands}"


def test_v3_lerobot_curated_reexported_on_decisions(tmp_path, monkeypatch):
    """v3 源的 LeRobot 包在裁决后同样自动重导出(2026-08-21 用户定:之前只提醒
    "重跑 run"是 bug —— run 不应用裁决,v3 客户永远拿不到落实裁决的成品包)。
    导出器打桩:只验接线(终态条目集、改标覆写、旧包被整体替换、写到临时目录再换位)。"""
    pytest = __import__("pytest")
    pytest.importorskip("pandas")
    daft = pytest.importorskip("daft")
    import json
    import os

    from curation.export import lerobot_writer as lw
    from curation.pipeline.rejudge import run_rejudge
    src = tmp_path / "src_v3"
    (src / "meta").mkdir(parents=True)
    (src / "meta" / "info.json").write_text(json.dumps(
        {"codebase_version": "v3.0", "total_episodes": 4, "fps": 30}), encoding="utf-8")
    delivery = tmp_path / "dlv"
    det = delivery / "details"
    det.mkdir(parents=True)
    (det / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\n"
        "ep000001,采纳建议改标,corrected task text,,2026-08-21 13:00:00\n"
        "ep000002,弃用该条,,,2026-08-21 13:00:05\n", encoding="utf-8")
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
    curated = delivery / "lerobot_curated"
    (curated / "meta").mkdir(parents=True)
    (curated / "stale").write_text("old")
    (curated / "meta" / "curation_camera_health.json").write_text(json.dumps(
        {"dataset": {"k": 1}, "episodes": [{"source_episode_id": "ep000000", "x": 1}]}))

    calls = []

    def fake_v3(dataset_dir, keep, out_dir, task_overrides=None, camera_health=None):
        assert os.path.isdir(out_dir) and not os.listdir(out_dir), "必须写进同级空临时目录"
        assert os.path.dirname(out_dir) == str(delivery), "临时目录要与交付同级(同一挂载才能原子换位)"
        assert (curated / "stale").exists(), "新包写完之前旧包一个字节都不能动"
        os.makedirs(os.path.join(out_dir, "meta"))
        with open(os.path.join(out_dir, "meta", "info.json"), "w") as f:
            json.dump({"codebase_version": "v3.0", "total_episodes": len(keep)}, f)
        calls.append((dataset_dir, list(keep), dict(task_overrides or {}), camera_health))
        return {"out_dir": out_dir}

    def boom(*a, **k):
        raise AssertionError("v3 源不该走 v2 导出器")
    monkeypatch.setattr(lw, "export_lerobot_v3", fake_v3)
    monkeypatch.setattr(lw, "export_lerobot_v2", boom)

    run_rejudge(str(delivery), str(src), {},
                rerun_fn=lambda i, e, nl: {"passed": True, "verdict": "success"})
    assert len(calls) == 1, "v3 分支必须真的重导出,不能只打印提醒"
    ds, keep, ov, ch = calls[0]
    assert ds == str(src) and keep == [0, 1, 3], "弃用的 ep000002 要从终态条目集里剔掉"
    assert ov == {1: "corrected task text"}, "采纳的改标要作为任务覆写传给导出器"
    assert ch["dataset"] == {"k": 1} and "ep000000" in ch["episodes"], "相机健康度旁挂文件要跟着走"
    assert not (curated / "stale").exists(), "旧包未被替换"
    assert json.load(open(curated / "meta" / "info.json"))["total_episodes"] == 3


# ═════════ dataset 汇总块平账(2026-08-25 droid-50 实战复盘 ①)═════════

def test_sync_dataset_summary_rebalances_after_drops():
    """剔条目后 delivered/verdict_drop/human_drop/通过率全部对回真实条目数。"""
    from curation.pipeline.rejudge import _sync_dataset_summary
    files = {
        "passed": {"dataset": {"input_episodes": 50, "dedup_removed": 0,
                               "verdict_drop": 6, "delivered": 44,
                               "hard_fail_breakdown": {"task_success": 5,
                                                       "timestamp_check": 1},
                               "summary_stats": {"pass_rate_pct": 88.0,
                                                 "avg_soft_score": 0.9307}},
                   "episodes": {f"ep{i:06d}": {"综合软分": 0.9}
                                for i in range(39)}},
        "review": {"episodes": {}},
        "reject": {"episodes": {
            **{f"epX{i}": {"原因": "人工裁决判失败(任务未完成)"} for i in range(4)},
            "epY0": {"原因": "人工裁决弃用(标注-画面分歧复核)"},
            "epZ0": {"原因": "未通过「任务成败判定」:取证仲裁"},
        }},
    }
    _sync_dataset_summary(files)
    d = files["passed"]["dataset"]
    assert d["delivered"] == 39
    assert d["verdict_drop"] == 11          # 50 - 0 - 39,账重新闭合
    assert d["human_drop"] == 5             # 只数「人工裁决」前缀,机器判废不算
    assert d["summary_stats"]["pass_rate_pct"] == 78.0
    assert d["summary_stats"]["avg_soft_score"] == 0.9
    # 幂等:重跑一遍数字一个不变(rejudge 可反复执行,平账靠重算不靠加减)
    import copy
    snap = copy.deepcopy(files["passed"]["dataset"])
    _sync_dataset_summary(files)
    assert files["passed"]["dataset"] == snap


def test_sync_dataset_summary_skips_legacy_and_untouched():
    """老交付(没有 dataset 块)整个跳过;没动过条目的交付账本原样。"""
    from curation.pipeline.rejudge import _sync_dataset_summary
    legacy = {"passed": {"episodes": {"a": {}}}, "review": {}, "reject": {}}
    _sync_dataset_summary(legacy)
    assert "dataset" not in legacy["passed"]
    untouched = {"passed": {"dataset": {"input_episodes": 3, "verdict_drop": 1,
                                        "delivered": 2},
                            "episodes": {"a": {}, "b": {}}},
                 "review": {}, "reject": {"episodes": {}}}
    import copy
    snap = copy.deepcopy(untouched)
    _sync_dataset_summary(untouched)
    assert untouched == snap


def test_overview_breakdown_lists_human_drop_row():
    """总览分解式带「人工裁决」行且等式闭合 —— 不列这行的话人工剔除量会落进
    差额兜底行、被冠上「质量分」的名头(2026-08-25 复盘 ①)。"""
    from curation.ui.manifest import drop_breakdown
    d = {"verdict_drop": 11, "human_drop": 5,
         "hard_fail_breakdown": {"task_success": 5, "timestamp_check": 1}}
    items, mode = drop_breakdown(d)
    assert mode == "decompose"
    assert ("人工裁决", 5) in items
    assert sum(n for _, n in items) == 11   # 机器 6 + 人工 5,不再有兜底差额行
    # 反向:没有 human_drop 字段时不出现这一行(老交付/未裁决交付)
    items2, _ = drop_breakdown({"verdict_drop": 6,
                                "hard_fail_breakdown": {"task_success": 5,
                                                        "timestamp_check": 1}})
    assert all(lbl != "人工裁决" for lbl, _n in items2)
