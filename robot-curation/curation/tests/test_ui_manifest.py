"""UI 数据层测试(2026-07-27 U1)。

契约:manifest.py 是"交付目录 → UI"的唯一读端,纯函数无副作用;
fixture 按 U0 定型的真实交付 schema 造(passed/reject/review 三 JSON +
details/evidence 帧 + details/plots 同步图),UI 逻辑全部在此测,
Gradio 层只剩摆组件(构造冒烟测试放最后,无 gradio 环境自动跳过)。
"""
from __future__ import annotations

import json
import os

import pytest

from curation.ui.manifest import (audit_rows, check_rows, discover_deliveries,
                                  episode_rows, funnel_rows, load_delivery,
                                  overview_markdown, parse_detail, skill_rows)

TS_DETAIL = json.dumps({"voc": 0.87, "completion_final": 0.3,
                        "probe_frames": [0, 4], "verdict": "endstate_failure_suspect",
                        "reason": "渐变问询不可判", "task_desc": "arrange the blanket"})


@pytest.fixture
def delivery(tmp_path):
    d = tmp_path / "droid-fake"
    (d / "details" / "evidence" / "ep000000").mkdir(parents=True)
    (d / "details" / "plots").mkdir(parents=True)
    (d / "details" / "evidence" / "ep000000" / "probe0_f0.jpg").write_bytes(b"\xff\xd8fake")
    (d / "details" / "evidence" / "ep000000" / "probe1_f4.jpg").write_bytes(b"\xff\xd8fake")
    (d / "details" / "plots" / "ep000001_sync.png").write_bytes(b"\x89PNGfake")

    (d / "passed.json").write_text(json.dumps({
        "数据集": "droid_fake", "机器人": "franka",
        "生成时间": "2026-07-27 08:00:00", "代码版本": "abc1234",
        "dataset": {"input_episodes": 3, "hard_gate_filtered": 0,
                    "verdict_keep": 2, "verdict_drop": 1, "dedup_removed": 0,
                    "delivered": 2, "hard_fail_breakdown": {"task_success": 1},
                    "summary_stats": {"pass_rate_pct": 66.7, "avg_soft_score": 0.91}},
        "config_effective": {"checks": {"task_success": {"vlm": {"model": "doubao"}}}},
        "skills": {"n_episodes": 2, "families": {
            "Arrange": {"count": 2, "pct": 100.0, "criterion": "整理类",
                        "subskills": {"Arrange soft goods": {
                            "count": 2, "pct": 100.0, "criterion": "软物归位"}}}}},
        "label_audit": {"high": []},
        "episodes": {
            "ep000000": {"判决": "通过", "综合软分": 0.93, "checks": {
                "任务成败判定": {"结果": "弃权", "detail": TS_DETAIL},
                "运动质量": {"结果": "软分", "score": 0.86}}},
            "ep000002": {"判决": "通过", "综合软分": 0.89, "checks": {
                "任务成败判定": {"结果": "pass", "detail": TS_DETAIL}}}},
    }, ensure_ascii=False))
    (d / "reject.json").write_text(json.dumps({
        "被拒总数": 1, "episodes": {
            "ep000001": {"判决": "拒绝", "原因": "硬门违规: 「任务成败判定」",
                         "综合软分": 0.94, "checks": {
                             "任务成败判定": {"结果": "拒绝", "detail": TS_DETAIL}}}}},
        ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "待人工裁决总数": 1,
        "episodes": {"ep000000": {"当前判决": "通过", "待裁决项": ["任务成败判定"],
                                  "弃权原因": {"任务成败判定": "渐变问询不可判"}}},
        "标注审计复核队列": [{"id": "ep000002", "label": "Open the door",
                              "caption": "put the pot", "reason": "跨族"}]},
        ensure_ascii=False))
    return str(d)


def test_parse_detail_variants():
    assert parse_detail('{"voc": 0.9}') == {"voc": 0.9}
    assert parse_detail({"voc": 0.9}) == {"voc": 0.9}          # 已是 dict 直通
    assert parse_detail("not json")["raw"] == "not json"       # 解不开保原文
    assert parse_detail(None) == {} and parse_detail("") == {}


def test_load_merges_three_jsons(delivery):
    m = load_delivery(delivery)
    assert set(m["episodes"]) == {"ep000000", "ep000001", "ep000002"}
    assert m["episodes"]["ep000001"]["verdict"] == "拒绝"
    assert m["episodes"]["ep000001"]["reject_reason"].startswith("硬门违规")
    assert m["episodes"]["ep000000"]["pending"] == ["任务成败判定"]
    # detail 已解开成 dict,VLM 理由可直接取
    assert m["episodes"]["ep000001"]["checks"]["任务成败判定"]["detail"]["voc"] == 0.87


def test_load_discovers_evidence_and_plots(delivery):
    m = load_delivery(delivery)
    assert len(m["episodes"]["ep000000"]["evidence"]) == 2
    assert m["episodes"]["ep000000"]["evidence"][0].endswith("probe0_f0.jpg")
    assert m["episodes"]["ep000001"]["plot"].endswith("ep000001_sync.png")
    assert m["episodes"]["ep000002"]["evidence"] == []
    assert m["episodes"]["ep000002"]["plot"] is None


def test_episode_rows_shape(delivery):
    rows = episode_rows(load_delivery(delivery))
    assert [r[0] for r in rows] == ["ep000000", "ep000001", "ep000002"]
    r0 = rows[0]
    assert r0[1] == "通过" and r0[3] == "任务成败判定" and r0[5] == 2   # 2 张证据帧
    assert rows[1][6] == "有"                                          # 同步图


def test_funnel_and_check_rows(delivery):
    m = load_delivery(delivery)
    fr = dict(funnel_rows(m))
    assert fr["输入 episode"] == 3 and fr["交付"] == 2
    cr = check_rows(m, "ep000001")
    ts = [r for r in cr if r[0] == "任务成败判定"][0]
    assert ts[1] == "拒绝" and "voc=0.87" in ts[3]


def test_skill_audit_overview(delivery):
    m = load_delivery(delivery)
    sk = skill_rows(m)
    assert sk[0][0] == "Arrange" and sk[0][1] == "Arrange soft goods" and sk[0][2] == 2
    au = audit_rows(m)
    assert au[0][0] == "ep000002" and au[0][3] == "跨族"
    md = overview_markdown(m)
    assert "droid_fake" in md and "franka" in md and "待人工裁决 **1** 条" in md


def test_discover_deliveries(delivery, tmp_path):
    assert discover_deliveries(delivery) == [delivery]          # 单交付:就是它
    root = os.path.dirname(delivery)
    assert discover_deliveries(root) == [delivery]              # 父目录:扫出子交付
    assert discover_deliveries(str(tmp_path / "空目录不存在")) == []


def test_load_tolerates_legacy_delivery(tmp_path):
    """U0 之前的老交付(无 config_effective/evidence)也要能打开,不许炸。"""
    d = tmp_path / "old"
    d.mkdir()
    (d / "passed.json").write_text(json.dumps({
        "数据集": "old", "episodes": {"ep0": {"判决": "通过", "checks": {}}}}))
    m = load_delivery(str(d))
    assert m["config_effective"] is None
    assert m["episodes"]["ep0"]["evidence"] == []
    assert funnel_rows(m) == []                                 # 无统计=空表,不炸


def test_app_builds_without_server(delivery):
    """Gradio 冒烟:四 tab 构造成功即可(不 launch)。无 gradio 环境自动跳过。"""
    pytest.importorskip("gradio")
    from curation.ui.app import build_app
    app = build_app(delivery)
    assert app is not None
