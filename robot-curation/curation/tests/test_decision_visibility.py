"""裁决沿用可见性 + 「有裁决尚未应用」提醒(2026-08-16)。

防的事故(用户 2026-08-16 拍板时点名的两面):
① 裁决 CSV 自 2026-08-14 起住在交付根 human-decisions/、跨跑批共用,rejudge 会把
   三周前的人工裁决自动应用到新跑批上 —— 报告与界面不吭声,用户以为这批结论全是
   机器判的,而它混着旧的人工判断;
② 反过来,跑完新一批忘了点「执行人工裁决」,交出去的就是把人的决定全丢掉的数据,
   而此前报告页/任务台/任务卡片一个字都不提。

三条硬约束在此钉死:未应用计数与 rejudge 幂等跳过**同一套判据**(有人另写一套
就变红);落空的裁决(episode 不在本次跑批里)单独成类不混进"未应用";时间解析
不出如实降级为"无法判定新旧",绝不猜。
"""
from __future__ import annotations

import datetime
import json
import os

import pytest

from curation.dataset_level import decisions as dec
from curation.pipeline.rejudge import run_rejudge

AT_OLD = "2026-08-01 10:00:00"          # 早于下面跑批目录名的时间戳 → 沿用
AT_NEW = "2026-08-11 09:00:00"          # 晚于跑批开始 → 本轮新裁
RUN_NAME = "20260810-120000"


def _views():
    """弃权条目的真实形态(passed+review 双呈现)+ 总览要用的 dataset 统计。"""
    passed = {"数据集": "fake", "机器人": "so101",
              "生成时间": "2026-08-10 12:30:00", "代码版本": "deadbee",
              "dataset": {"input_episodes": 2, "verdict_drop": 0,
                          "dedup_removed": 0, "delivered": 2,
                          "hard_fail_breakdown": {},
                          "summary_stats": {"pass_rate_pct": 100.0,
                                            "avg_soft_score": 0.9}},
              "episodes": {
                  "epX": {"判决": "通过", "综合软分": 0.8,
                          "checks": {"任务成败判定": {"结果": "弃权"}}},
                  "epY": {"判决": "通过", "综合软分": 0.7,
                          "checks": {"任务成败判定": {"结果": "弃权"}}}}}
    review = {"待人工裁决总数": 2, "episodes": {
        e: {"当前判决": "通过", "待裁决项": ["任务成败判定"],
            "弃权原因": {"任务成败判定": "渐变问询不可判"}}
        for e in ("epX", "epY")}}
    reject = {"被拒总数": 0, "episodes": {}}
    return passed, review, reject


def _mk_run(tmp_path, *, run_name=RUN_NAME, views=None, verdict_rows="",
            decision_rows="", appeal_rows="", run_facts=None):
    """新布局交付:<root>/<run_name>/ 三件套 + <root>/human-decisions/ 三张 CSV。"""
    root = tmp_path / "deliv"
    run = root / run_name
    run.mkdir(parents=True, exist_ok=True)
    p, r, j = views or _views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (run / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                          encoding="utf-8")
    (run / "report.md").write_text("# 报告\n", encoding="utf-8")
    if run_facts is not None:
        (run / "run.json").write_text(json.dumps(run_facts, ensure_ascii=False),
                                      encoding="utf-8")
    hd = root / "human-decisions"
    hd.mkdir(exist_ok=True)
    if verdict_rows:
        (hd / "task_verdicts.csv").write_text(
            "episode_id,verdict,note,at\n" + verdict_rows, encoding="utf-8")
    if decision_rows:
        (hd / "label_decisions.csv").write_text(
            "episode_id,decision,new_label,note,at\n" + decision_rows,
            encoding="utf-8")
    if appeal_rows:
        (hd / "reject_appeals.csv").write_text(
            "episode_id,appeal,note,at\n" + appeal_rows, encoding="utf-8")
    return str(run)


# ───────── 沿用判据:时间解析与新旧分类 ─────────


