"""EE 列布局识别与非 rpy 姿态表示(2026-09-16 umi 教训):此前 EE 数据一律把第 3-5 列当 rpy
三元组做姿态里程表,umi 的 rot6d 分量被当欧拉角,指令与读数第 3 列差出十几倍量程,
执行器饱和整批归零。现在:①布局按 names 识别;②里程表按表示法算;③指令与读数同源时
饱和/卡顿判不适用;④路径效率只在平移列上算。老数据集(有档案)走老规则,数值不变。"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from curation.core.checks.motion_quality import _rotation_sequence, motion_quality
from curation.ingest.dataset_semantics import (_names_look_ee, detect_ee_layout,
                                               resolve_semantics)

UMI_NAMES = [f"robot{r}_{n}" for r in (0, 1)
             for n in ("pos_x", "pos_y", "pos_z", "rot6d_0", "rot6d_1", "rot6d_2",
                       "rot6d_3", "rot6d_4", "rot6d_5", "gripper_width")]


def test_layout_from_names_recognises_rot6d_blocks_and_grippers():
    lay = detect_ee_layout(UMI_NAMES, 20)
    assert lay["rotation_blocks"] == [(3, 6, "rot6d"), (13, 6, "rot6d")]
    assert lay["gripper_dims"] == (9, 19)
    assert lay["translation_dims"] == (0, 1, 2, 10, 11, 12)
    assert lay["euler_triplet"] is False
    assert lay["source"] == "names"


@pytest.mark.parametrize("names,dim,blocks,grip", [
    (["x", "y", "z", "roll", "pitch", "yaw", "gripper"], 7, [(3, 3, "rpy")], (6,)),
    (["x", "y", "z", "qx", "qy", "qz", "qw", "gripper"], 8, [(3, 4, "quat")], (7,)),
    (None, 7, [(3, 3, "rpy")], (6,)),            # 没 names:经典 6/7 维保留老规则
    (None, 20, [], ()),                          # 没 names 且不是经典维数:不把任何列当角度
])
def test_layout_fallbacks(names, dim, blocks, grip):
    lay = detect_ee_layout(names, dim)
    assert lay["rotation_blocks"] == blocks and lay["gripper_dims"] == grip


def test_prefixed_names_count_as_ee_but_joint_names_do_not():
    assert _names_look_ee([n.lower() for n in UMI_NAMES])
    assert not _names_look_ee(["shoulder_pan.pos", "wrist_roll.pos", "gripper.pos"])
    assert not _names_look_ee(["motor_0", "motor_1"])


def test_resolve_semantics_fallback_writes_layout_into_extras():
    info = {"robot_type": "some_new_arm",
            "features": {"action": {"names": UMI_NAMES}, "observation.state": {"names": UMI_NAMES}}}
    a = np.cumsum(np.random.default_rng(0).normal(size=(50, 20)) * 0.01, axis=0)
    sem = resolve_semantics(info, a, "nope")
    assert sem.action_space == "ee" and sem.source == "inferred"
    assert sem.extras["layout"]["rotation_blocks"] == [[3, 6, "rot6d"], [13, 6, "rot6d"]]
    assert sem.gripper_dims == (9, 19)


def _rot6d_series(n: int, turn: float):
    """绕 z 匀速转 turn 弧度的 rot6d 序列(旋转矩阵前两列)。"""
    ang = np.linspace(0.0, turn, n)
    mats = Rotation.from_euler("z", ang).as_matrix()
    return np.concatenate([mats[:, :, 0], mats[:, :, 1]], axis=1)


def test_rotation_sequence_rot6d_and_quat_give_true_turn():
    r6 = _rotation_sequence(_rot6d_series(30, 1.2), "rot6d")
    assert abs((r6[0].inv() * r6[-1]).magnitude() - 1.2) < 1e-6
    q = Rotation.from_euler("z", np.linspace(0, 0.7, 20)).as_quat()
    rq = _rotation_sequence(q, "quat")
    assert abs((rq[0].inv() * rq[-1]).magnitude() - 0.7) < 1e-6
    assert _rotation_sequence(np.zeros((5, 6)), "rot6d") is None      # 退化不硬算
    assert _rotation_sequence(np.zeros((5, 5)), "weird") is None


def _umi_like(n: int = 240, fps: float = 30.0):
    """umi 样式:每手 xyz+rot6d+夹爪,action ≡ 下一帧 state。"""
    rng = np.random.default_rng(1)
    t = np.linspace(0, 1, n)[:, None]
    xyz = np.concatenate([0.3 * np.sin(2 * np.pi * t), 0.1 * t, 0.2 * np.cos(2 * np.pi * t)], axis=1)
    r6 = _rot6d_series(n, 2.5)
    grip = 0.05 + 0.04 * (t[:, 0] > 0.5)
    hand = np.concatenate([xyz, r6, grip[:, None]], axis=1)
    state = np.concatenate([hand, hand + 0.01], axis=1) + rng.normal(size=(n, 20)) * 1e-4
    action = np.concatenate([state[1:], state[-1:]], axis=0)          # 指令 = 下一帧读数
    return action, state, fps


LAYOUT = detect_ee_layout(UMI_NAMES, 20)


def test_same_source_guard_makes_saturation_and_stuck_not_applicable():
    action, state, fps = _umi_like()
    res = motion_quality(action, state, fps, gripper_dims=LAYOUT["gripper_dims"],
                         angle_dims=LAYOUT["angle_dims"], angle_mode="absolute",
                         euler_triplet=False, control_mode="absolute", same_space=True,
                         rotation_blocks=LAYOUT["rotation_blocks"],
                         translation_dims=LAYOUT["translation_dims"])
    d = res.detail
    assert d["same_source"] is True
    assert d["actuator_saturation"] is None and "同源" in d["saturation_reason"]
    assert d["stuck"] is None and "同源" in d["stuck_reason"]
    assert d["path_efficiency_cols"] == "translation"
    assert res.score > 0.7, "饱和不再拉零分:总分只按平滑度/尖刺/夹爪算"


def test_rot6d_block_no_longer_inflates_saturation_gap_when_tracking_lags():
    """真控制器(指令领先读数一帧,带跟踪误差)也不能因为旋转块被当 rpy 而饱和归零。"""
    action, state, fps = _umi_like()
    lagged = np.concatenate([state[:1], state[:-1]], axis=0)          # 读数落后一帧
    lagged = lagged + 0.002 * np.sign(action - lagged)                 # 再带一点跟踪误差
    res = motion_quality(action, lagged, fps, gripper_dims=LAYOUT["gripper_dims"],
                         angle_dims=LAYOUT["angle_dims"], angle_mode="absolute",
                         euler_triplet=False, control_mode="absolute", same_space=True,
                         rotation_blocks=LAYOUT["rotation_blocks"],
                         translation_dims=LAYOUT["translation_dims"])
    d = res.detail
    assert not d.get("same_source")
    assert d["actuator_saturation"] is not None and d["actuator_saturation"] > 0.8
    assert d["saturation_gap_ratio"] < 0.1, "直比只用平移+夹爪列,不含里程表列"


def test_legacy_rpy_path_unchanged_without_rotation_blocks():
    """老调用方式(angle_dims + euler_triplet,没有 rotation_blocks)照旧可用。"""
    n = 120
    t = np.linspace(0, 1, n)[:, None]
    a = np.concatenate([t, t * 0.5, -t, 0.3 * t, 0.2 * t, 0.1 * t, (t > 0.5) * 1.0], axis=1)
    s = np.concatenate([a[:1], a[:-1]], axis=0) + 0.01
    res = motion_quality(a, s, 10.0, gripper_dims=(6,), angle_dims=(3, 4, 5),
                         angle_mode="absolute", euler_triplet=True, control_mode="absolute",
                         same_space=True)
    assert res.detail["actuator_saturation"] is not None
    assert "path_efficiency_cols" not in res.detail


# ---- 2026-10-14:没有列名时按数值认四元数块与夹爪(RH20T) -----------------------------------

def _rh20t_like_action(t: int = 120):
    tt = np.linspace(0, 2 * np.pi, t)
    xyz = np.stack([0.5 + 0.1 * np.sin(tt), 0.1 * np.cos(tt), 0.17 + 0.05 * np.sin(2 * tt)], 1)
    q = Rotation.from_euler("xyz", np.stack([0.1 * np.sin(tt), 0.2 * np.cos(tt), np.pi + 0.3 * np.sin(tt)], 1)).as_quat()
    q[(np.arange(t) // 30) % 2 == 1] *= -1
    grip = np.where(np.arange(t) < 60, 95.0, 0.0)
    return np.column_stack([xyz, q, grip]).astype(np.float32)


def test_numeric_layout_finds_the_unit_quaternion_block_and_the_switch_gripper():
    from curation.ingest.dataset_semantics import detect_ee_layout_numeric
    lay = detect_ee_layout_numeric(_rh20t_like_action(), 8)
    assert lay["rotation_blocks"] == [(3, 4, "quat")] and lay["angle_dims"] == (3, 4, 5, 6)
    assert lay["translation_dims"] == (0, 1, 2) and lay["gripper_dims"] == (7,)
    assert lay["source"] == "numeric" and lay["euler_triplet"] is False


def test_numeric_layout_does_not_mistake_rpy_or_joints_for_a_quaternion():
    from curation.ingest.dataset_semantics import detect_ee_layout_numeric
    a = _rh20t_like_action()
    rpy = np.column_stack([a[:, :3], Rotation.from_quat(a[:, 3:7] * np.sign(a[:, 6:7])).as_euler("xyz"), a[:, 7] / 95.0])
    assert detect_ee_layout_numeric(rpy, 7) is None
    joints = np.cumsum(np.random.default_rng(0).normal(size=(120, 6)) * 0.01, axis=0)
    assert detect_ee_layout_numeric(joints, 6) is None


def test_reader_layout_without_names_uses_the_numeric_fingerprint_and_keeps_the_preflight_gripper():
    from curation.ingest.lerobot_reader import resolve_dataset_semantics
    a = _rh20t_like_action()
    s = np.column_stack([a[:, :7], np.zeros((len(a), 7)), a[:, 7]])     # like RH20T's 15-column state
    info = {"robot_type": "flexiv", "fps": 10,
            "features": {"action": {"shape": [8]}, "observation.state": {"shape": [15]}}}
    rows = [{"action": a, "proprio_state": s, "fps": 10.0}] * 2
    sem = resolve_dataset_semantics(info, rows, "rh20t_like")
    lay = sem.extras.get("layout") or {}
    assert lay.get("rotation_blocks") == [[3, 4, "quat"]] and lay.get("source") == "numeric"
    assert tuple(lay.get("gripper_dims") or ()) == (7,) and sem.gripper_dims == (7,)


# ---- F8 (2026-10-14 HIW-500):预检判不出整列空间,列名能分块就按块算 ------------------------------

HIW_NAMES = ["pivot_vx", "pivot_vy", "pivot_vyaw", "pivot_roll", "pivot_pitch", "pivot_yaw", "pivot_height",
             "left_ee_x", "left_ee_y", "left_ee_z", "left_ee_roll", "left_ee_pitch", "left_ee_yaw",
             "right_ee_x", "right_ee_y", "right_ee_z", "right_ee_roll", "right_ee_pitch", "right_ee_yaw",
             "left_trigger", "left_squeeze", "right_trigger", "right_squeeze"]


def _hiw_like(t: int = 240, seed: int = 0):
    rng = np.random.default_rng(seed); tt = np.linspace(0, 4 * np.pi, t)
    a = np.zeros((t, 23))
    a[:, 0:3] = np.where(rng.random((t, 3)) < 0.08, rng.normal(0, 0.3, (t, 3)), 0.0)       # joystick velocities
    a[:, 3:7] = 0.1 * np.sin(tt[:, None] + np.arange(4))                                   # torso rpy + height
    for base in (7, 13):
        a[:, base:base + 3] = 0.2 * np.sin(tt[:, None] + np.arange(3) + base)              # ee xyz
        a[:, base + 3:base + 6] = 0.3 * np.sin(0.5 * tt[:, None] + np.arange(3))           # ee rpy
    a[:, 19:23] = np.where(np.arange(t)[:, None] < t // 2, [10.0, 1.0, 10.0, 1.0], [0.0, 0.0, 0.0, 0.0])
    return a


def test_names_layout_classifies_velocities_pose_blocks_and_is_informative():
    lay = detect_ee_layout(HIW_NAMES, 23)
    assert lay["velocity_dims"] == (0, 1, 2) and lay["informative"] is True
    assert lay["rotation_blocks"] == [(3, 3, "rpy"), (10, 3, "rpy"), (16, 3, "rpy")]
    assert lay["translation_dims"] == (7, 8, 9, 13, 14, 15) and lay["gripper_dims"] == (19, 20, 21, 22)


def test_reader_keeps_a_names_only_layout_when_the_preflight_has_no_verdict():
    from curation.ingest.lerobot_reader import resolve_dataset_semantics
    a = _hiw_like(); s = np.cumsum(np.random.default_rng(1).normal(size=(len(a), 29)) * 0.01, axis=0)  # 29 joints
    info = {"robot_type": "unitree_g1", "fps": 30,
            "features": {"action": {"shape": [23], "names": HIW_NAMES}, "observation.state": {"shape": [29]}}}
    sem = resolve_dataset_semantics(info, [{"action": a, "proprio_state": s, "fps": 30.0}] * 2, "hiw_like")
    assert sem.action_space == "unknown" and sem.source == "preflight_unknown"
    lay = sem.extras["layout"]
    assert lay["names_only"] is True and lay["informative"] is True and lay["velocity_dims"] == [0, 1, 2]


def test_no_names_and_no_verdict_still_has_no_layout():
    from curation.ingest.lerobot_reader import resolve_dataset_semantics
    a = _hiw_like(); s = np.cumsum(np.random.default_rng(1).normal(size=(len(a), 29)) * 0.01, axis=0)
    info = {"robot_type": "unitree_g1", "fps": 30, "features": {"action": {"shape": [23]}, "observation.state": {"shape": [29]}}}
    sem = resolve_dataset_semantics(info, [{"action": a, "proprio_state": s, "fps": 30.0}] * 2, "hiw_like")
    assert sem.action_space == "unknown" and not (sem.extras or {}).get("layout")
