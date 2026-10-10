"""Findings from the modules' answers (design doc 17 §1-§2, F12.2): every code of the registry's catalogue is
drawn from a real answer shape, every record 2.0 fits the contract, and the default levels keep today's gates."""
from __future__ import annotations

import json

import pytest

from curation.contracts import modules as registry
from curation.contracts import schemas
from curation.pipeline import findings as F
from curation.pipeline.records import is_v2, legacy_verdict, passes_funnel, record_from_struct, upgrade_record

# ---------------------------------------------------------------- answer shapes (exact outputs of the algorithms)

TS_PASS = {"n": 200, "duration_s": 19.9, "dt_nominal": 0.1, "max_dt": 0.1, "jitter_ratio": 0.0}
TS_GAP = {"n": 190, "duration_s": 19.9, "dt_nominal": 0.1, "max_dt": 1.1,
          "reason": "第 90 帧后间隔突增至 1.10 秒(正常约 0.10 秒),疑似丢帧", "gap_frames": [{"frame": 89, "dt": 1.1}]}
TS_FRAGMENT = {"n": 8, "duration_s": 0.467, "reason": "全长只有 0.47 秒(不足 1 秒,疑似采集中断的碎片)"}
TS_JITTER = {"n": 200, "duration_s": 21.1, "dt_nominal": 0.1, "max_dt": 0.14, "jitter_ratio": 0.1508}
TS_ORDER = {"n": 100, "reason": "时间戳出现倒退/重复(第 50 帧)", "frame": 49, "ts": [4.9, 4.9]}
TS_SINGLE = {"reason": "只有 1 个时间戳,连时长都算不出"}

KIN_JOINT = {"n_violations": 3, "violations": [
    {"type": "joint_limit", "joint": 3, "frame": 40, "value": 0.4302, "limit": [-3.0718, -0.0698]},
    {"type": "velocity_limit", "joint": 3, "frame": 39, "value": 27.2314, "limit": 2.175},
    {"type": "velocity_limit", "joint": 3, "frame": 40, "value": 27.7019, "limit": 2.175}],
    "profile": "franka", "unit": "rad", "margin": 0.02}
KIN_EE = {"mode": "ee", "n_violations": 3, "violations": [
    {"type": "ee_reach", "joint": "xyz", "frame": 50, "value": 2.0248, "limit": 1.4875},
    {"type": "ee_translation_velocity", "joint": "xyz", "frame": 49, "value": 22.4762, "limit": 1.87},
    {"type": "ee_rotation_velocity", "joint": "rpy", "frame": 59, "value": 22.5001, "limit": 2.75}],
    "profile": "franka", "reach_m": 1.19, "margin": 0.25}
KIN_DOF = {"reason": "关节数 6 与 franka 规格表的 7 个关节对不上(…)"}
KIN_DRAFT = {"reason": "agibot 极限表不可用于硬判(quality=draft),仅做了格式校验"}

MQ_STUCK = {"accel_rms": 0.3878, "spike_frames": [], "spike_isolation": 1.12,
            "gripper_reason": "本数据集没有夹爪列(规格档未配置 gripper_dims)", "saturation_gap_ratio": 0.1027,
            "stuck_strategy": "cmd_delta_vs_pos",
            "stuck_joints": [{"joint": 2, "axis": "z", "segment": 0, "max_dead_run": 44, "freeze_start_frame": 49,
                              "freeze_end_frame": 92, "envelope_start_frame": 45, "envelope_frames": 154}],
            "active_ratio": 1.0, "idle_head_s": 0.0, "idle_tail_s": 0.0, "idle_mid_count": 0, "idle_mid_total_s": 0,
            "smoothness": 0.8311, "path_efficiency": 0.1989, "spike": 1.0, "joint_stability": 1.0,
            "gripper_jitter": None, "actuator_saturation": 0.6221, "stuck": 0.0, "fluency": 1.0}
MQ_IDLE = {"spike_frames": [238, 239], "spike_isolation": 1190.77, "active_ratio": 0.7863, "idle_head_s": 2.0,
           "idle_tail_s": 0.97, "idle_mid_count": 1, "idle_mid_total_s": 1.3,
           "idle_mid_segments": [{"start_frame": 200, "start_s": 6.67, "dur_s": 1.3}],
           "smoothness": 0.1521, "spike": 0.0, "stuck": 1.0, "fluency": 0.4235, "actuator_saturation": 0.31,
           "saturation_axes": {"0": 0.9, "3": 1.4}, "gripper_jitter": 0.2, "gripper_flip_hz": [3.1],
           "path_efficiency": 0.5, "joint_stability": 1.0}