def test_parse_decision_time_two_formats_no_guessing():
    """两种合法时间都要认(CSV 钟面时间 / 跑批目录时间戳);认不出返回 None ——
    fixture 里的 "t1" 这类占位串曾差点被按"某个时刻"处理,那就是在猜。"""
    assert dec.parse_decision_time("2026-08-15 05:29:43") == \
        datetime.datetime(2026, 8, 15, 5, 29, 43)
    assert dec.parse_decision_time("20260815-052943") == \
        datetime.datetime(2026, 8, 15, 5, 29, 43)
    assert dec.parse_decision_time("2026-08-15 05:29") == \
        datetime.datetime(2026, 8, 15, 5, 29)          # 分钟精度的老手写记录也认
    assert dec.parse_decision_time("t1") is None
    assert dec.parse_decision_time("") is None and dec.parse_decision_time(None) is None
    assert dec.parse_decision_time("droid-200-full") is None


def test_run_started_at_prefers_run_facts_then_dirname(tmp_path):
    """跑批开始时间的权威顺序:run.json 的「跑批」字段 > 目录名时间戳 > None。"""
    d = tmp_path / RUN_NAME
    d.mkdir()
    assert dec.run_started_at(str(d)) == datetime.datetime(2026, 8, 10, 12, 0, 0)
    (d / "run.json").write_text(json.dumps({"跑批": "20260501-000000"}),
                                encoding="utf-8")
    assert dec.run_started_at(str(d)) == datetime.datetime(2026, 5, 1, 0, 0, 0)
    legacy = tmp_path / "droid-200-full"                 # 老布局:名字不是时间戳
    legacy.mkdir()
    assert dec.run_started_at(str(legacy)) is None       # 判不了就是判不了,不猜


def test_carryover_fresh_unknown_classification():
    """裁决时间早于跑批开始 → 沿用;晚于 → 本轮新裁;任一时间解析不出 → 降级
    为 unknown(绝不硬塞进沿用或新裁)。"""
    files = {"passed": {"episodes": {"epX": {
        "人工裁决": {"裁决": "判成功", "裁决时间": AT_OLD}}}},
        "review": {"episodes": {}}, "reject": {"episodes": {}}}
    started = datetime.datetime(2026, 8, 10, 12, 0, 0)
    old = dec.decisions_view(files, {}, {"epX": {"verdict": "判成功", "at": AT_OLD}},
                             {}, run_started=started)[0]
    assert old["status"] == "applied" and old["when"] == "carryover"
    files2 = {"passed": {"episodes": {"epX": {
        "人工裁决": {"裁决": "判成功", "裁决时间": AT_NEW}}}},
        "review": {"episodes": {}}, "reject": {"episodes": {}}}
    new = dec.decisions_view(files2, {}, {"epX": {"verdict": "判成功", "at": AT_NEW}},
                             {}, run_started=started)[0]
    assert new["when"] == "fresh"
    undated = dec.decisions_view(files2, {},
                                 {"epX": {"verdict": "判成功", "at": AT_NEW}},
                                 {}, run_started=None)[0]
    assert undated["when"] == "unknown"


def test_result_changing_and_mark_only_counted_separately():
    """计数分开报(用户点名):把 38 条混成一个数,读者会以为 38 条结论全被人动过,
    而实际只有改结果的那几条。搁置/维持拒绝/维持原标注只留痕,不改任何数据。"""
    started = datetime.datetime(2026, 8, 10, 12, 0, 0)
    files = {"passed": {"episodes": {
        "epA": {"人工裁决": {"裁决": "判成功", "裁决时间": AT_OLD}},
        "epB": {"人工裁决": {"裁决": "搁置", "裁决时间": AT_OLD}}}},
        "review": {"episodes": {}}, "reject": {"episodes": {
            "epC": {"人工复议": {"复议结论": "维持拒绝", "复议时间": AT_OLD}}}}}
    recs = dec.decisions_view(files, {},
                              {"epA": {"verdict": "判成功", "at": AT_OLD},
                               "epB": {"verdict": "搁置", "at": AT_OLD}},
                              {"epC": {"appeal": "维持拒绝", "at": AT_OLD}},
                              run_started=started)
    c = dec.application_counts(recs)
    assert c["carryover"] == 3 and c["carryover_changed"] == 1
    assert c["applied"] == 3 and c["unapplied"] == 0


def test_orphaned_decision_is_its_own_bucket_not_unapplied():
    """落空的裁决(episode 不在本次跑批的三件套里)单独成类:混进"未应用"那个
    数字永远消不掉,提醒就成了狼来了。"""
    files = {"passed": {"episodes": {"epX": {}}},
             "review": {"episodes": {}}, "reject": {"episodes": {}}}
    recs = dec.decisions_view(files, {},
                              {"epX": {"verdict": "判成功", "at": AT_OLD},
                               "epGone": {"verdict": "判失败", "at": AT_OLD}}, {})
    by = {r["id"]: r for r in recs}
    assert by["epX"]["status"] == "unapplied"
    assert by["epGone"]["status"] == "orphaned"
    c = dec.application_counts(recs)
    assert c["unapplied"] == 1 and c["orphaned"] == 1


