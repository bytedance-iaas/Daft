"""数据集语义层验收:profile 命中/未知回退/新增数据集零改动/velocity stuck。"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.motion_quality import motion_quality
from curation.ingest import dataset_semantics as DS
from curation.ingest.dataset_semantics import resolve_semantics


def test_known_datasets_match_profiles():
    """5 个已知数据集都由 profile 权威解析(不靠指纹猜)。"""
    cases = {
        "unknown": (["motor_0", "motor_1"], "joint", "absolute"),
        "so100": (["main_shoulder_pan", "main_shoulder_lift", "main_elbow_flex",
                   "main_wrist_flex", "main_wrist_roll", "main_gripper"], "joint", "absolute"),
        "widowx": (["x", "y", "z", "roll", "pitch", "yaw", "gripper"], "ee", "delta"),
        "franka": (["x", "y", "z", "roll", "pitch", "yaw", "gripper"], "ee", "velocity"),
    }
    for robot, (anames, space, mode) in cases.items():
        info = {"robot_type": robot, "features": {"action": {"names": anames}}}
        s = resolve_semantics(info, np.zeros((30, len(anames))))
        assert s.source == "profile", f"{robot} 未命中 profile"
        assert s.action_space == space and s.control_mode == mode


def test_unknown_dataset_falls_back_to_inference():
    """未知机器人 → 数值指纹推断,不崩,标记 inferred。"""
    info = {"robot_type": "brand_new_arm", "features": {"action": {"names": ["j0", "j1"]}}}
    absolute = np.cumsum(np.random.default_rng(0).normal(0, 0.01, (60, 2)), axis=0) + 5.0
    s = resolve_semantics(info, absolute)
    assert s.source == "inferred"
    assert s.action_space == "joint" and s.control_mode == "absolute"


def test_new_dataset_profile_is_picked_up_zero_code(tmp_path, monkeypatch):
    """扩展性契约:丢一个新 YAML → 新数据集被正确解析,不改任何代码。"""
    prof_dir = tmp_path / "profiles"
    prof_dir.mkdir()
    (prof_dir / "newbot.yaml").write_text(
        "match: {robot_type: newbot, action_names: [a, b, c]}\n"
        "action: {space: joint, control_mode: velocity, gripper_dims: [2],"
        " stuck_strategy: velocity_dual_scale, unit: normalized}\n"
        "state: {space: joint}\n")
    monkeypatch.setattr(DS, "_PROFILE_DIR", str(prof_dir))
    info = {"robot_type": "newbot", "features": {"action": {"names": ["a", "b", "c"]}}}
    s = resolve_semantics(info, np.zeros((30, 3)))
    assert s.source == "profile" and s.profile_name == "newbot.yaml"
    assert s.control_mode == "velocity" and s.gripper_dims == (2,)
    assert s.stuck_strategy == "velocity_dual_scale"


def test_profile_does_not_leak_across_datasets(tmp_path, monkeypatch):
    """新增 profile 不影响不匹配的数据集(仍走推断)。"""
    prof_dir = tmp_path / "p"
    prof_dir.mkdir()
    (prof_dir / "newbot.yaml").write_text(
        "match: {robot_type: newbot, action_names: [a, b]}\n"
        "action: {space: ee, control_mode: velocity}\nstate: {space: ee}\n")
    monkeypatch.setattr(DS, "_PROFILE_DIR", str(prof_dir))
    other = {"robot_type": "different", "features": {"action": {"names": ["a", "b"]}}}
    s = resolve_semantics(other, np.cumsum(np.random.default_rng(1).normal(0, .01, (60, 2)), 0) + 3)
    assert s.source == "inferred"                    # robot_type 不匹配 → 不误用


def test_velocity_dual_scale_stuck():
    """velocity 策略:速度指令活跃但实际冻结 → stuck;正常跟随 → 不判。"""
    rng = np.random.default_rng(0)
    T = 100
    a = np.zeros((T, 7))
    a[:, 0] = rng.uniform(0.3, 0.9, T)               # x 持续有速度指令
    p = np.zeros((T, 8))
    p[:, 0] = np.cumsum(a[:, 0] * 0.015)             # 实际位移=速度×系数(正常跟随)
    clean = motion_quality(a, p, 15.0, gripper_dims=(6,), control_mode="velocity",
                           same_space=True, stuck_strategy="velocity_dual_scale")
    assert clean.detail["stuck"] == 1.0

    p2 = p.copy()
    p2[40:, 0] = p2[40, 0]                            # x 从帧40起冻结(指令仍在动)
    stuck = motion_quality(a, p2, 15.0, gripper_dims=(6,), control_mode="velocity",
                           same_space=True, stuck_strategy="velocity_dual_scale")
    assert stuck.detail["stuck"] == 0.0


def test_stuck_strategy_abstain():
    a = np.cumsum(np.random.default_rng(0).normal(0, 0.01, (60, 3)), axis=0)
    p = a + 0.001
    r = motion_quality(a, p, 15.0, control_mode="unknown", same_space=True,
                       stuck_strategy="abstain")
    assert r.detail["stuck"] is None
