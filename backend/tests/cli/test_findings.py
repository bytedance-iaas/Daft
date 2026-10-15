"""Findings from the modules' answers (design doc 17 §1-§2, F12.2): every code of the registry's catalogue is
drawn from a real answer shape, every record 2.0 fits the contract, and the default levels keep today's gates."""
from __future__ import annotations

import json

import pytest

from curation.contracts import modules as registry
from curation.contracts import schemas
from curation.pipeline import findings as F
from curation.pipeline.records import legacy_verdict, record_from_struct

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
# 8.2 (2026-10-14, so101): the state is read, the profile's velocity limit is an observed envelope (approximate), the
# leader arm's gripper command closed in one frame - the robot itself never exceeded anything
KIN_SO101 = {"n_violations": 1, "violations": [
    {"type": "velocity_limit", "joint": 1, "frame": 46, "value": 308.2107, "limit": 300.0}],
    "profile": "so101", "unit": "deg_normalized", "margin": 0.02, "quality": "approximate", "basis": "state",
    "n_command_velocity": 1, "command_velocity_violations": [
    {"type": "velocity_limit", "joint": 5, "frame": 76, "value": 405.7187, "limit": 300.0}]}
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


def _curve(center: float, width: float, amp: float, floor: float = 0.1, step: float = 0.1):
    """A correlation curve over ±2 s: a Gaussian bump of ``amp`` at ``center`` over ``floor``."""
    import math
    lags = [round(-2.0 + i * step, 4) for i in range(int(4.0 / step) + 1)]
    return {"lags_s": lags, "corr": [round(floor + (amp - floor) * math.exp(-0.5 * ((x - center) / width) ** 2), 4)
                                     for x in lags]}


# the borrowed-peak test (MV-3): wrist is trusted and misaligned by -0.97 s; front cannot place its own peak
# (ambiguous) but its curve is clearly higher around 0 than at -0.97 s -> the two cameras disagree
SYNC_BORROW = {"verdict": "annotated", "per_camera": {
    "front": {"lag_s": 0.07, "corr_peak": 0.59, "corr_at_zero": 0.556, "trusted": False, "code": "ambiguous_peak",
              "xc_curve": _curve(0.07, 0.35, 0.59)},
    "wrist": {"lag_s": -0.97, "corr_peak": 0.5632, "trusted": True, "code": "misaligned"}},
    "flagged_cameras": ["wrist"], "suspect_cameras": [], "noisy_cameras": [], "abstained_cameras": ["front"],
    "consensus_lag_s": None, "n_cameras": 2, "n_trusted": 1, "reason": "仅 1 路可信"}
# the same, but front's curve is flat: -0.97 s fits it as well as anything -> cannot be verified, no MV-3
SYNC_BORROW_FLAT = {**SYNC_BORROW, "per_camera": {
    **SYNC_BORROW["per_camera"],
    "front": {**SYNC_BORROW["per_camera"]["front"], "xc_curve": _curve(0.07, 3.0, 0.59, floor=0.5)}}}
# the same, but front has no signal at all (low_corr): its curve is noise and takes no part
SYNC_BORROW_WEAK = {**SYNC_BORROW, "per_camera": {
    **SYNC_BORROW["per_camera"],
    "front": {**SYNC_BORROW["per_camera"]["front"], "corr_peak": 0.15, "code": "low_corr",
              "xc_curve": _curve(0.07, 0.35, 0.15)}}}
# the windowed medians (second estimator): neither camera's whole-curve reading is trusted, but both are solid
# window-wise and 0.2 s apart -> MV-3 by the windows; with a wobbly camera (IQR) or a small spread -> nothing
def _lw(lag, n_trusted=9, iqr=0.03, dt=1 / 30):
    return {"win_s": 2.0, "hop_s": 1.0, "dt_s": round(dt, 4), "n_windows": 11, "n_trusted": n_trusted, "lag_s": lag,
            "iqr_s": iqr, "windows": []}


SYNC_WINDOWED = {"verdict": "annotated", "per_camera": {
    "front": {"lag_s": -0.73, "corr_peak": 0.65, "trusted": False, "code": "ambiguous_peak", "lag_windows": _lw(-0.13)},
    "wrist": {"lag_s": 0.07, "corr_peak": 0.61, "trusted": False, "code": "ambiguous_peak", "lag_windows": _lw(0.07)}},
    "flagged_cameras": [], "suspect_cameras": [], "noisy_cameras": [], "abstained_cameras": ["front", "wrist"],
    "consensus_lag_s": None, "n_cameras": 2, "n_trusted": 0, "reason": "2 路相机均未给出可信读数"}