# ───────── report.md 的「沿用」小节 ─────────


def test_report_gets_carryover_section_with_per_episode_table(tmp_path):
    """裁决时间早于跑批开始 → rejudge 后报告出「沿用」小节:分开计数 + 逐条表格
    + 本轮新裁条数。没有这一节,读者分不清某条结论是机器判的还是人改过的。"""
    run = _mk_run(tmp_path,
                  verdict_rows=f"epX,判成功,,{AT_OLD}\nepY,搁置,,{AT_NEW}\n")
    s = run_rejudge(run, "/unused", {}, rerun_fn=None)
    assert s["verdict_pass"] == ["epX"] and s["verdict_hold"] == ["epY"]
    md = (tmp_path / "deliv" / RUN_NAME / "report.md").read_text(encoding="utf-8")
    assert "沿用自此前的人工裁决" in md
    assert "沿用 1 条:改变结果的 1 条" in md and "仅标记的 0 条" in md
    assert "本轮新裁 1 条" in md
    assert f"| epX | 判成功 | {AT_OLD} |" in md          # 逐条表格,具体到 episode
    assert "| epY" not in md                             # 本轮新裁的不进沿用表


def test_report_carryover_section_absent_when_nothing_carried(tmp_path):
    """零沿用(裁决都是本轮新裁、时间可判)→ 整节不出现,不制造噪音。"""
    run = _mk_run(tmp_path, verdict_rows=f"epX,判成功,,{AT_NEW}\n")
    run_rejudge(run, "/unused", {}, rerun_fn=None)
    md = (tmp_path / "deliv" / RUN_NAME / "report.md").read_text(encoding="utf-8")
    assert "沿用自此前的人工裁决" not in md


def test_report_degrades_honestly_when_time_unparseable(tmp_path):
    """时间解析不出(老布局目录名 + 手写占位裁决时间)→ 如实写「无法判定新旧」,
    不许把它们硬算成沿用或新裁。"""
    d = tmp_path / "legacy-name"
    d.mkdir()
    p, r, j = _views()
    for name, data in [("passed", p), ("review", r), ("reject", j)]:
        (d / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False),
                                        encoding="utf-8")
    (d / "report.md").write_text("# 报告\n", encoding="utf-8")
    (d / "details").mkdir()
    (d / "details" / "task_verdicts.csv").write_text(
        "episode_id,verdict,note,at\nepX,判成功,,t1\n", encoding="utf-8")
    run_rejudge(str(d), "/unused", {}, rerun_fn=None)
    md = (d / "report.md").read_text(encoding="utf-8")
    assert "无法判定新旧" in md and "沿用 " not in md


# ───────── 同源:UI 计数与 rejudge 幂等必须走同一个判据 ─────────


def test_unapplied_criteria_shared_between_rejudge_and_ui(tmp_path, monkeypatch):
    """★ 换掉共享判据,rejudge 的跳过与 UI 的计数必须**一起**变 —— 谁把比对逻辑
    另写一套(内联回 rejudge 或 UI),这条就红。两套判据迟早说两种话:界面喊着
    「未应用」而 rejudge 说「已落库跳过」,用户对着按钮白点。"""
    from curation.ui.manifest import decision_status, load_delivery
    run = _mk_run(tmp_path, verdict_rows=f"epX,判成功,,{AT_OLD}\n")

    # 正向:判据说"全都落库了" → rejudge 一条不应用,UI 报 1 条已应用
    monkeypatch.setattr(dec, "task_verdict_applied_in", lambda *a: "passed")
    s = run_rejudge(run, "/unused", {}, rerun_fn=None)
    assert s["unchanged"] == ["epX"] and s["verdict_pass"] == []
    psd = json.loads((tmp_path / "deliv" / RUN_NAME / "passed.json")
                     .read_text(encoding="utf-8"))
    assert psd["episodes"]["epX"]["判决"] == "通过"       # 真没应用
    c = decision_status(load_delivery(run))["counts"]
    assert c["applied"] == 1 and c["unapplied"] == 0

    # 反向:判据说"都没落库" → 即便溯源已在,rejudge 也会重新应用,UI 报未应用
    monkeypatch.setattr(dec, "task_verdict_applied_in", lambda *a: "")
    s2 = run_rejudge(run, "/unused", {}, rerun_fn=None)
    assert s2["verdict_pass"] == ["epX"] and s2["unchanged"] == []
    c2 = decision_status(load_delivery(run))["counts"]
    assert c2["applied"] == 0 and c2["unapplied"] == 1


