"""stuck 冻结包络验收(2026-07-15 用户定):证据段定罪,包络对齐视频观感。

动因:droid ep89 报告 stuck 3s,用户视频复核"不止三秒"——之后操作员停手、手臂
仍静止约 5s,画面上与 stuck 无异。包络 = 证据段前后连续静止的总窗口。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.motion_quality import motion_quality

FPS = 15.0


def _stuck_traj(T=400, freeze=(100, 300), push=(120, 165)):
    """velocity 控制合成:实际在 freeze 区间冻结,指令只在 push 区间发力。

    push ⊂ freeze → 证据段=push 长度,包络=freeze 长度。
    """
    rng = np.random.default_rng(0)
    t = np.linspace(0, 4 * np.pi, T)
    state = np.zeros((T, 3))
    for j in range(3):
        state[:, j] = 0.3 * np.sin(t + j)
    state[freeze[0]:freeze[1]] = state[freeze[0]]          # 实际冻结
    state += rng.normal(0, 1e-6, state.shape)
    act = np.abs(np.diff(state, axis=0, prepend=state[:1])) * 3.0 + 0.01  # 平时有速度指令
    act[freeze[0]:freeze[1]] = 0.0                          # 冻结期操作员默认没推
    act[push[0]:push[1], 1] = 0.5                           # 只在 push 窗口猛推 y 轴
    return act, state


def test_envelope_longer_than_evidence():
    act, state = _stuck_traj()
    d = motion_quality(act, state, FPS, control_mode="velocity",
                       stuck_strategy="velocity_dual_scale").detail
    assert d["stuck"] == 0.0
    sj = [s for s in d["stuck_joints"] if s["joint"] == 1][0]
    # 证据段 ≈ push 窗(45 帧);包络 ≈ freeze 窗(200 帧)
    assert 35 <= sj["max_dead_run"] <= 50
    assert sj["envelope_frames"] >= 4 * sj["max_dead_run"]
    # 窗口净位移判据的边界分辨率=±1窗(15帧):"从 t 起 1s 无净进展"在冻结开始
    # 前一点点就可能为真(走一小步就冻住),属语义软化非误差
    assert abs(sj["envelope_start_frame"] - 100) <= 18
    assert sj["envelope_frames"] >= 180


def test_envelope_equals_evidence_when_no_idle_around():
    """指令几乎全程在推(冻结段≈证据段)→ 包络≈证据段,不虚增。

    (push 窗占比须 <50%:活跃线=指令中位数,推力帧过半会把中位数顶成推力值本身)"""
    act, state = _stuck_traj(push=(100, 280))
    d = motion_quality(act, state, FPS, control_mode="velocity",
                       stuck_strategy="velocity_dual_scale").detail
    sj = [s for s in d["stuck_joints"] if s["joint"] == 1][0]
    assert sj["envelope_frames"] <= sj["max_dead_run"] * 1.15 + 8


def _droid_ep(idx):
    import json as _j
    from curation.ingest.lerobot_reader import read_lerobot_rows
    r = read_lerobot_rows("/data03/hao/data/droid_lerobot", episode_indices={idx},
                          skip_missing=True)[0]
    ex = _j.loads(r.get("semantics_extras") or "{}")
    vs = tuple(ex.get("velocity_scale_translation_empirical") or ()) or None
    d = motion_quality(np.asarray(r["action"], float),
                       np.asarray(r["proprio_state"], float), r["fps"],
                       gripper_dims=r["gripper_dims"], control_mode=r["control_mode"],
                       angle_dims=r["angle_dims"], euler_triplet=r["euler_triplet"],
                       stuck_strategy=r["stuck_strategy"], velocity_scale=vs).detail
    from curation.dataset_level.stuck_events import build_stuck_events
    return build_stuck_events(d.get("stuck_joints") or [], r["fps"])


@pytest.mark.skipif(not os.path.exists("/data03/hao/data/droid_lerobot/meta"),
                    reason="无 droid 数据")
def test_ep89_matches_video():
    """真数据锚定:ep89 用户视频复核='11.7s 起卡了不止 3 秒'(期望位移判据下
    证据=响应失效全程 ~5-6s),其后仍有操作员停手的 idle 段。"""
    evs = _droid_ep(89)
    tot = sum(e["stuck_seconds"] for e in evs)
    assert 4.5 <= tot <= 7.5, f"ep89 stuck 总时长漂移: {tot}"
    main = max(evs, key=lambda e: e["stuck_seconds"])
    assert 10.0 <= main["stuck_start_sec"] <= 12.0
    assert main["freeze_total_seconds"] >= main["stuck_seconds"]
    assert main["timeline"][-1]["state"] == "idle"        # 停手段仍在剧本尾部


@pytest.mark.skipif(not os.path.exists("/data03/hao/data/droid_lerobot/meta"),
                    reason="无 droid 数据")
def test_ep23_creep_is_stuck():
    """真数据锚定:ep23 用户视频复核='全程手臂没动'(蠕动 0.5cm/s vs 期望 ~10cm/s,
    响应效率 4%)。旧净位移判据只判 1.2s/新期望位移判据应覆盖大半条。"""
    evs = _droid_ep(23)
    tot = sum(e["stuck_seconds"] for e in evs)
    assert tot >= 8.0, f"ep23 蠕动卡死漏判: 仅 {tot}s(全长 12.8s)"


def test_multi_segment_same_axis_all_reported():
    """同一轴两段 stuck(中隔真实运动)→ 两段都报(旧实现只留最长段,timeline 会说谎)。"""
    act, state = _stuck_traj(T=800, freeze=(100, 200), push=(110, 160))
    # 再造第二段:400~500 冻结,410~455 推
    state[400:500] = state[400]
    act[400:500] = 0.0
    act[410:455, 1] = 0.5
    d = motion_quality(act, state, FPS, control_mode="velocity",
                       stuck_strategy="velocity_dual_scale").detail
    segs = [s for s in d["stuck_joints"] if s["joint"] == 1]
    assert len(segs) == 2, f"两段只报了 {len(segs)}: {segs}"
    assert segs[0]["segment"] == 0 and segs[1]["segment"] == 1
    assert abs(segs[1]["freeze_start_frame"] - 410) <= 3


def test_quiet_gap_splits_evidence():
    """指令安静 >0.5s 打断证据段:安静期不能定罪(ep89 曾把 4.7s 无指令圈进证据)。"""
    act, state = _stuck_traj(T=600, freeze=(100, 400), push=(110, 160))
    act[300:350, 1] = 0.5                        # 冻结后段再推一阵(dead 50帧,够格)
    d = motion_quality(act, state, FPS, control_mode="velocity",
                       stuck_strategy="velocity_dual_scale").detail
    segs = [s for s in d["stuck_joints"] if s["joint"] == 1]
    assert len(segs) == 2                        # 中间 140 帧安静 → 拆两段
    assert segs[0]["freeze_end_frame"] <= 165    # 第一段不吞安静期
    assert 295 <= segs[1]["freeze_start_frame"] <= 305


def test_timeline_chronology():
    """timeline:按时间排序、状态交替、帧与秒自洽、stuck 段等于证据并集。"""
    from curation.dataset_level.stuck_events import build_stuck_events
    sjs = [{"axis": "y", "segment": 0, "max_dead_run": 45, "freeze_start_frame": 176,
            "freeze_end_frame": 230, "envelope_start_frame": 168, "envelope_frames": 139},
           {"axis": "z", "segment": 0, "max_dead_run": 37, "freeze_start_frame": 177,
            "freeze_end_frame": 214, "envelope_start_frame": 172, "envelope_frames": 120}]
    evs = build_stuck_events(sjs, 15.0)
    assert len(evs) == 1 and evs[0]["axes"] == ["y", "z"]
    tl = evs[0]["timeline"]
    assert [s["state"] for s in tl] == ["idle", "stuck", "idle"]
    for a, b in zip(tl, tl[1:]):
        assert a["end_s"] == b["start_s"] and a["frames"][1] == b["frames"][0]  # 首尾相接
    stuck_seg = tl[1]
    assert stuck_seg["frames"] == [176, 230]     # y∪z 证据并集
    assert "idle_before_seconds" not in evs[0]   # 冗余摘要已删(timeline 是唯一细节源)


def test_amplitude_floor_deadband_nudge_not_convicted():
    """幅度地板(M2 全量程 2%):死区级微动(1%量程)被无视≠卡死(so101 ep18 教训)。"""
    T, FPSL = 300, 30.0
    rng = np.random.default_rng(1)
    q = np.zeros((T, 3)); q[:, 0] = np.linspace(0, 50, T)     # 关节0 正常动
    cmd = q.copy()
    cmd[100:160, 1] = 2.0                                      # 关节1: 微动指令(全量程200的1%)
    state = q + rng.normal(0, 1e-4, q.shape)                   # 关节1 实际没理它
    d = motion_quality(cmd, state, FPSL, control_mode="absolute",
                       joint_spans=(200.0, 200.0, 200.0)).detail
    assert d["stuck"] == 1.0, f"死区微动被定罪: {d.get('stuck_joints')}"


def test_duration_tier_short_event_low_confidence():
    """时长分级:1.2s 大幅度事件 → 低置信记录,不定罪;2.5s → 定罪。"""
    act, state = _stuck_traj(T=400, freeze=(100, 300), push=(110, 128))   # 1.2s@15fps
    d = motion_quality(act, state, FPS, control_mode="velocity",
                       stuck_strategy="velocity_dual_scale").detail
    assert d["stuck"] == 1.0
    assert len(d.get("stuck_low_confidence", [])) >= 1
    act2, state2 = _stuck_traj(T=400, freeze=(100, 300), push=(110, 148))  # 2.5s
    d2 = motion_quality(act2, state2, FPS, control_mode="velocity",
                        stuck_strategy="velocity_dual_scale").detail
    assert d2["stuck"] == 0.0