MQ_NO_STATE = {"accel_rms": 0.3878, "spike_frames": [], "gripper_reason": "本数据集没有夹爪列(规格档未配置 gripper_dims)",
               "active_ratio": 0.8543, "idle_head_s": 0.0, "idle_tail_s": 2.9, "smoothness": 0.8311,
               "path_efficiency": 0.1989, "spike": 1.0, "joint_stability": 1.0, "gripper_jitter": None,
               "actuator_saturation": None, "stuck": None, "fluency": 1.0}

VQ = {"sharpness": 1.0, "exposure": 1.0, "integrity": 0.0, "dead_ratio": 0.0, "frozen_ratio": 1.0, "n_frames": 12,
      "per_camera": {"observation.images.front": 1.0, "observation.images.wrist": 0.0},
      "per_camera_detail": {
          "observation.images.front": {"score": 1.0, "sharpness": 1.0, "exposure": 1.0, "integrity": 1.0, "frozen_ratio": 0.0},
          "observation.images.wrist": {"score": 0.0, "sharpness": 1.0, "exposure": 1.0, "integrity": 0.0, "frozen_ratio": 1.0},
          "observation.images.side": {"score": 0.2, "sharpness": 0.31, "exposure": 0.42, "integrity": 0.3, "frozen_ratio": 0.1}},
      "worst_camera": "observation.images.wrist", "padded_channels": ["observation.images.image_3"],
      "camera_liveness": {"live": ["observation.images.front", "observation.images.wrist"],
                          "dead_or_padded": ["observation.images.image_3"]}}

SYNC_ALL = {"verdict": "misaligned_all", "per_camera": {
    "front": {"lag_s": -0.6, "corr_peak": 0.9373, "trusted": True, "code": "misaligned"},
    "wrist": {"lag_s": -0.6, "corr_peak": 0.9343, "trusted": True, "code": "misaligned"}},
    "flagged_cameras": ["front", "wrist"], "suspect_cameras": [], "noisy_cameras": [], "abstained_cameras": [],
    "consensus_lag_s": -0.6, "n_cameras": 2, "n_trusted": 2, "reason": "全部 2/2 路可信相机一致指向 Δ=-0.60s"}
SYNC_ONE = {"verdict": "annotated", "per_camera": {
    "front": {"lag_s": 0.8, "corr_peak": 0.9423, "trusted": True, "code": "misaligned"},
    "wrist": {"lag_s": 0.0, "corr_peak": 0.9505, "trusted": True, "code": "aligned"}},
    "flagged_cameras": ["front"], "suspect_cameras": [], "noisy_cameras": [], "abstained_cameras": [],
    "consensus_lag_s": None, "n_cameras": 2, "n_trusted": 2, "reason": "相机间矛盾"}
SYNC_WEAK = {"verdict": "undecidable", "per_camera": {
    "front": {"lag_s": -0.8, "corr_peak": 0.147, "trusted": False, "code": "low_corr"},
    "wrist": {"lag_s": -0.5, "corr_peak": 0.1374, "trusted": False, "code": "low_corr"}},
    "flagged_cameras": [], "suspect_cameras": [], "noisy_cameras": [], "abstained_cameras": ["front", "wrist"],
    "consensus_lag_s": None, "n_cameras": 2, "n_trusted": 0, "reason": "2 路相机均未给出可信读数"}
SYNC_SUSPECT = {"verdict": "annotated", "per_camera": {
    "ext": {"lag_s": 0.7, "corr_peak": 0.5, "trusted": False, "code": "flat_peak"}},
    "flagged_cameras": [], "suspect_cameras": ["ext"], "noisy_cameras": [], "abstained_cameras": ["ext"],
    "n_cameras": 1, "n_trusted": 0, "reason": "疑似"}
SYNC_NONE = {"verdict": "undecidable", "per_camera": {}, "n_cameras": 0, "reason": "没有状态量,无法比对"}

TASK_FAIL = {"verdict": "failure", "init_verdict": "failure", "review": "no", "cam_votes": {"front": "no"},
             "rules": ["video_double_signed_failure"], "video_completion": 0.2, "input_mode": "video",
             "protocol": "video-task/1", "task_desc_source": "原始标注", "reason": "杯子没有放进水槽",
             "video_assessment": {"verdict": "failure", "completion": 0.2, "reason": "杯子掉在台面",
                                  "evidence": [{"camera": "observation.images.front", "start_s": 4.2, "end_s": 6.0,
                                                "observation": "杯子从夹爪滑落"}]}}
TASK_UNSURE = {"verdict": "uncertain", "init_verdict": "success", "review": "split", "rules": [],
               "video_completion": 0.7, "input_mode": "video", "protocol": "video-task/1",
               "task_desc_source": "自产caption", "reason": "复核意见不一致"}