def test_drop_and_keep_decisions_skip_on_second_run(tmp_path):
    """弃用该条/维持原标注也走同一套幂等(2026-08-16 起):第二趟 rejudge 记
    unchanged,不再在报告里把同一次人工弃用重数一遍。"""
    p, r, j = _views()
    r["标注-画面分歧复核队列"] = [
        {"id": "epX", "label": "old-x", "caption": "cap-x"},
        {"id": "epY", "label": "old-y", "caption": "cap-y"}]
    run = _mk_run(tmp_path, views=(p, r, j),
                  decision_rows=(f"epX,弃用该条,,,{AT_OLD}\n"
                                 f"epY,维持原标注,,,{AT_OLD}\n"))
    first = run_rejudge(run, "/unused", {}, rerun_fn=None)
    assert first["dropped"] == ["epX"] and first["kept"] == ["epY"]
    second = run_rejudge(run, "/unused", {}, rerun_fn=None)
    assert set(second["unchanged"]) == {"epX", "epY"}
    assert second["dropped"] == [] and second["kept"] == []


# ───────── 三处显示(报告页顶部 / 总览小字 / 裁决卡片)─────────


def _manifest(run):
    from curation.ui.manifest import load_delivery
    return load_delivery(run)


def test_all_three_displays_silent_without_decisions(tmp_path):
    """无裁决时三处显示**全部不出现**:空提示占着位置,只会让人以为漏看了什么。"""
    from curation.ui.manifest import (application_counts_md, carryover_note_md,
                                      decision_trace_md, unapplied_banner_md,
                                      unapplied_card_note)
    run = _mk_run(tmp_path)
    m = _manifest(run)
    assert unapplied_banner_md(m) == ""
    assert carryover_note_md(m) == ""
    assert decision_trace_md(m, "verdict", "epX") == ""
    assert application_counts_md(run) == ""
    assert unapplied_card_note(run) == ""


def test_banner_wording_pure_machine_vs_partially_applied(tmp_path):
    """报告页顶部提醒:一条都没应用才说「纯机器结论」;应用了一部分再那么说
    就是假话,改说还差几条。"""
    from curation.ui.manifest import unapplied_banner_md
    run = _mk_run(tmp_path, verdict_rows=f"epX,判成功,,{AT_OLD}\n")
    banner = unapplied_banner_md(_manifest(run))
    assert "尚未应用" in banner and "纯机器结论" in banner and "1" in banner
    run_rejudge(run, "/unused", {}, rerun_fn=None)      # 应用掉 → 提醒消失
    assert unapplied_banner_md(_manifest(run)) == ""
    # 应用了一部分、又添了新裁决:不许再说「纯机器结论」,改说还差几条
    hd = tmp_path / "deliv" / "human-decisions"
    old = (hd / "task_verdicts.csv").read_text(encoding="utf-8")
    (hd / "task_verdicts.csv").write_text(old + f"epY,判失败,,{AT_NEW}\n",
                                          encoding="utf-8")
    mixed = unapplied_banner_md(_manifest(run))
    assert "尚未应用" in mixed and "已应用" in mixed and "纯机器结论" not in mixed


def test_overview_reconciliation_table_gains_no_row(tmp_path):
    """★ 沿用计数只进表下小字区,**绝不进对账表**:那张表的口径是
    「输入 = 判废 + 精确去重删除 + 交付」,加一行沿用就破了对账。
    钉法:同一份交付,有沿用与把裁决记录整个拿走之后,表与口径小字逐字相同 ——
    沿用信息只活在 carryover_note_md 那一行小字里。"""
    from curation.ui.manifest import (carryover_note_md, overview_note_md,
                                      overview_rows)
    run = _mk_run(tmp_path, verdict_rows=f"epX,判成功,,{AT_OLD}\n")
    run_rejudge(run, "/unused", {}, rerun_fn=None)      # 裁决落库(= 沿用出现)
    m = _manifest(run)
    carry = carryover_note_md(m)
    assert "沿用" in carry and "**1** 条" in carry and "改变了结果" in carry
    rows_with, note_with = overview_rows(m), overview_note_md(m)
    assert all("沿用" not in str(c) for row in rows_with for c in row)
    assert "沿用" not in note_with
    os.remove(tmp_path / "deliv" / "human-decisions" / "task_verdicts.csv")
    m2 = _manifest(run)                                  # 沿用消失……
    assert carryover_note_md(m2) == ""
    assert overview_rows(m2) == rows_with                # ……表却一行未动
    assert overview_note_md(m2) == note_with


