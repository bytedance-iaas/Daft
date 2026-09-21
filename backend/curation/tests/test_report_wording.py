"""report.md 的措辞:客户拿到的报告要和界面说同一种话(2026-08-14 用户点名)。

事故形状不是崩溃,是**同一件事两套说法**:界面 2026-08-11 就把「软分」统一叫
「质量分」、2026-08-13 又把总览合成一张表(口径 = 输入 = 判废 + 精确去重删除 +
交付),而交付给客户的 report.md 里还写着「硬门拦截(漏斗中途淘汰)」「判决
keep / drop」「软分」「杀」—— 客户对着两份材料读同一批数字,两边对不上。

⚠️ 本文件只钉**人看的字**。JSON 的键名与数值口径是数据契约(passed/reject/
review.json),一个字都不许跟着改 —— 最后两条用例专门守这条线。
"""
from __future__ import annotations

import json

from curation.export.report import (DROP_SOFT_LABEL, build_report,
                                    drop_breakdown, save_report, to_markdown)

VERDICTS = {
    "ep000000": {"verdict": "keep", "reason": "", "hard_fails": [], "soft_score": 0.93},
    "ep000001": {"verdict": "keep", "reason": "", "hard_fails": [], "soft_score": 0.88},
    "ep000002": {"verdict": "drop", "reason": "未通过「时间戳检查」:全长只有 0.47 秒",
                 "hard_fails": ["timestamp_check"], "soft_score": None},
    "ep000003": {"verdict": "drop", "reason": "软分 0.412 < 0.5",
                 "hard_fails": [], "soft_score": 0.412},
}
STATS = {"input": 4, "after_numeric_gates": 3, "survivors_for_vlm": 3, "output": 3}


def _report(verdicts=None, dedup=()):
    r = build_report(verdicts or VERDICTS, STATS, list(dedup),
                     {"n_episodes": 2, "n_skills": 0, "skills": {}, "undersampled": []})
    r.update({"数据集": "droid_batch", "生成时间": "2026-08-14 10:00:00",
              "代码版本": "abc1234"})
    r["dataset"]["summary_stats"] = {
        "pass_rate_pct": 50.0, "avg_soft_score": 0.905,
        # 键是实现名 —— 真交付(droid-200-full)里就是这样,渲染时才翻成人话
        "per_check": {"visual_quality": {"avg_score": 0.93, "pass": 0, "fail": 0,
                                         "abstain": 0},
                      "timestamp_check": {"avg_score": None, "pass": 3, "fail": 1,
                                          "abstain": 0}}}
    r["episodes"]["results"] = {
        e: {"verdict": v["verdict"], "reason": v["reason"],
            "soft_score": v["soft_score"],
            "checks": {"timestamp_check": {"passed": v["verdict"] == "keep",
                                           "score": None, "detail": ""}}}
        for e, v in (verdicts or VERDICTS).items()}
    return r


#: 内部黑话清单。这些词出现在客户报告里就是缺陷,不是风格问题。
JARGON = ("漏斗", "软分", "硬门", "keep / drop", "淘汰")


def test_report_markdown_has_no_internal_jargon():
    """★ 报告正文里一个黑话都不许剩。

    「漏斗」是我们内部讲实现的词,「软分/硬门」是机制名,「keep/drop」是代码里的
    取值 —— 客户读到的应该是「质量分」「判废」「交付」。
    """
    md = to_markdown(_report())
    for bad in JARGON:
        assert bad not in md, f"报告里还留着黑话:{bad}"
    assert "| 交付 |" in md and "| 判废 |" in md      # 逐条表的判决列说人话
    assert "平均质量分" in md and "(质量分)" in md


def test_report_prints_check_names_the_way_the_ui_does():
    """检查名也得说人话:客户报告里印 `timestamp_check` 等于没写。

    汇总统计与逐条结果表的列名都来自实现名(json 里照旧,那是数据契约),渲染
    markdown 时统一过一次 CHECK_CN —— 界面从来都是「时间戳检查」。
    """
    md = to_markdown(_report())
    for en in ("timestamp_check", "visual_quality", "kinematic_limits"):
        assert en not in md, f"报告里还印着实现名:{en}"
    assert "- 时间戳检查(判废项)" in md and "- 视觉质量(质量分)" in md
    assert "| episode | 判决 | 质量分 | 时间戳检查 |" in md