TASK_HELD = {"verdict": "label_conflict_suspect", "init_verdict": "failure", "review": "no",
             "rules": ["kill_held_label_conflict"], "input_mode": "video", "protocol": "video-task/1",
             "task_desc_source": "原始标注",
             "label_check": {"annotation": "pour rice into the cup", "caption": "pour rice into the green bowl",
                             "outcome": "different"}}
TASK_OK_FRAMES = {"verdict": "recovery", "rules": ["recovery_dip"], "dip": 0.31, "completion_final": 0.95,
                  "task_desc_source": "无"}

CAM = {"protocol": "camera-check/1", "cams": ["observation.images.a", "observation.images.b"], "known": 6,
       "clean_ratio": 0.5, "reason": "", "items": {"glitch": "severe", "shake": "unknown", "contamination": "minor"},
       "per_camera": {
           "observation.images.a": {"answered": True, "glitch": {"level": "minor", "times": [[1.0, 1.5]]},
                                    "shake": {"level": "unknown"}, "contamination": {"level": "none", "kind": "none"}},
           "observation.images.b": {"answered": True, "glitch": {"level": "severe", "times": []},
                                    "shake": {"level": "unknown"},
                                    "contamination": {"level": "minor", "kind": "dirt", "times": [[0.0, 9.0]]}}}}
CAM_SHAKE = {"protocol": "camera-check/1", "reason": "", "items": {"glitch": "none", "shake": "severe", "contamination": "none"},
             "per_camera": {"observation.images.a": {"answered": True, "glitch": {"level": "none"},
                                                     "shake": {"level": "severe", "times": [[2.0, 3.0]]},
                                                     "contamination": {"level": "none"}}}}

EEF_REJECT = {"decision": {"outcome": "reject", "reason": "ext 的末端位置与画面不符", "confirmed": [
    {"code": "confirmed", "subitem": "position_2d", "camera_id": "ext"}], "human": [], "unchecked": []},
    "reason": "ext 的末端位置与画面不符", "assessment_mode": "verdict", "overall": "assessed",
    "record": {"status": "suspect", "sources": {"pose": {"status": "suspect", "reasons": ["record_deviation"]}}}}
EEF_HUMAN = {"decision": {"outcome": "human", "reason": "需要人工裁决：模型与 CPU 意见冲突",
                          "human": [{"code": "conflict", "subitem": "position_2d", "camera_id": "ext"}], "confirmed": []},
             "reason": "需要人工裁决：模型与 CPU 意见冲突", "assessment_mode": "verdict"}
EEF_OPINION = {"decision": {"outcome": "opinion", "confirmed": [], "human": [], "unchecked": []},
               "assessment_mode": "vlm_opinion", "overall": "opinion",
               "opinion": {"protocol": "eef-opinion/1", "status": "answered", "segments": 1, "flagged": True,
                           "max_confidence": 0.85, "cameras": {"ext": {"status": "answered", "segments": [
                               {"start_s": 2.67, "end_s": 6.33, "aspect": "position", "confidence": 0.85}]}}},
               "record": {"status": "unsupported", "reasons": ["record_mapping_missing"]}}
#: registry 5.0 (design doc 25 §7): the merged cells - a conflict between the CPU and the model's review ...
_CPU_ISSUE = {"verdict": "issue", "p": 0.9, "confidence": 0.8}
EEF_CONFLICT = {"assessment_mode": "verdict", "overall": "assessed", "merged": {
    "cells": [{"subitem": "position_2d", "camera": "ext", "p": 0.9, "label": "inconsistent", "flags": ["conflict"],
               "sources": {"cpu": _CPU_ISSUE, "vlm_review": {"verdict": "ok", "p": 0.1, "confidence": 0.8}},
               "time_s": [2.0, 4.0]},
              {"subitem": "temporal_alignment", "camera": "ext", "p": 0.05, "label": "consistent",
               "flags": ["single_source"], "missing": "model_cannot_see",
               "sources": {"cpu": {"verdict": "ok", "p": 0.05, "confidence": 0.9}}}],
    "episode": {"label": "inconsistent", "p": 0.9, "subitem": "position_2d", "camera": "ext", "flags": ["conflict"],
                "reason": "不一致 · 0.90 · 冲突", "conflicts": 1}}}
#: ... the model's whole-clip opinion alone (no gripper reference): capped, one finding per cell at "possibly" or above ...
EEF_OPINIONS = {"assessment_mode": "vlm_opinion", "overall": "opinion", "merged": {
    "cells": [{"subitem": "position_2d", "camera": "ext", "p": 0.8, "label": "inconsistent",
               "flags": ["single_source"], "missing": "no_gripper_reference",
               "sources": {"vlm_opinion": {"verdict": "issue", "p": 0.85, "confidence": 0.7}}, "time_s": [2.67, 6.33]},
              {"subitem": "orientation_2d", "camera": "ext", "p": 0.55, "label": "possibly_inconsistent",
               "flags": ["single_source"], "missing": "no_gripper_reference",
               "sources": {"vlm_opinion": {"verdict": "issue", "p": 0.55, "confidence": 0.1}}}],
    "episode": {"label": "inconsistent", "p": 0.8, "subitem": "position_2d", "camera": "ext", "flags": ["single_source"],
                "reason": "不一致 · 0.80", "conflicts": 0}}}
