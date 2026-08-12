"""被拒理由的单一事实源(2026-08-11 用户定稿)。

事故:ep000018 的横幅只写「未通过「时间戳检查」」,用户第一反应是同步出了问题——
实际是 0.47 秒的采集残段。理由必须带上**该检查自己写的那句人话**,而且报告、
reject.json、UI 横幅、左清单**引用同一个串**(禁止各拼各的)。
"""
from __future__ import annotations

import json

from curation.export.report import (check_detail_reason, hard_fail_reason,
                                    save_report, to_markdown)
from curation.pipeline.verdict import episode_verdict

CFG = {"checks": {"timestamp_check": {"enable": True, "gate": "hard"},
                  "motion_quality": {"enable": True, "gate": "soft", "weight": 1.0}},
       "verdict": {"soft_threshold": 0.5}}

STUB_REASON = "全长只有 0.47 秒(不足 1 秒,疑似采集中断的碎片)"


def test_hard_fail_reason_carries_the_checks_own_words():
    """格式 = 未通过「检查中文名」:该检查写的人话。detail 是 JSON 串也要认。"""
    checks = {"timestamp_check": {"passed": False,
                                  "detail": json.dumps({"reason": STUB_REASON,
                                                        "duration_s": 0.467})}}
    line = hard_fail_reason(["timestamp_check"], checks)
    assert line == f"未通过「时间戳检查」:{STUB_REASON}"
    assert "硬门" not in line and "timestamp_check" not in line
    # dict 形态的 detail 同样吃
    assert check_detail_reason({"detail": {"reason": STUB_REASON}}) == STUB_REASON
    # 检查没写理由:只报检查名,**绝不编**
    assert hard_fail_reason(["timestamp_check"], {"timestamp_check": {}}) == \
        "未通过「时间戳检查」"
    # 多维同时投拒绝:逐条列全,不合并成一句糊话
    two = hard_fail_reason(["timestamp_check", "kinematic_limits"],
                           {"timestamp_check": {"detail": {"reason": STUB_REASON}},
                            "kinematic_limits": {"detail": {"reason": "关节 3 超限"}}})
    assert two == f"未通过「时间戳检查」:{STUB_REASON};未通过「运动学极限」:关节 3 超限"


def test_verdict_reason_is_the_single_source(tmp_path):
    """判决层拼一次,reject.json 与 report.md 的淘汰明细都引用它(不各拼各的)。"""
    checks = {"timestamp_check": {"passed": False, "score": None,
                                  "detail": json.dumps({"reason": STUB_REASON})},
              "motion_quality": {"passed": None, "score": 0.9, "detail": ""}}
    v = episode_verdict(checks, CFG)
    assert v["verdict"] == "drop"
    assert v["reason"] == f"未通过「时间戳检查」:{STUB_REASON}"

    report = {"数据集": "ds", "机器人": {"robot_type": "franka"},
              "生成时间": "t", "代码版本": "c",
              "dataset": {"input_episodes": 1, "hard_gate_filtered": 0,
                          "verdict_keep": 0, "verdict_drop": 1, "dedup_removed": 0,
                          "delivered": 0, "hard_fail_breakdown": {"timestamp_check": 1},
                          "funnel_stats": {"input": 1, "output": 1}},
              "skills": {}, "episodes": {
                  "kept": {}, "duplicates": [],
                  "dropped": {"ep000018": {"reason": v["reason"], "soft_score": None}},
                  "results": {"ep000018": {"verdict": "drop", "reason": v["reason"],
                                           "soft_score": None, "checks": checks}}}}
    md = to_markdown(report)
    # 淘汰明细那一行 = 判决层拼的同一句(此前这里漏的是英文检查名 timestamp_check)
    assert f"- ep000018: 未通过「时间戳检查」:{STUB_REASON}" in md
    assert "硬门违规: timestamp_check" not in md
    # (报告其余小节的措辞清扫是另一单的范围,本单只管被拒理由这一句)
    save_report(report, str(tmp_path))
    rj = json.loads((tmp_path / "reject.json").read_text(encoding="utf-8"))
    assert rj["episodes"]["ep000018"]["原因"] == f"未通过「时间戳检查」:{STUB_REASON}"


def test_ui_banner_and_list_quote_the_same_string(tmp_path):
    """UI 横幅只**引用**交付里的那句(不自拼);左清单取其中的人话部分。"""
    from curation.ui.manifest import (BUCKET_ALL, episode_card_html,
                                      episode_list_items, episode_reason_line,
                                      load_delivery)
    d = tmp_path / "dlv"
    (d / "details").mkdir(parents=True)
    (d / "passed.json").write_text(json.dumps(
        {"数据集": "ds", "dataset": {}, "episodes": {}}, ensure_ascii=False),
        encoding="utf-8")
    (d / "reject.json").write_text(json.dumps({"episodes": {"ep000018": {
        "判决": "拒绝", "原因": f"未通过「时间戳检查」:{STUB_REASON}",
        "checks": {"时间戳检查": {"结果": "拒绝",
                                  "detail": json.dumps({"reason": STUB_REASON})}}}}},
        ensure_ascii=False), encoding="utf-8")
    m = load_delivery(str(d))
    line = episode_reason_line(m, "ep000018")
    assert line == f"未通过「时间戳检查」:{STUB_REASON}"      # 一字不差地引用
    assert line in episode_card_html(m, "ep000018")
    # 清单行与横幅**故意解耦**:列窄,前缀会把"怎么了"挤出视野 → 清单只放人话
    label = episode_list_items(m, BUCKET_ALL)[0]["label"]
    assert label.startswith("ep000018 ❌ 全长只有 0.47 秒")
    assert "未通过「" not in label


def test_legacy_delivery_gets_the_words_appended(tmp_path):
    """老交付只写了「硬门违规: 「X」」:UI 补上检查的人话,并抹掉"硬门"黑话。"""
    from curation.ui.manifest import episode_reason_line, load_delivery
    d = tmp_path / "old"
    (d / "details").mkdir(parents=True)
    (d / "passed.json").write_text('{"数据集": "ds", "dataset": {}, "episodes": {}}',
                                   encoding="utf-8")
    (d / "reject.json").write_text(json.dumps({"episodes": {"ep000018": {
        "判决": "拒绝", "原因": "硬门违规: 「时间戳检查」",
        "checks": {"时间戳检查": {"结果": "拒绝",
                                  "detail": json.dumps({"reason": STUB_REASON})}}}}},
        ensure_ascii=False), encoding="utf-8")
    line = episode_reason_line(load_delivery(str(d)), "ep000018")
    assert line == f"未通过「时间戳检查」:{STUB_REASON}"
    assert "硬门" not in line