def test_overview_uses_the_same_arithmetic_as_the_ui_table():
    """★ 总览的口径与界面那张总览表逐行对齐:输入 = 判废 + 精确去重删除 + 交付。

    此前这里还有一行「硬门拦截(漏斗中途淘汰)」,它和「判废」同名不同义(一个是
    中途被刷掉的,一个是最终判废的全部)—— 正是界面那次合表要消灭的自相矛盾。
    数字仍在 json 的 hard_gate_filtered 里,只是不再摆在客户眼前当第二个口径。
    """
    md = to_markdown(_report())
    assert "- 输入 episode:4" in md
    assert "- 判废:2" in md
    assert "- 精确去重删除:0" in md
    assert "- **交付:2 条**" in md
    assert "输入 = 判废 + 交付" in md
    assert "硬门拦截" not in md and "中途淘汰" not in md

    dup = to_markdown(_report(dedup=[{"episode_id": "ep000001",
                                      "duplicate_of": "ep000000"}]))
    assert "输入 = 判废 + 精确去重删除 + 交付" in dup   # 去重真删了就得写进等式


def test_overview_breakdown_covers_the_drops_without_a_check_name():
    """★ 判废子项要能加出总数:没有检查名的那一类补一行「综合质量分不达标」。

    只列 hard_fail_breakdown 的话,走综合加权分被判废的那条 episode 在子项里查无
    此人,报表上就是「判废 2,子项 1」自己打自己脸(与界面总览表同一条修正)。
    """
    items, overlap = drop_breakdown(_report()["dataset"])
    assert items == [("时间戳检查", 1), (DROP_SOFT_LABEL, 1)] and overlap is False
    md = to_markdown(_report())
    assert f"  - {DROP_SOFT_LABEL}:1" in md
    assert "  - 时间戳检查:1" in md


def test_overview_breakdown_stops_decomposing_when_items_overlap():
    """一条同时踩中两个硬门 → 子项相加大于总数,这时改口说「其中」并当场注明。"""
    v = dict(VERDICTS)
    v["ep000002"] = dict(v["ep000002"],
                         hard_fails=["timestamp_check", "kinematic_limits"])
    v["ep000003"] = dict(v["ep000003"], verdict="drop",
                         hard_fails=["timestamp_check"])
    r = _report(v)                       # 判废 2 条,子项相加却是 3
    items, overlap = drop_breakdown(r["dataset"])
    assert overlap is True and dict(items) == {"时间戳检查": 2, "运动学极限": 1}
    md = to_markdown(r)
    assert "同一条可能同时踩中多项" in md
    assert "  - 其中 时间戳检查:2" in md
    assert DROP_SOFT_LABEL not in md          # 已经多出来了,别再往上加


def test_drop_reason_lines_speak_the_same_words():
    """判废逐条那节:理由仍是判决层拼的那一句(单一事实源),只把黑话换成人话。"""
    md = to_markdown(_report())
    assert "## 判废明细(逐条)" in md
    assert "- ep000003: 质量分 0.412 < 0.5" in md
    assert "- ep000002: 未通过「时间戳检查」:全长只有 0.47 秒" in md


def test_json_keys_and_numbers_are_untouched(tmp_path):
    """★ 措辞清扫**只动 markdown**:三份 json 的键名、判决取值、数值全不动。

    passed/reject/review.json 的键是数据契约(rejudge、UI、客户的下游脚本都按它
    读),跟着报告的措辞一起改会静默打断所有读端。
    """
    r = _report()
    d = r["dataset"]
    assert d["verdict_keep"] == 2 and d["verdict_drop"] == 2
    assert d["hard_gate_filtered"] == 1 and d["delivered"] == 2
    assert d["hard_fail_breakdown"] == {"timestamp_check": 1}

    save_report(r, str(tmp_path))
    passed = json.loads((tmp_path / "passed.json").read_text(encoding="utf-8"))
    reject = json.loads((tmp_path / "reject.json").read_text(encoding="utf-8"))
    assert set(passed["dataset"]) >= {"verdict_keep", "verdict_drop",
                                      "hard_gate_filtered", "hard_fail_breakdown"}
    assert "综合软分" in passed["episodes"]["ep000000"]      # 键名原样
    assert reject["episodes"]["ep000003"]["判决"] == "拒绝"
    assert reject["episodes"]["ep000003"]["综合软分"] == 0.412