#: ... and an episode nothing could be said about: no finding, MV-4 not assessed
EEF_CANNOT = {"assessment_mode": "vlm_opinion", "overall": "opinion", "merged": {
    "cells": [], "episode": {"label": "cannot_tell", "p": None, "flags": [], "why": [], "conflicts": 0,
                             "reason": "判断不了：这一条推不出轨迹"}}}
#: a handheld gripper's wrist camera whose poses come late (design doc 22 §5.3): the model saw nothing amiss
_EGO_SEG = {"start_frame": 50, "end_frame": 250, "start_s": 1.683, "end_s": 8.35, "reason": "time_offset",
            "magnitude": 0.467, "unit": "s", "band": "moderate", "band_level": 2, "evidence_frames": [120, 135],
            "lag_s": 0.467}
EEF_EGO = {**EEF_OPINION, "opinion": {**EEF_OPINION["opinion"], "flagged": False, "segments": 0, "cameras": {}},
           "ego_motion": {"status": "suspect", "verdict": "bad", "explanation_zh": "x", "worst": {"camera": "robot0_camera0", **_EGO_SEG},
                          "cameras": {"robot0_camera0": {"status": "suspect", "segments": [_EGO_SEG], "unmatched": [],
                                                         "metrics": {"rotation_median_deg": 1.99, "rotation_p95_deg": 8.0,
                                                                     "lag_s": 0.467, "lag_confidence": 0.8, "coverage": 0.97},
                                                         "lag": {"lag_s": 0.467, "confidence": 0.8, "flagged": True}},
                                      "robot1_camera0": {"status": "ok", "segments": [], "unmatched": [],
                                                         "metrics": {"rotation_median_deg": 0.5, "lag_s": -0.033},
                                                         "lag": {"lag_s": -0.033, "confidence": 0.84, "flagged": False}}}}}


def _integ(*findings, files=None):
    return {"outcome": "reject", "reason": "x", "tiers": {"L1": True, "L2": True, "L3": False},
            "findings": list(findings), "files": files or []}


INTEG_CASES = [
    ({"level": "reject", "code": "file_empty", "tier": "L1", "message": "wrist 视频 0 字节", "camera": "wrist"}, "file_empty"),
    ({"level": "reject", "code": "file_truncated", "tier": "L1", "message": "被截断，第 40 帧（2.6 秒）起的数据缺失",
      "file": "videos/w/ep1.mp4", "span_s": [2.6, None]}, "file_truncated"),
    ({"level": "reject", "code": "file_truncated", "tier": "L2", "message": "录制中断，且读不出数据（McapError）"}, "cut_unreadable"),
    ({"level": "reject", "code": "zero_filled", "tier": "L1", "message": "零填充", "file": "v.mp4", "span_s": [1.0, 2.0]}, "zero_filled"),
    ({"level": "reject", "code": "structure_invalid", "tier": "L1", "message": "找不到完整的 moov"}, "structure_invalid"),
    ({"level": "reject", "code": "crc_mismatch", "tier": "L2", "message": "CRC 不符"}, "crc_mismatch"),
    ({"level": "reject", "code": "decode_failed", "tier": "L3", "message": "第 3.2 秒起解码失败"}, "decode_failed"),
    ({"level": "suspect", "code": "decode_concealed", "tier": "L3", "message": "解码器掩盖了错误"}, "decode_concealed"),
    ({"level": "suspect", "code": "count_mismatch", "tier": "L1", "message": "帧数 280，记录 300"}, "count_mismatch"),
    ({"level": "suspect", "code": "cut_off", "tier": "L1", "message": "录制中断：文件尾没有 mcap 结束标识"}, "cut_off"),
    ({"level": "suspect", "code": "duplicate_content", "tier": "dataset", "message": "与 ep 7 的内容完全相同"}, "duplicate_content"),
    ({"level": "suspect", "code": "stream_missing", "tier": "peers", "message": "缺 /cam topic", "args": {"topic": "/cam"}}, "stream_missing"),
    ({"level": "suspect", "code": "rate_outlier", "tier": "peers", "message": "频率减半"}, "rate_outlier"),
    ({"level": "suspect", "code": "table_inconsistent", "tier": "dataset", "message": "episode 表前后不一致"}, "table_inconsistent"),
    ({"level": "reject", "code": "row_invalid", "tier": "L2", "message": "数据不合规：ep1: action 缺失或为空"}, "action_missing"),
    ({"level": "reject", "code": "row_invalid", "tier": "L2", "message": "数据不合规：action 含 3 个 NaN/Inf"}, "values_invalid"),
    ({"level": "reject", "code": "row_invalid", "tier": "L2", "message": "数据不合规：timestamps 长度(9) != action 帧数(10)"}, "length_mismatch"),
    ({"level": "reject", "code": "row_invalid", "tier": "L2", "message": "数据不合规：时间戳非严格递增(帧 3→4: 1→1)"}, "timestamps_invalid"),
    ({"level": "reject", "code": "row_invalid", "tier": "L2", "message": "数据不合规：视频文件不存在: wrist → v.mp4"}, "video_missing"),
    ({"level": "reject", "code": "row_invalid", "tier": "L2", "message": "数据不合规：fps 非法: 0"}, "metadata_invalid"),
    ({"level": "reject", "code": "file_truncated", "tier": "L2", "message": "录制中断，且读不出数据（RecordLengthLimitExceeded）",
      "args": {"error": "RecordLengthLimitExceeded"}}, "cut_unreadable"),
]

