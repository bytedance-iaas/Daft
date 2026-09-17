"""轨迹页「检查明细」单条视图(2026-09-16 用户定):要点=判定链、多一行打标、按检查分块。"""
from __future__ import annotations

import json

import pytest

from curation.ui.episode_detail import (LABEL_ROW_NAME, LABEL_STATE_DECIDED,
                                        LABEL_STATE_DISAGREE, LABEL_STATE_NO_LABEL,
                                        episode_timeline_html, kinematics_html, label_row,
                                        motion_subdims_html, sync_caption_html,
                                        task_chain_gist, visual_cameras_html)
from curation.ui.manifest import check_rows, check_table_html, load_delivery

MOTION = {"accel_rms": 7.55, "path_len": 18.8, "spike_frames": [], "spike_peak": 5.49,
          "spike_isolation": 2.93, "tail_std": 0.068, "gripper_reason": "本数据集没有夹爪列",
          "stuck_strategy": "cmd_delta_vs_pos", "active_ratio": 1.0, "idle_head_s": 0.0,
          "idle_tail_s": 0.0, "smoothness": 0.811, "path_efficiency": 0.0092, "spike": 1.0,
          "joint_stability": 0.4726, "gripper_jitter": None, "actuator_saturation": None,
          "saturation_reason": "指令与读数同源(指令即下一帧读数),无法评估执行响应",
          "same_source": True, "stuck": None, "stuck_reason": "指令与读数同源", "fluency": 1.0}
VISUAL = {"sharpness": 0.95, "exposure": 1.0, "integrity": 1.0, "n_frames": 97,
          "per_camera": {"cam_a": {"score": 1.0, "sharpness": 1.0, "exposure": 1.0,
                                   "integrity": 1.0, "frozen_ratio": 0.0, "status": "OK"},
                         "cam_b": {"score": 0.75, "sharpness": 0.75, "exposure": 1.0,
                                   "integrity": 1.0, "frozen_ratio": 0.0, "status": "OK"}}}
SYNC = {"verdict": "aligned", "per_camera": {"cam_a": {"lag_s": 0.1, "corr_peak": 0.63,
                                                       "corr_at_zero": 0.54, "trusted": True},
                                             "cam_b": {"lag_s": -0.03, "corr_peak": 0.3,
                                                       "corr_at_zero": 0.3, "trusted": False}}}
TASK_REJECT = {"voc": 0.82, "completion_final": 0.55, "verdict": "arbitration_failure",
               "reason": "取证仲裁:2 条有效取证路一致判未完成(≥2 路相互印证)"}


@pytest.fixture
def delivery(tmp_path):
    d = tmp_path / "umi-fake"
    (d / "details" / "plots").mkdir(parents=True)
    (d / "details" / "plots" / "ep000002_sync.png").write_bytes(b"\x89PNGfake")

    def ep(task_state, task_detail, verdict):
        return {"判决": verdict, "综合软分": 0.79, "checks": {
            "时间戳检查": {"结果": "pass", "detail": json.dumps({"n": 1444, "duration_s": 48.1})},
            "运动质量": {"结果": "软分", "score": 0.9, "detail": json.dumps(MOTION)},
            "视觉质量": {"结果": "软分", "score": 0.97, "detail": json.dumps(VISUAL)},
            "视频-动作同步": {"结果": "pass", "detail": json.dumps(SYNC)},
            "任务成败判定": {"结果": task_state, "detail": json.dumps(task_detail)}}}

    (d / "passed.json").write_text(json.dumps({
        "dataset": {"input_episodes": 3, "hard_gate_filtered": 0, "verdict_keep": 2,
                    "verdict_drop": 1, "dedup_removed": 0, "delivered": 2,
                    "hard_fail_breakdown": {"task_success": 1},
                    "container": {"format": "lerobot", "findings": [
                        {"项": "机器人型号", "状态": "已跳过",
                         "说明": "型号 umi_dual_handheld_gripper 不在规格库,运动学极限整项跳过(其余检查照常)"}]}},
        "episodes": {"ep000001": ep("pass", {"verdict": "success", "reason": ""}, "通过"),
                     "ep000003": ep("pass", {"verdict": "success", "reason": ""}, "通过")},
        "skills": {"families": {"deformable": {"name_zh": "柔性物操作", "count": 2, "pct": 66.7,
                                              "subskills": {"fold": {"name_zh": "折叠", "count": 2, "pct": 66.7}}}}},
    }, ensure_ascii=False))
    (d / "reject.json").write_text(json.dumps({
        "episodes": {"ep000002": dict(ep("拒绝", TASK_REJECT, "拒绝"),
                                       **{"原因": "未通过「任务成败判定」:" + TASK_REJECT["reason"]})}},
        ensure_ascii=False))
    (d / "review.json").write_text(json.dumps({
        "episodes": {},
        "标注-画面分歧复核队列": [
            {"id": "ep000003", "label": "Open the airfryer", "caption": "press the plunger",
             "reason": "分歧(文本对判官):两者不是同一任务", "priority": "重点"}]},
        ensure_ascii=False))
    (d / "details" / "task_details.json").write_text(json.dumps({"episodes": {
        "ep000002": {"episode_id": "ep000002", "result": "拒绝", "instruction": "Tidy up the bed",
                     "instruction_source": "原始标注", "init_verdict": "success",
                     "verdict": "arbitration_failure", "reason": TASK_REJECT["reason"],
                     "rules": ["success_candidate_weak", "weak_success_vetoed_by_review",
                               "arbitration_kill_double_signed"],
                     "scoring": {"voc": 0.82, "strong_score": False},
                     "review": {"cam_votes": {"cam_a": "no", "cam_b": "no"}, "tally": "no"},
                     "arbitration": {"applied": True, "intent_source": "原始标注",
                                     "n_effective": 2, "consensus": "no"}},
        "ep000001": {"episode_id": "ep000001", "result": "pass", "instruction": "fold the shirt",
                     "instruction_source": "自产caption", "init_verdict": "success",
                     "verdict": "success", "reason": "", "rules": ["success_strong"],
                     "scoring": {"voc": 0.95, "strong_score": True},
                     "review": {"cam_votes": {"cam_a": "yes", "cam_b": "yes"}, "tally": "yes"}},
    }}, ensure_ascii=False))
    (d / "details" / "skill_assignment.csv").write_text(
        "episode_id,family,subskill,caption,grouping_text,grouping_text_source\n"
        "ep000001,deformable,fold,fold the shirt,fold the shirt,自产caption\n"
        "ep000003,deformable,fold,press the plunger,Open the airfryer,原始标注\n")
    (d / "details" / "episodes_timeline.json").write_text(json.dumps({"episodes": {
        "ep000002": {"duration_s": 10.0, "segments": [
            {"start_s": 0.0, "end_s": 1.0, "state": "idle"},
            {"start_s": 1.0, "end_s": 10.0, "state": "normal"}],
            "totals": {"stuck": 0.0, "idle": 1.0, "normal": 9.0}}}}))
    return d