SYNC_WINDOWED_WOBBLY = {**SYNC_WINDOWED, "per_camera": {**SYNC_WINDOWED["per_camera"],
    "front": {**SYNC_WINDOWED["per_camera"]["front"], "lag_windows": _lw(-0.13, iqr=0.5)}}}
SYNC_WINDOWED_FEW = {**SYNC_WINDOWED, "per_camera": {**SYNC_WINDOWED["per_camera"],
    "front": {**SYNC_WINDOWED["per_camera"]["front"], "lag_windows": _lw(-0.13, n_trusted=2)}}}
SYNC_WINDOWED_CLOSE = {**SYNC_WINDOWED, "per_camera": {**SYNC_WINDOWED["per_camera"],
    "front": {**SYNC_WINDOWED["per_camera"]["front"], "lag_windows": _lw(0.0)}}}
# at 15 fps three frames are 0.2 s: the same 0.2 s spread is within tolerance
SYNC_WINDOWED_15FPS = {**SYNC_WINDOWED, "per_camera": {
    "front": {**SYNC_WINDOWED["per_camera"]["front"], "lag_windows": _lw(-0.13, dt=1 / 15)},
    "wrist": {**SYNC_WINDOWED["per_camera"]["wrist"], "lag_windows": _lw(0.07, dt=1 / 15)}}}
# the same, but front's curve prefers the very lag wrist found -> the whole episode may be shifted, not MV-3
SYNC_BORROW_SAME = {**SYNC_BORROW, "per_camera": {
    **SYNC_BORROW["per_camera"],
    "front": {**SYNC_BORROW["per_camera"]["front"], "lag_s": -0.9, "xc_curve": _curve(-0.95, 0.6, 0.55)}}}

TASK_FAIL = {"verdict": "failure", "init_verdict": "failure", "review": "no", "cam_votes": {"front": "no"},
             "rules": ["video_double_signed_failure"], "video_completion": 0.2, "input_mode": "video",
             "protocol": "video-task/1", "task_desc_source": "原始标注", "reason": "杯子没有放进水槽",
             "video_assessment": {"verdict": "failure", "completion": 0.2, "reason": "杯子掉在台面",
                                  "evidence": [{"camera": "observation.images.front", "start_s": 4.2, "end_s": 6.0,
                                                "observation": "杯子从夹爪滑落"}]}}
TASK_UNSURE = {"verdict": "uncertain", "video_completion": 0.7, "input_mode": "video", "protocol": "video-task/2",
               "task_desc_source": "原始标注", "reason": "看不清杯子最后是否放稳"}
#: an episode without a task text is not judged (D72)
TASK_SKIPPED = {"skipped": "no_task_text", "reason": "没有任务标注"}

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
# registry 8.1: occlusion per camera with its share, place and length, and the camera's own identity
CAM_VIEW = {"protocol": "camera-check/5", "reason": "", "identity": "inconsistent",
            "items": {"glitch": "none", "shake": "none", "contamination": "none", "occlusion": "severe"},
            "per_camera": {
                "observation.images.wrist": {"answered": True, "glitch": {"level": "none"}, "shake": {"level": "none"},
                                             "contamination": {"level": "none", "kind": "none"},
                                             "occlusion": {"level": "severe", "source": "person", "times": [[1.0, 4.0]],
                                                           "share": 0.3, "coverage": 0.45, "region": "center", "duration_s": 3.0},
                                             "view": {"kind": "fixed", "arm": "single", "matches_name": "no", "same_as": "",
                                                      "note": "scene never moves with the arm"}},
                "observation.images.front": {"answered": True, "glitch": {"level": "none"}, "shake": {"level": "none"},
                                             "contamination": {"level": "none", "kind": "none"},
                                             "occlusion": {"level": "none", "source": "none", "times": [], "share": 0.0},
                                             "view": {"kind": "fixed", "arm": "single", "matches_name": "yes", "same_as": "",
                                                      "note": ""}}}}