def test_card_trace_line_wording(tmp_path):
    """裁决卡片溯源行:沿用的写「你在 <时间> 裁过:<裁决>(本次沿用)」;还没
    执行的写「尚未应用」;落空的写「无处可施」—— 别让人对着落空裁决反复点执行。"""
    from curation.ui.manifest import decision_trace_md
    run = _mk_run(tmp_path,
                  verdict_rows=(f"epX,判成功,,{AT_OLD}\n"
                                f"epY,判失败,,{AT_OLD}\n"
                                f"epGone,判成功,,{AT_OLD}\n"))
    m = _manifest(run)
    assert "尚未应用" in decision_trace_md(m, "verdict", "epX")
    assert "无处可施" in decision_trace_md(m, "verdict", "epGone")
    run_rejudge(run, "/unused", {}, rerun_fn=None)
    m = _manifest(run)
    line = decision_trace_md(m, "verdict", "epX")
    assert f"你在 {AT_OLD} 裁过" in line and "判成功" in line and "本次沿用" in line


def test_console_counts_line_separates_orphaned(tmp_path):
    """任务台计数行:共 N / 已应用 M / 未应用 K;落空的单独说,不进未应用。"""
    from curation.ui.manifest import application_counts_md
    run = _mk_run(tmp_path,
                  verdict_rows=(f"epX,判成功,,{AT_OLD}\n"
                                f"epGone,判失败,,{AT_OLD}\n"))
    line = application_counts_md(run)
    assert "共 2 条" in line and "已应用 0 条" in line and "未应用 1 条" in line
    assert "1 条无处可施" in line
    run_rejudge(run, "/unused", {}, rerun_fn=None)
    line2 = application_counts_md(run)
    assert "已应用 1 条" in line2 and "未应用 0 条" in line2
    assert "1 条无处可施" in line2                        # 落空的不会被"应用"消化掉


def test_finished_run_card_reminds_about_unapplied(tmp_path):
    """跑批完成的任务卡片:有未应用裁决时提一句(那是离「忘记执行」最近的时刻);
    应用完就闭嘴。卡片定位靠 argv 的 --output/--run-name,拿不准宁可不提醒。"""
    from curation.ui import runner
    from curation.ui.manifest import unapplied_card_note
    run = _mk_run(tmp_path, verdict_rows=f"epX,判成功,,{AT_OLD}\n")
    st = {"command": "run", "state": "done",
          "argv": ["python", "-m", "curation.cli", "run",
                   "--input", "/x", "--output", str(tmp_path / "deliv"),
                   "--run-name", RUN_NAME]}
    assert runner.run_output_dir(st) == run
    note = unapplied_card_note(run)
    assert "1 条人工裁决" in note and "尚未应用" in note
    html = runner.status_html(st, extra=note)
    assert "尚未应用" in html
    assert runner.run_output_dir({"command": "rejudge", "argv": []}) is None
    run_rejudge(run, "/unused", {}, rerun_fn=None)
    assert unapplied_card_note(run) == ""


def test_records_cache_invalidates_when_csv_grows(tmp_path):
    """run_decision_records 的 mtime 缓存必须在裁决文件变化后失效:任务台 2 秒
    一轮询靠它省 IO,但省成"新裁决三分钟后才显示"就是另一个事故。"""
    run = _mk_run(tmp_path, verdict_rows=f"epX,判成功,,{AT_OLD}\n")
    assert dec.application_counts(dec.run_decision_records(run))["total"] == 1
    hd = tmp_path / "deliv" / "human-decisions"
    old = (hd / "task_verdicts.csv").read_text(encoding="utf-8")
    (hd / "task_verdicts.csv").write_text(old + f"epY,判失败,,{AT_NEW}\n",
                                          encoding="utf-8")
    assert dec.application_counts(dec.run_decision_records(run))["total"] == 2