def test_task_chain_gist_reads_like_a_verdict_path(delivery):
    m = load_delivery(delivery)
    g = task_chain_gist(m, "ep000002")
    assert g.startswith("打分层成功候选(弱) → 2 路复核一致判未完成 → 仲裁 2 路一致未完成 → 判废")
    assert "意图取原始标注" in g and "voc=" not in g
    assert task_chain_gist(m, "ep000001").startswith("打分层成功候选(强) → 2 路复核一致判完成 → 通过")


def test_check_rows_have_concise_gists_and_a_label_row(delivery):
    m = load_delivery(delivery)
    rows = {r[0]: r for r in check_rows(m, "ep000002")}
    assert "总分=平滑度 0.81、尖刺 1.00 的均值;不适用:夹爪抖动、执行器饱和(指令与读数同源)" == rows["运动质量"][3]
    assert rows["视觉质量"][3].startswith("2 路相机;最低 0.75")
    assert "1/2 路可信" in rows["视频-动作同步"][3] and "+0.10/-0.03" in rows["视频-动作同步"][3]
    assert rows["时间戳检查"][3] == "1444 帧 48.1 秒,帧间隔稳定"
    assert rows[LABEL_ROW_NAME][2] == ""
    html = check_table_html(m, "ep000002")
    assert html.count("#FFECE8") == 1


def test_label_row_states(delivery):
    m = load_delivery(delivery)
    r3 = label_row(m, "ep000003")            # 在分歧队列里,未裁
    assert r3[1] == LABEL_STATE_DISAGREE and "柔性物操作 › 折叠" in r3[3] and "不是同一任务" in r3[3]
    r1 = label_row(m, "ep000001")            # 没标注,用自产描述
    assert r1[1] == LABEL_STATE_NO_LABEL and "自产描述:fold the shirt" in r1[3]
    (delivery / "human-decisions").mkdir()
    (delivery / "human-decisions" / "label_decisions.csv").write_text(
        "episode_id,decision,new_label,note,at\nep000003,维持原标注,,,2026-09-16 10:00:00\n")
    m = load_delivery(delivery)
    r3 = label_row(m, "ep000003")
    assert r3[1] == LABEL_STATE_DECIDED and "人工:维持原标注" in r3[3]


def test_blocks_render_only_what_this_episode_has(delivery):
    m = load_delivery(delivery)
    mo = motion_subdims_html(m, "ep000002")
    assert "运动质量子项" in mo and "计入总分" in mo and "不适用" in mo and "同源" in mo
    vi = visual_cameras_html(m, "ep000002")
    assert "视觉质量(逐相机)" in vi and "0.75" in vi
    tl = episode_timeline_html(m, "ep000002")
    assert "卡顿与空闲时间线" in tl and "ep000002 · 10.0s" in tl
    assert episode_timeline_html(m, "ep000001") == ""        # 没有时间线的条目不占位
    kin = kinematics_html(m, "ep000002")
    assert "本次未跑运动学极限" in kin and "不在规格库" in kin
    assert "视频-动作同步曲线" in sync_caption_html()


def test_gists_tolerate_old_delivery_shapes():
    """老交付:视觉 per_camera 的值是一个数、同步 per_camera 缺字典 —— 要点不崩、给降级说法。"""
    from curation.ui.episode_detail import sync_gist, visual_gist
    assert visual_gist({"per_camera": {"cam_a": 0.9, "cam_b": 0.7}}).startswith("2 路相机;最低 0.70(")
    assert visual_gist({}) == ""
    assert sync_gist({"verdict": "aligned", "per_camera": {"cam_a": 0.3}}) == "同步正常"