# registry 8.1: the arms are crossed BETWEEN cameras - every camera agrees with its own mount, and
# only the measured pairing shows that the one called left rides the right arm
CAM_ARMS = {"protocol": "camera-check/10", "reason": "", "identity": "inconsistent",
            "items": {k: "unknown" for k in ("glitch", "shake", "contamination", "occlusion")},
            "arms": {"identity": "crossed", "crossed": ["observation.images.left_wrist"],
                     "basis": "motion_pairing", "readings": {"margin": 1.4},
                     "note": "画面运动跟的是另一条臂"},
            "per_camera": {"observation.images.left_wrist": {"answered": True},
                           "observation.images.right_wrist": {"answered": True}}}

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
#: a handheld gripper's wrist camera whose poses come late (design doc 22 §5.3): its merged cell is the finding, and
#: its time offset reading is the episode's AV-1
_EGO_CAMS = {"robot0_camera0": {"status": "suspect", "segments": [], "unmatched": [],
                                "metrics": {"rotation_median_deg": 1.99, "lag_s": 0.467, "lag_confidence": 0.8},
                                "lag": {"lag_s": 0.467, "confidence": 0.8, "flagged": True}},
             "robot1_camera0": {"status": "ok", "segments": [], "unmatched": [],
                                "metrics": {"rotation_median_deg": 0.5, "lag_s": -0.033},
                                "lag": {"lag_s": -0.033, "confidence": 0.84, "flagged": False}}}
EEF_EGO = {"assessment_mode": "vlm_opinion", "overall": "opinion",
           "ego_motion": {"status": "suspect", "cameras": _EGO_CAMS},
           "merged": {"cells": [{"subitem": "ego_motion", "camera": "robot0_camera0", "p": 0.7,
                                 "label": "possibly_inconsistent", "flags": ["single_source"],
                                 "sources": {"cpu": {"verdict": "issue", "p": 0.7, "confidence": 0.8}},
                                 "time_s": [1.683, 8.35]}],
                      "episode": {"label": "possibly_inconsistent", "p": 0.7, "subitem": "ego_motion",
                                  "camera": "robot0_camera0", "flags": ["single_source"], "conflicts": 0,
                                  "reason": "可能不一致 · 0.70"}}}