# (module, passed, score, details) -> expected codes; unassessable items
CASES = [
    ("timestamp_check", True, None, TS_PASS, set(), set()),
    ("timestamp_check", False, None, TS_GAP, {"gap"}, set()),
    ("timestamp_check", False, None, TS_FRAGMENT, {"fragment"}, set()),
    ("timestamp_check", False, None, TS_JITTER, {"jitter"}, set()),
    ("timestamp_check", False, None, TS_ORDER, {"out_of_order"}, set()),
    ("timestamp_check", False, None, TS_SINGLE, {"single_stamp"}, set()),
    ("kinematic_limits", False, None, KIN_JOINT, {"joint_limit", "velocity_limit"}, set()),
    ("kinematic_limits", False, None, KIN_EE, {"ee_reach", "ee_translation_velocity", "ee_rotation_velocity"}, set()),
    ("kinematic_limits", False, None, KIN_DOF, {"data_invalid"}, set()),
    ("kinematic_limits", None, None, KIN_DRAFT, set(), {"ACT-4"}),
    ("motion_quality", None, 0.8177, MQ_STUCK, {"stuck"}, {"ACT-5"}),
    ("motion_quality", None, 0.4914, MQ_IDLE, {"smoothness_low", "spike", "gripper_jitter", "actuator_saturation",
                                              "fluency_low", "idle_opening"}, set()),
    ("motion_quality", None, 0.9, MQ_NO_STATE, {"idle_closing"}, {"ACT-4", "ACT-5", "ACT-8"}),
    ("visual_quality", None, 0.4, VQ, {"frozen", "exposure_low", "sharpness_low", "information_death", "dead_or_padded"}, set()),
    ("visual_quality", None, None, {}, set(), {"IMG-1", "IMG-2", "IMG-3", "IMG-4", "STRM-1"}),
    ("video_action_sync", False, None, SYNC_ALL, {"misaligned_all"}, set()),
    ("video_action_sync", True, None, SYNC_ONE, {"camera_misaligned", "lag_inconsistent"}, set()),
    ("video_action_sync", True, None, SYNC_WEAK, {"undecidable"}, set()),
    ("video_action_sync", True, None, SYNC_SUSPECT, {"suspect"}, set()),
    ("video_action_sync", True, None, SYNC_NONE, set(), {"AV-1", "AV-3", "MV-3"}),
    ("task_success", False, None, TASK_FAIL, {"failure"}, {"TASK-10"}),
    ("task_success", None, None, TASK_UNSURE, {"uncertain", "task_text_missing"}, {"TASK-10"}),
    ("task_success", None, None, TASK_HELD, {"label_conflict_suspect", "uncertain"}, {"TASK-10"}),
    ("task_success", True, None, TASK_OK_FRAMES, {"recovery", "task_text_missing"}, set()),
    ("camera_defects", None, None, CAM, {"glitch", "contamination"}, {"IMG-6"}),
    ("camera_defects", None, None, CAM_SHAKE, {"shake"}, set()),
    ("camera_defects", None, None, {"reason": "任务成败判定没有产生结果，无法读取逐机位复核"}, set(), {"IMG-5", "IMG-6", "IMG-7"}),
    # AV-1 since 4.1: only a wrist camera's own motion reads the episode's timing
    ("eef_video_consistency", False, None, EEF_REJECT, {"inconsistent", "record_mismatch"}, {"AV-1"}),
    ("eef_video_consistency", None, None, EEF_HUMAN, {"unsettled"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_OPINION, {"opinion_mismatch"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_EGO, {"ego_motion_suspect"}, set()),
    ("eef_video_consistency", True, None, EEF_CONFLICT, {"conflict"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_OPINIONS, {"inconsistent"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_CANNOT, set(), {"MV-4", "AV-1"}),
    ("dedup", False, None, {"duplicate_of": 43, "reason": "与 ep000043 字节级完全重复"}, {"duplicate"}, set()),
    ("dedup", True, None, {}, set(), set()),

] + [("data_integrity", None if f["level"] == "suspect" else False, None, _integ(f), {code}, set())
     for f, code in INTEG_CASES]


def _record(module, passed, score, details, **kw):
    return record_from_struct(module, 7, {"passed": passed, "score": score, "detail": json.dumps(details)}, **kw)


@pytest.mark.parametrize("module,passed,score,details,codes,unassessable", CASES,
                         ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_each_answer_gives_its_findings(module, passed, score, details, codes, unassessable):
    rec = _record(module, passed, score, details)
    assert schemas.errors("cli/result-record.schema.json", rec) == []
    assert is_v2(rec) and rec["status"] == "ok" and rec["details"] == details      # details stay as written
    assert {f["code"] for f in rec["findings"]} == codes
    assert {u["item"] for u in rec["unassessable"]} == unassessable
    covers = registry.get(module).covers
    assert rec["assessed"] == [i for i in covers if i not in unassessable]
    for f in rec["findings"]:
        spec = registry.finding_code(module, f["code"])
        assert f["item"] == spec.item and f["message_zh"]
    # the default policy keeps today's gates: a failed episode stops the funnel, nothing else does - but the EEF
    # module rejects nothing since 5.0 (D81; a run from before keeps its rejects through its frozen defaults)
    assert passes_funnel(rec) == (passed is not False or module == "eef_video_consistency")


#: codes no answer can raise yet; every module's codes have a fixture since the skill profile left
NO_SOURCE_YET: set[tuple[str, str]] = set()


def test_every_code_is_drawn_from_a_real_answer():
    """F12.2 acceptance ②: every code of §2.2 has a fixture (dataset-level ones in the next test)."""
    seen = {(m, f["code"]) for m, passed, score, d, *_ in CASES for f in _record(m, passed, score, d)["findings"]}
    wanted = {(s.id, c.code) for s in registry.MODULES for c in s.codes if c.scope_kind != "dataset"}
    assert wanted - seen == NO_SOURCE_YET



def test_a_wrist_camera_off_its_poses_is_one_finding_at_its_worst_stretch():
    """design doc 22 §5.3: info level, at most one an episode, at the worst stretch of the camera it is on, with
    every camera's readings; the episode is kept whatever it says."""
    rec = _record("eef_video_consistency", True, None, EEF_EGO)
    (f,) = rec["findings"]
    assert f["code"] == "ego_motion_suspect" and f["item"] == "MV-4" and f["severity"] == "medium"
    assert f["message_zh"] == "腕部相机的运动与记录的位姿不一致：位姿约晚 0.47 s（第 51–251 帧，中）"
    assert f["time_s"] == [1.683, 8.35] and f["scope"] == {"camera": "robot0_camera0"}
    assert f["readings"]["lag_s"] == {"robot0_camera0": 0.467}
    assert f["readings"]["cameras"]["robot1_camera0"]["status"] == "ok"
    assert rec["assessed"] == ["MV-4", "AV-1"] and passes_funnel(rec)
    severe = {**EEF_EGO, "ego_motion": {**EEF_EGO["ego_motion"], "worst": {"camera": "robot1_camera0", **_EGO_SEG,
                                                                          "reason": "rotation", "magnitude": 9.61,
                                                                          "unit": "deg", "band": "severe"}}}
    (f,) = _record("eef_video_consistency", True, None, severe)["findings"]
    assert f["severity"] == "high" and f["message_zh"].endswith("画面里的转动与位姿差 9.6°（第 51–251 帧，重）")
    calm = {**EEF_EGO, "ego_motion": {**EEF_EGO["ego_motion"], "status": "ok", "worst": None}}
    assert _record("eef_video_consistency", True, None, calm)["findings"] == []


def test_a_record_mismatch_says_which_records_disagree():
    """design doc 25 §6.1: a generated trajectory is its record's own - the dataset's two records disagree (pose column
    against the joints' kinematics, source internal); an upload is compared with the records (source upload)."""
    internal = {**EEF_OPINIONS, "record": {"status": "suspect", "source": "internal", "reasons": ["record_internal_mismatch"],
                                           "sources": {}, "internal": {"compared": True, "consistent": False,
                                                                       "position_mm": {"median": 9.0, "p95": 31.25}}}}
    (f,) = [x for x in _record("eef_video_consistency", True, None, internal)["findings"] if x["code"] == "record_mismatch"]
    assert f["message_zh"] == "数据集的末端位姿列与关节角正解不一致（位置差 p95 31.2 mm）"
    assert f["readings"] == {"status": "suspect", "source": "internal", "reasons": ["record_internal_mismatch"]}
    upload = {**EEF_OPINIONS, "record": {"status": "suspect", "source": "upload", "reasons": [],
                                         "sources": {"pose": {"status": "suspect", "reasons": ["record_deviation"]}}}}
    (f,) = [x for x in _record("eef_video_consistency", True, None, upload)["findings"] if x["code"] == "record_mismatch"]
    assert f["message_zh"].startswith("上传的轨迹与数据集自己记录的末端位姿不一致") and f["readings"]["source"] == "upload"


def test_intervals_scopes_and_readings():
    gap = _record("timestamp_check", False, None, TS_GAP)["findings"][0]
    assert gap["frames"] == [89, 90] and gap["readings"]["gaps"] == [{"frame": 89, "dt_s": 1.1}]
    stuck = _record("motion_quality", None, 0.8, MQ_STUCK)["findings"][0]
    assert stuck["frames"] == [49, 92] and stuck["scope"] == {"channel": "joint 2"}
    idle = next(f for f in _record("motion_quality", None, 0.49, MQ_IDLE)["findings"] if f["code"] == "idle_opening")
    assert idle["time_s"] == [0.0, 2.0]
    spike = next(f for f in _record("motion_quality", None, 0.49, MQ_IDLE)["findings"] if f["code"] == "spike")
    assert spike["severity"] == "high" and spike["readings"]["spike_frames"] == [239, 240]    # 2nd-difference index + 1
    rec = _record("visual_quality", None, 0.4, VQ)
    assert rec["readings"]["score"] == 0.4
    assert {(f["code"], f["scope"]["camera"]) for f in rec["findings"]} == {
        ("frozen", "wrist"), ("exposure_low", "side"), ("sharpness_low", "side"), ("information_death", "side"),
        ("dead_or_padded", "image_3")}
    fail = _record("task_success", False, None, TASK_FAIL)["findings"][0]
    assert fail["time_s"] == [4.2, 6.0] and fail["scope"] == {"camera": "front"}
    glitch = [f for f in _record("camera_defects", None, None, CAM)["findings"] if f["code"] == "glitch"]
    assert {(f["scope"]["camera"], f["severity"]) for f in glitch} == {("a", "low"), ("b", "medium")}
    assert next(f for f in glitch if f["scope"]["camera"] == "a")["time_s"] == [1.0, 1.5]
    opinion = _record("eef_video_consistency", True, None, EEF_OPINION)["findings"][0]
    assert opinion["time_s"] == [2.67, 6.33] and opinion["scope"] == {"camera": "ext"}
    lag = next(f for f in _record("video_action_sync", True, None, SYNC_ONE)["findings"] if f["code"] == "lag_inconsistent")
    assert lag["scope"] == {"cameras": ["front", "wrist"]}
    trunc = _record("data_integrity", False, None, _integ(INTEG_CASES[1][0]))["findings"][0]
    assert "time_s" not in trunc and trunc["readings"] == {"from_s": 2.6}          # a truncation runs to the end
    zero = _record("data_integrity", False, None, _integ(INTEG_CASES[3][0], files=[{"file": "v.mp4", "window_s": [0.5, 9.0]}]))
    assert zero["findings"][0]["time_s"] == [0.5, 1.5]                              # a shared file's window is subtracted
    dup = _record("dedup", False, None, {"duplicate_of": 43})["findings"][0]
    assert dup["readings"] == {"group_id": 43, "duplicate_of": 43}


def test_thresholds_are_module_parameters():
    """Design doc 17 §1.3: the judgement lines are parameters with defaults, not constants of the evaluation."""
    loose = _record("visual_quality", None, 0.4, VQ, params={"exposure_min": 0.3, "sharpness_min": 0.3})
    assert {f["code"] for f in loose["findings"]} == {"frozen", "information_death", "dead_or_padded"}
    strict = _record("video_action_sync", True, None, SYNC_ONE, params={"spread_tol_s": 1.0})
    assert "lag_inconsistent" not in {f["code"] for f in strict["findings"]}
    calm = _record("motion_quality", None, 0.49, MQ_IDLE, params={"idle_edge_min_s": 5})
    assert "idle_opening" not in {f["code"] for f in calm["findings"]}


def test_guards_keep_todays_gates():
    """A failed gate always blocks by default, a person's question always reaches review (P18)."""
    rec = _record("timestamp_check", False, None, {"n": 3})                         # a fail v1 did not explain
    assert [f["code"] for f in rec["findings"]] == ["gap"] and not passes_funnel(rec)
    rec = _record("data_integrity", False, None, _integ())
    assert [f["code"] for f in rec["findings"]] == ["structure_invalid"]
    rec = _record("eef_video_consistency", None, None, {"decision": {}})
    assert [f["code"] for f in rec["findings"]] == ["unsettled"]


def test_context_and_errors():
    rec = _record("motion_quality", None, 0.9, MQ_STUCK, context={"action_semantics": {"source": "profile"}})
    assert rec["readings"]["action_semantics"] == {"source": "profile"}
    err = record_from_struct("task_success", 3, None, incidents=[{"step": "probe", "cause": "timeout"}])
    assert err["status"] == "error" and err["findings"] == [] and err["assessed"] == []
    assert schemas.errors("cli/result-record.schema.json", err) == []
    assert legacy_verdict(err) == "error" and not passes_funnel(err)


def test_the_compatibility_verdict_matches_1_0():
    """Views that still count by 1.0's verdict read a 2.0 record the same way (camera_defects, always abstain
    in 1.0, and sync without cameras, a pass in 1.0, are the two deliberate exceptions)."""
    for module, passed, score, details, *_ in CASES:
        verdict = {True: "pass", False: "fail"}.get(passed) or ("scored" if score is not None else "abstain")
        old = {"episode_index": 7, "module": module, "verdict": verdict, "passed": passed, "score": score,
               "gate": "none", "details": details, "evidence": [], "elapsed_s": None, "error": None}
        new = upgrade_record(old)
        if module == "camera_defects" or details is SYNC_NONE:
            continue
        if module == "eef_video_consistency":      # 1.0's EEF verdicts used 4.x's levels (D81 changed them)
            continue
        assert legacy_verdict(new) == old["verdict"], (module, details)


def test_dataset_level_findings():
    recs = {e: _record("timestamp_check", True, None, {**TS_PASS, "duration_s": 20.0 + e * 0.1}) for e in range(10)}
    recs[10] = _record("timestamp_check", True, None, {**TS_PASS, "duration_s": 95.0})
    found, readings = F.dataset_level("timestamp_check", recs)
    assert [f["code"] for f in found] == ["duration_outlier"] and found[0]["readings"]["episodes"] == [10]
    assert found[0]["unit"] == "dataset" and found[0]["item"] is None     # the platform's own reading (taxonomy 1.3)
    sem = {"source": "preflight_unknown", "action_space": "unknown", "control_mode": "unknown", "undetermined": True}
    recs = {e: _record("motion_quality", None, 0.9, MQ_STUCK, context={"action_semantics": sem}) for e in range(3)}
    found, readings = F.dataset_level("motion_quality", recs)
    assert [f["code"] for f in found] == ["action_semantics_undetermined"] and readings["active_ratio_mean"] == 1.0
    integ = {"findings": [{"level": "dataset", "code": "orphan_files", "message": "2 个不属于任何 episode 的文件",
                           "args": {"count": 2}},
                          {"level": "dataset", "code": "dark_camera", "message": "wrist 近乎全黑"},
                          {"level": "dataset", "code": "table_overlap", "message": "ep 3 与 ep 4 重叠"}]}
    found, _ = F.dataset_level("data_integrity", {}, integrity=integ)
    assert [f["code"] for f in found] == ["orphan_files", "dark_camera", "table_overlap"]
    assert found[0]["item"] is None
    every = {(s.id, c.code) for s in registry.MODULES for c in s.codes if c.scope_kind == "dataset"}
    # the EEF cameras' calibration suspects (5.4): tests/eef/test_calibration_suspect.py
    assert every == {("data_integrity", "orphan_files"), ("data_integrity", "dark_camera"),
                     ("data_integrity", "table_overlap"), ("timestamp_check", "duration_outlier"),
                     ("motion_quality", "action_semantics_undetermined"),
                     ("eef_video_consistency", "calibration_suspect")}
    for f in found:
        assert schemas.errors("cli/common.schema.json#/$defs/finding", f) == []


def test_p20_items_have_fixtures():
    """P20: the items that needed only a mapping.

    LABEL-1 (several descriptions of one episode that disagree) left the list with the skill
    profile - it was the only module that compared them - and nothing covers it now."""
    items = set()
    for m, passed, score, d, *_ in CASES:
        rec = _record(m, passed, score, d)
        items |= {f["item"] for f in rec["findings"]}
    items |= {"SET-3", "ACT-6"}                       # dataset level, test_dataset_level_findings
    assert {"LABEL-2", "IMG-3", "TASK-1", "AV-3", "MV-3", "SET-3", "ACT-6"} <= items
    assert "LABEL-1" not in items