#: the trajectory uploaded against the dataset's own record: reported only
EEF_RECORD = {**EEF_OPINIONS, "record": {"status": "suspect", "source": "upload", "reasons": [],
                                         "sources": {"pose": {"status": "suspect", "reasons": ["record_deviation"]}}}}

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
    # the MV-3 record layer (2026-10-13): one camera's container timing against the window, the cameras against each other
    ({"level": "suspect", "code": "camera_start_offset", "tier": "L1", "message": "front 相机的视频起点比时间轴晚 0.200 秒（6.0 帧）",
      "file": "videos/front/f.mp4", "camera": "front", "args": {"start_offset_s": 0.2, "edit_offset_s": 0.2}}, "camera_start_offset"),
    ({"level": "suspect", "code": "camera_timing_mismatch", "tier": "L1",
      "message": "各相机的视频时间轴彼此对不上：起点偏移 front +0.200 秒、wrist +0.000 秒",
      "args": {"start_offset_s": {"front": 0.2, "wrist": 0.0}}}, "camera_timing_mismatch"),
    ({"level": "suspect", "code": "camera_rate_mismatch", "tier": "L1", "message": "front 相机的帧间隔中位数 35.03 毫秒，按 30 fps 应为 33.33 毫秒（比例 1.051）",
      "file": "videos/front/f.mp4", "camera": "front", "args": {"rate_ratio": 1.0508}}, "camera_rate_mismatch"),
    ({"level": "reject", "code": "decode_failed", "tier": "L3", "message": "第 3.2 秒起解码失败"}, "decode_failed"),
    ({"level": "suspect", "code": "decode_concealed", "tier": "L3", "message": "解码器掩盖了错误"}, "decode_concealed"),
    ({"level": "suspect", "code": "count_mismatch", "tier": "L1", "message": "帧数 280，记录 300"}, "count_mismatch"),
    ({"level": "suspect", "code": "cut_off", "tier": "L1", "message": "录制中断：文件尾没有 mcap 结束标识"}, "cut_off"),
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
    # design doc 29 (D95-D97): the module says these itself, with the registry's codes
    ({"level": "reject", "code": "data_missing", "tier": "L1", "message": "数据文件不存在：data/chunk-000/episode_000005.parquet",
      "file": "data/chunk-000/episode_000005.parquet"}, "data_missing"),
    ({"level": "reject", "code": "video_missing", "tier": "L1", "message": "wrist 相机的视频文件不存在：videos/w/ep0.mp4",
      "file": "videos/w/ep0.mp4", "camera": "wrist"}, "video_missing"),
    ({"level": "reject", "code": "column_missing", "tier": "L1", "message": "ep2.parquet 里没有 observation.state 列（info.json 声明了）",
      "file": "ep2.parquet", "args": {"column": "observation.state"}}, "column_missing"),
    ({"level": "reject", "code": "action_missing", "tier": "L1", "message": "缺动作 topic /action（这条 mcap 里没有它）",
      "args": {"topic": "/action"}}, "action_missing"),
    ({"level": "reject", "code": "length_mismatch", "tier": "L1", "message": "数据有 69 帧，episode 表记 79 帧",
      "args": {"rows": 69, "length": 79}}, "length_mismatch"),
    ({"level": "reject", "code": "values_invalid", "tier": "L1", "message": "observation.state 每帧 6 维，info.json 声明 7 维",
      "args": {"column": "observation.state", "width": 6, "declared": 7}}, "values_invalid"),
    ({"level": "reject", "code": "timestamps_invalid", "tier": "L1", "message": "timestamp 有 3 帧为空（帧 4、9、17）"},
     "timestamps_invalid"),
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
    ("kinematic_limits", True, None, KIN_SO101, {"velocity_limit_approx", "command_velocity_limit"}, set()),
    ("kinematic_limits", False, None, KIN_EE, {"ee_reach", "ee_translation_velocity", "ee_rotation_velocity"}, set()),
    ("kinematic_limits", False, None, KIN_DOF, {"data_invalid"}, set()),
    ("kinematic_limits", None, None, KIN_DRAFT, set(), {"ACT-4"}),
    ("motion_quality", None, 0.8177, MQ_STUCK, {"stuck"}, {"ACT-5"}),
    ("motion_quality", None, 0.4914, MQ_IDLE, {"smoothness_low", "spike", "gripper_jitter", "actuator_saturation",
                                              "fluency_low", "idle_opening"}, set()),
    ("motion_quality", None, 0.9, MQ_NO_STATE, {"idle_closing"}, {"ACT-4", "ACT-5", "ACT-8"}),
    ("visual_quality", None, 0.4, VQ, {"frozen", "exposure_low", "sharpness_low", "information_death", "dead_or_padded"}, set()),
    ("visual_quality", None, None, {}, set(), {"IMG-1", "IMG-2", "IMG-3", "IMG-4", "STRM-1", "FILE-4"}),
    ("video_action_sync", False, None, SYNC_ALL, {"misaligned_all"}, set()),
    ("video_action_sync", True, None, SYNC_ONE, {"camera_misaligned", "lag_inconsistent"}, set()),
    ("video_action_sync", True, None, SYNC_BORROW, {"camera_misaligned", "lag_inconsistent"}, set()),
    ("video_action_sync", True, None, SYNC_BORROW_FLAT, {"camera_misaligned"}, set()),
    ("video_action_sync", True, None, SYNC_BORROW_WEAK, {"camera_misaligned", "undecidable"}, set()),
    ("video_action_sync", True, None, SYNC_BORROW_SAME, {"camera_misaligned"}, set()),
    ("video_action_sync", True, None, SYNC_WINDOWED, {"lag_inconsistent"}, set()),
    ("video_action_sync", True, None, SYNC_WINDOWED_WOBBLY, set(), set()),
    ("video_action_sync", True, None, SYNC_WINDOWED_FEW, set(), set()),
    ("video_action_sync", True, None, SYNC_WINDOWED_CLOSE, set(), set()),
    ("video_action_sync", True, None, SYNC_WINDOWED_15FPS, set(), set()),
    ("video_action_sync", True, None, SYNC_WEAK, {"undecidable"}, set()),
    ("video_action_sync", True, None, SYNC_SUSPECT, {"suspect"}, set()),
    ("video_action_sync", True, None, SYNC_NONE, set(), {"AV-1", "AV-3", "MV-3", "FILE-4"}),
    ("task_success", False, None, TASK_FAIL, {"failure"}, set()),
    ("task_success", None, None, TASK_UNSURE, {"uncertain"}, set()),
    ("task_success", None, None, TASK_SKIPPED, {"task_text_missing"}, {"TASK-4"}),
    ("task_success", True, None, {"verdict": "success", "task_desc_source": "原始标注"}, set(), set()),
    ("camera_defects", None, None, CAM, {"glitch", "contamination"}, {"IMG-6", "IMG-8", "MV-2"}),
    ("camera_defects", None, None, CAM_SHAKE, {"shake"}, {"IMG-8", "MV-2"}),
    ("camera_defects", None, None, CAM_VIEW, {"occlusion", "identity"}, set()),
    ("camera_defects", None, None, CAM_ARMS, {"identity"}, {"IMG-5", "IMG-6", "IMG-7", "IMG-8"}),
    ("camera_defects", None, None, {"reason": "任务成败判定没有产生结果，无法读取逐机位复核"}, set(),
     {"IMG-5", "IMG-6", "IMG-7", "IMG-8", "MV-2"}),
    # the pairing is measured, so a crossing stands even when the request brought no picture answers
    ("camera_defects", None, None, {"reason": "判定请求没有按相机作答", "identity": "inconsistent",
      "arms": {"identity": "crossed", "crossed": ["observation.images.left_wrist"],
               "basis": "motion_pairing", "note": "left_wrist 的画面跟着 right 动（余量 1.0）"}},
     {"identity"}, {"IMG-5", "IMG-6", "IMG-7", "IMG-8"}),
    # and an unknown says which half was missing: measured but inconclusive, never "the model did not answer"
    ("camera_defects", None, None, {"items": {k: "unknown" for k in ("glitch", "shake", "contamination", "occlusion")},
      "per_camera": {"observation.images.left_wrist": {"answered": True}},
      "arms": {"identity": "unknown", "reason": "measurement_inconclusive",
               "note": "几种对应关系的证据一样强"}},
     set(), {"IMG-5", "IMG-6", "IMG-7", "IMG-8", "MV-2"}),
    # AV-1 since 4.1: only a wrist camera's own motion reads the episode's timing
    ("eef_video_consistency", True, None, EEF_EGO, {"ego_motion_suspect"}, set()),
    ("eef_video_consistency", True, None, EEF_RECORD, {"inconsistent", "record_mismatch"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_CONFLICT, {"conflict"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_OPINIONS, {"inconsistent"}, {"AV-1"}),
    ("eef_video_consistency", True, None, EEF_CANNOT, set(), {"MV-4", "AV-1"}),
    ("duplicate_check", False, None, {"same_as": [43, 50], "group_id": 12}, {"duplicate"}, set()),
    ("duplicate_check", True, None, {"action_hash": "abc"}, set(), set()),

] + [("data_integrity", None if f["level"] == "suspect" else False, None, _integ(f), {code},
      {"FILE-7"})            # these fixtures measured no camera's size: FILE-7 is unassessed
     for f, code in INTEG_CASES] + [
    # the sizes measured are a reading, so FILE-7 is assessed - and one that disagrees is a finding
    ("data_integrity", None, None,
     {"outcome": "suspect", "reason": "x", "tiers": {"L1": True, "L2": False, "L3": False},
      "files": [], "resolution": {"wrist": [128, 96]}, "findings": [
          {"level": "suspect", "code": "resolution_mismatch", "tier": "L1", "camera": "wrist",
           "message": "wrist 相机实际是 128×96，元数据声明 64×48，对不上",
           "args": {"mismatches": [{"kind": "declared", "expected": [64, 48], "actual": [128, 96],
                                    "measured_from": "frame"}], "size_wh": [128, 96]}}]},
     {"resolution_mismatch"}, set()),
    ("data_integrity", True, None,
     {"outcome": "pass", "reason": "", "tiers": {"L1": True, "L2": False, "L3": False},
      "files": [], "resolution": {"wrist": [128, 96], "exterior": [128, 96]}, "findings": []},
     set(), set()),
]


def _record(module, passed, score, details, **kw):
    return record_from_struct(module, 7, {"passed": passed, "score": score, "detail": json.dumps(details)}, **kw)


@pytest.mark.parametrize("module,passed,score,details,codes,unassessable", CASES,
                         ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_each_answer_gives_its_findings(module, passed, score, details, codes, unassessable):
    rec = _record(module, passed, score, details)
    assert schemas.errors("cli/result-record.schema.json", rec) == []
    assert rec["status"] == "ok" and rec["details"] == details      # details stay as written
    assert {f["code"] for f in rec["findings"]} == codes
    assert {u["item"] for u in rec["unassessable"]} == unassessable
    covers = registry.get(module).covers
    assert rec["assessed"] == [i for i in covers if i not in unassessable]
    for f in rec["findings"]:
        spec = registry.finding_code(module, f["code"])
        assert f["item"] == spec.item and f["message_zh"]
    # the default policy keeps today's gates: a failed episode has a blocking finding, nothing else has - but the
    # EEF module rejects nothing (D81) and the duplicate check only reports (D87)
    # a failed sync record is asked, not rejected (7.5), like a duplicate
    assert (legacy_verdict(rec) == "fail") == (passed is False and module not in ("duplicate_check", "video_action_sync"))


#: codes no answer can raise yet; every module's codes have a fixture since the skill profile left
NO_SOURCE_YET: set[tuple[str, str]] = set()


#: what the decoder said about a corrupt video (D93): the frame stage and task success's video preparation
CORRUPT = [{"step": "decode", "camera": "wrist", "cause": "InvalidDataError: [Errno 1094995529] Invalid data found "
                                                       "when processing input: 'avcodec_send_packet()'"}]


@pytest.mark.parametrize("module", ["visual_quality", "video_action_sync", "task_success"])
def test_a_corrupt_video_is_a_blocking_finding_not_an_error(module):
    """D93 (design doc 28 §2): the decoder's InvalidDataError is the file's fault - ``decode_failed`` on the camera,
    blocking, not appealable; the other items unassessable; the incidents kept in the details."""
    rec = _record(module, None, None, {"reason": "解码失败"}, incidents=CORRUPT)
    assert schemas.errors("cli/result-record.schema.json", rec) == []
    assert rec["status"] == "ok" and rec["error"] is None
    assert [(f["code"], f["item"], f["scope"]) for f in rec["findings"]] == [("decode_failed", "FILE-4", {"camera": "wrist"})]
    assert rec["details"]["undecodable"] == CORRUPT
    assert {u["reason"] for u in rec["unassessable"]} == {"video_undecodable"}
    assert "FILE-4" in rec["assessed"] and "FILE-4" not in {u["item"] for u in rec["unassessable"]}
    assert legacy_verdict(rec) == "fail"


def test_a_frame_stage_that_judged_the_other_cameras_keeps_their_findings():
    """One camera of several corrupt: what the frame stage measured on the others stays, plus decode_failed."""
    rec = _record("visual_quality", None, 0.4, VQ, incidents=CORRUPT)
    assert {f["code"] for f in rec["findings"]} == {"frozen", "exposure_low", "sharpness_low", "information_death",
                                                   "dead_or_padded", "decode_failed"}
    assert rec["unassessable"] == []


@pytest.mark.parametrize("cause", ["TimeoutError: read timed out", "ConnectionResetError: [Errno 54] reset",
                                   "MemoryError", "HTTP 503 from tos"])
def test_a_failure_of_the_run_stays_an_error(cause):
    """Anything else - network, timeouts, memory - is still an execution error and waits for a retry (D33)."""
    rec = _record("visual_quality", None, None, {}, incidents=[{"step": "decode", "camera": "wrist", "cause": cause}])
    assert rec["status"] == "error" and rec["findings"] == []
    mixed = _record("visual_quality", None, None, {}, incidents=CORRUPT + [{"step": "read", "cause": cause}])
    assert mixed["status"] == "error"                       # one transient failure: the whole record waits


def test_every_code_is_drawn_from_a_real_answer():
    """F12.2 acceptance ②: every code of §2.2 has a fixture (dataset-level ones in the next test)."""
    seen = {(m, f["code"]) for m, passed, score, d, *_ in CASES for f in _record(m, passed, score, d)["findings"]}
    seen |= {(m, f["code"]) for m in ("visual_quality", "video_action_sync", "task_success")
             for f in _record(m, None, None, {}, incidents=CORRUPT)["findings"]}
    wanted = {(s.id, c.code) for s in registry.MODULES for c in s.codes if c.scope_kind != "dataset"}
    assert wanted - seen == NO_SOURCE_YET



def test_a_wrist_camera_off_its_poses_is_its_merged_cell():
    """design doc 22 §5.3 / 25 §7: a wrist camera's own motion is a merged cell like the others - one finding at
    its stretch, on the camera it is on; the camera's time offset makes AV-1 assessed; the episode is kept."""
    rec = _record("eef_video_consistency", True, None, EEF_EGO)
    (f,) = rec["findings"]
    assert f["code"] == "ego_motion_suspect" and f["item"] == "MV-4" and f["severity"] == "medium"
    assert f["time_s"] == [1.683, 8.35] and f["scope"] == {"camera": "robot0_camera0"}
    assert f["readings"]["subitem"] == "ego_motion" and f["readings"]["sources"]["cpu"]["verdict"] == "issue"
    assert rec["assessed"] == ["MV-4", "AV-1"] and legacy_verdict(rec) != "fail"
    calm = {**EEF_EGO, "merged": {"cells": [], "episode": {"label": "consistent", "p": 0.1, "flags": [], "conflicts": 0}}}
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
    opinion = _record("eef_video_consistency", True, None, EEF_OPINIONS)["findings"][0]
    assert opinion["time_s"] == [2.67, 6.33] and opinion["scope"] == {"camera": "ext"}
    lag = next(f for f in _record("video_action_sync", True, None, SYNC_ONE)["findings"] if f["code"] == "lag_inconsistent")
    assert lag["scope"] == {"cameras": ["front", "wrist"]}
    trunc = _record("data_integrity", False, None, _integ(INTEG_CASES[1][0]))["findings"][0]
    assert "time_s" not in trunc and trunc["readings"] == {"from_s": 2.6}          # a truncation runs to the end
    zero = _record("data_integrity", False, None, _integ(INTEG_CASES[3][0], files=[{"file": "v.mp4", "window_s": [0.5, 9.0]}]))
    assert zero["findings"][0]["time_s"] == [0.5, 1.5]                              # a shared file's window is subtracted
    dup = _record("duplicate_check", False, None, {"same_as": [50, 43], "group_id": 12})["findings"][0]
    assert dup["readings"] == {"group_id": 12, "same_as": [43, 50]}
    assert dup["message_zh"] == "与 ep000043、ep000050 完全相同（动作与各路画面字节一致）"


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
    rec = _record("timestamp_check", False, None, {"n": 3})                         # a fail it did not explain
    assert [f["code"] for f in rec["findings"]] == ["gap"] and legacy_verdict(rec) == "fail"
    rec = _record("data_integrity", False, None, _integ())
    assert [f["code"] for f in rec["findings"]] == ["structure_invalid"]
    rec = _record("task_success", None, None, {})
    assert [f["code"] for f in rec["findings"]] == ["uncertain"] and legacy_verdict(rec) == "abstain"


def test_context_and_errors():
    rec = _record("motion_quality", None, 0.9, MQ_STUCK, context={"action_semantics": {"source": "profile"}})
    assert rec["readings"]["action_semantics"] == {"source": "profile"}
    err = record_from_struct("task_success", 3, None, incidents=[{"step": "probe", "cause": "timeout"}])
    assert err["status"] == "error" and err["findings"] == [] and err["assessed"] == []
    assert schemas.errors("cli/result-record.schema.json", err) == []
    assert legacy_verdict(err) == "error"


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
    # 8.6: columns named as joints that carry a normalized opening (G1's right arm) - once per dataset, by name
    sem = {"source": "preflight", "action_space": "joint", "control_mode": "absolute", "undetermined": False,
           "nonjoint_columns": ["kRightShoulderRoll", "kRightElbow"]}
    recs = {e: _record("motion_quality", None, 0.9, MQ_STUCK, context={"action_semantics": sem}) for e in range(3)}
    found, _ = F.dataset_level("motion_quality", recs)
    assert [f["code"] for f in found] == ["columns_not_joints"] and found[0]["unit"] == "dataset"
    assert found[0]["readings"]["columns"] == ["kRightShoulderRoll", "kRightElbow"] and "kRightElbow" in found[0]["message_zh"]
    assert found[0]["item"] is None and found[0]["severity"] == "medium"
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
                     ("motion_quality", "action_semantics_undetermined"), ("motion_quality", "columns_not_joints"),
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
    items.add("ACT-6")                                # dataset level, test_dataset_level_findings
    assert {"LABEL-2", "IMG-3", "TASK-1", "AV-3", "MV-3", "ACT-6"} <= items
    assert "LABEL-1" not in items


def test_borrowed_peak_readings():
    """MV-3 by the borrowed peak: the finding names both cameras and carries the two correlations it compared;
    a flat curve is recorded as unverified instead."""
    rec = _record("video_action_sync", True, None, SYNC_BORROW)
    (f,) = [x for x in rec["findings"] if x["code"] == "lag_inconsistent"]
    assert f["scope"] == {"cameras": ["front", "wrist"]}
    r = f["readings"]
    assert r["ref_camera"] == "wrist" and r["camera"] == "front" and r["lag_ref_s"] == -0.97
    assert r["corr_max"] > 0.5 and r["corr_at_ref_lag"] < 0.2 and r["ratio"] >= 1.25
    assert "front" in f["message_zh"] and "0.97" in f["message_zh"]
    flat = _record("video_action_sync", True, None, SYNC_BORROW_FLAT)
    assert not [x for x in flat["findings"] if x["code"] == "lag_inconsistent"]
    assert flat["readings"]["mv3_unverified_cameras"] == ["front"]
    same = _record("video_action_sync", True, None, SYNC_BORROW_SAME)
    assert same["readings"]["mv3_unverified_cameras"] == ["front"]
    weak = _record("video_action_sync", True, None, SYNC_BORROW_WEAK)
    assert "mv3_unverified_cameras" not in weak["readings"]


def test_windowed_median_spread_readings():
    """MV-3 by the windowed medians carries both lags, the spread and the tolerance it beat; the whole-curve
    readings keep their own window summary."""
    rec = _record("video_action_sync", True, None, SYNC_WINDOWED)
    (f,) = [x for x in rec["findings"] if x["code"] == "lag_inconsistent"]
    r = f["readings"]
    assert r["basis"] == "windowed_median" and r["lag_front_s"] == -0.13 and r["lag_wrist_s"] == 0.07
    assert abs(r["spread_s"] - 0.2) < 1e-6 and abs(r["tol_s"] - 0.1) < 1e-6
    assert rec["readings"]["per_camera"]["front"]["lag_windows"]["n_trusted"] == 9


def test_a_single_camera_is_reported_from_its_steady_windows_too():
    """7.5: AV-1 from the camera's best reading - a camera whose whole-episode reading is not trusted but whose
    windows are steady beyond the tolerance is reported, with the basis in the readings; within it, nothing."""
    off = {**SYNC_WINDOWED, "per_camera": {**SYNC_WINDOWED["per_camera"],
           "front": {**SYNC_WINDOWED["per_camera"]["front"], "lag_windows": _lw(-0.4)}}}
    rec = _record("video_action_sync", True, None, off)
    (f,) = [x for x in rec["findings"] if x["code"] == "camera_misaligned"]
    assert f["message_zh"] == "front 比动作早 0.40 秒" and f["readings"]["basis"] == "windowed_median"
    rec = _record("video_action_sync", True, None, SYNC_WINDOWED)                       # -0.13: within 0.25 s
    assert not [x for x in rec["findings"] if x["code"] == "camera_misaligned"]


def test_windowed_stability_is_the_reading_s_word_and_the_task_s_lines(monkeypatch):
    """Registry 7.4: every camera's ``lag_windows`` reading says whether it is ``stable`` by the task's
    ``window_min_trusted`` / ``window_max_iqr_s``; the same lines decide which cameras the windowed comparison
    uses, and ``window_spread_tol_s`` what it reports (never below three frames)."""
    rec = _record("video_action_sync", True, None, SYNC_WINDOWED_WOBBLY)
    per = rec["readings"]["per_camera"]
    assert per["front"]["lag_windows"]["stable"] is False and per["wrist"]["lag_windows"]["stable"] is True
    assert not [x for x in rec["findings"] if x["code"] == "lag_inconsistent"]
    # a task that tolerates the wobble: front is stable, the 0.2 s spread is reported
    loose = _record("video_action_sync", True, None, SYNC_WINDOWED_WOBBLY, params={"window_max_iqr_s": 0.6})
    assert loose["readings"]["per_camera"]["front"]["lag_windows"]["stable"] is True
    assert [x for x in loose["findings"] if x["code"] == "lag_inconsistent"]
    # a task that wants more trusted windows than there are: nothing is stable, nothing is compared
    strict = _record("video_action_sync", True, None, SYNC_WINDOWED, params={"window_min_trusted": 12})
    assert all(c["lag_windows"]["stable"] is False for c in strict["readings"]["per_camera"].values())
    assert not [x for x in strict["findings"] if x["code"] == "lag_inconsistent"]
    # a wider spread line swallows the 0.2 s; a narrower one cannot go below three frames (0.1 s at 30 fps)
    wide = _record("video_action_sync", True, None, SYNC_WINDOWED, params={"window_spread_tol_s": 0.25})
    assert not [x for x in wide["findings"] if x["code"] == "lag_inconsistent"]
    narrow = _record("video_action_sync", True, None, SYNC_WINDOWED, params={"window_spread_tol_s": 0.02})
    (f,) = [x for x in narrow["findings"] if x["code"] == "lag_inconsistent"]
    assert abs(f["readings"]["tol_s"] - 0.1) < 1e-3                          # 3 × the rounded 0.0333 s frame

