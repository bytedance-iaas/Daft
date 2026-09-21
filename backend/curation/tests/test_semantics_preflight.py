"""动作语义预检(2026-09-02):合成数据验 5 种假设 + 与 profile 声明的一致性 + 读路集成。"""
from __future__ import annotations

import numpy as np
import pytest

from curation.ingest.semantics_preflight import agrees_with, preflight
from curation.ingest.dataset_semantics import DatasetSemantics

FRANKA = [(-2.8973, 2.8973), (-1.7628, 1.7628), (-2.8973, 2.8973), (-3.0718, -0.0698),
          (-2.8973, 2.8973), (-0.0175, 3.7525), (-2.8973, 2.8973)]
FPS = 15.0


def _smooth(rng, n, k, scale):
    w = rng.normal(0, 1, (n, k))
    return np.stack([np.convolve(w[:, j], np.ones(12) / 12, mode="same") for j in range(k)], 1) * scale


def joint_abs_rows(n_eps=6):
    rng = np.random.default_rng(0)
    rows = []
    for e in range(n_eps):
        n = 200
        mid = np.array([(lo + hi) / 2 for lo, hi in FRANKA])
        amp = np.array([(hi - lo) / 2 * 0.5 for lo, hi in FRANKA])
        a = mid + amp * np.tanh(_smooth(rng, n, 7, 2.0))
        s = a + rng.normal(0, 0.005, a.shape)
        rows.append({"action": a.astype(np.float32), "proprio_state": s.astype(np.float32), "fps": FPS})
    return rows


def ee_increment_rows(n_eps=6):
    """droid/libero 型:归一化末端增量/速度指令 + 米制位姿读数(积分而来)。"""
    rng = np.random.default_rng(1)
    rows = []
    for e in range(n_eps):
        n = 240
        cmd = np.zeros((n, 7))
        cmd[:, :3] = np.clip(_smooth(rng, n, 3, 2.5), -0.9, 0.9)
        cmd[:, 3:6] = rng.normal(0, 0.05, (n, 3))
        cmd[:, 6] = (rng.random(n) > 0.7).astype(float)
        v = np.array([0.18, 0.23, 0.28]) * cmd[:, :3] + rng.normal(0, 0.01, (n, 3))
        pos = np.cumsum(v, axis=0) / FPS + np.array([0.4, 0.0, 0.5])
        s = np.zeros((n, 8)); s[:, :3] = pos; s[:, 3:6] = rng.normal(0, 0.01, (n, 3)); s[:, 7] = cmd[:, 6]
        rows.append({"action": cmd.astype(np.float32), "proprio_state": s.astype(np.float32), "fps": FPS})
    return rows


def ee_abs_rows(n_eps=6):
    rng = np.random.default_rng(2)
    rows = []
    for e in range(n_eps):
        n = 200
        pos = np.cumsum(_smooth(rng, n, 3, 0.02), axis=0) + np.array([0.4, 0.0, 0.5])
        a = np.zeros((n, 7)); a[:, :3] = pos + rng.normal(0, 0.002, (n, 3)); a[:, 3:6] = 0.1
        s = np.zeros((n, 8)); s[:, :3] = pos; s[:, 3:6] = 0.1
        rows.append({"action": a.astype(np.float32), "proprio_state": s.astype(np.float32), "fps": FPS})
    return rows


def test_joint_absolute_recognized():
    pf = preflight(joint_abs_rows(), joint_limits=FRANKA, dof=7, gripper_hint=(6,))
    assert pf["status"] == "confident" and pf["hypothesis"] == "joint_absolute", pf["scores"]
    assert pf["action_space"] == "joint" and pf["control_mode"] == "absolute"


def test_ee_increment_recognized_even_with_franka_limits():
    """libero 原型:7 维末端增量恰与 Franka 7 关节同维;增量≈0 从第 0 帧起就在 joint4
    极限外 → 关节-绝对假设崩;末端增量与位姿差分高相关 → 末端-增量胜出。"""
    pf = preflight(ee_increment_rows(), joint_limits=FRANKA, dof=7, gripper_hint=(6,))
    assert pf["status"] == "confident" and pf["hypothesis"] == "ee_increment", pf["scores"]
    assert pf["action_space"] == "ee" and pf["control_mode"] == "delta" and pf["fit"] > 0.8
    assert pf["scores"]["joint_absolute"] < 0.3


def test_ee_absolute_recognized():
    pf = preflight(ee_abs_rows(), joint_limits=FRANKA, dof=7, gripper_hint=(6,))
    assert pf["status"] == "confident" and pf["hypothesis"] == "ee_absolute", pf["scores"]


def test_garbage_is_unknown_not_guessed():
    rng = np.random.default_rng(3)
    rows = [{"action": rng.normal(0, 5, (100, 7)).astype(np.float32),
             "proprio_state": rng.normal(0, 5, (100, 8)).astype(np.float32), "fps": FPS}
            for _ in range(4)]
    pf = preflight(rows, joint_limits=FRANKA, dof=7, gripper_hint=(6,))
    assert pf["status"] in ("none", "ambiguous") and pf["action_space"] == "unknown"
    assert preflight([])["status"] == "none"


def test_agreement_with_profile_treats_velocity_and_delta_alike():
    pf = preflight(ee_increment_rows(), joint_limits=FRANKA, dof=7, gripper_hint=(6,))
    droid = DatasetSemantics(action_space="ee", proprio_space="ee", control_mode="velocity")
    aloha = DatasetSemantics(action_space="joint", proprio_space="joint", control_mode="absolute")
    assert agrees_with(pf, droid) is True and agrees_with(pf, aloha) is False
    assert agrees_with({"status": "none"}, droid) is None


def test_reader_uses_preflight_when_profiles_ignored(monkeypatch):
    """读路集成:忽略 profile 后 pusht(关节-绝对)由预检判出,来源标 preflight;
    命中 profile 时只留核对结果。"""
    import os
    pusht = next((p for p in ("/data03/hao/data/pusht", "/mnt/tos/datasets/pusht")
                  if os.path.exists(p)), None)
    if pusht is None:
        pytest.skip("pusht 数据未下载")
    import json
    from curation.ingest.lerobot_reader import read_lerobot_meta
    with_profile = read_lerobot_meta(pusht, max_episodes=3)
    ex = json.loads(with_profile[0]["semantics_extras"])
    assert with_profile[0]["semantics_source"] == "profile"
    assert ex["semantics_preflight"]["agrees_with_profile"] in (True, None)
    monkeypatch.setenv("CURATION_SEMANTICS_IGNORE_PROFILES", "1")
    rows = read_lerobot_meta(pusht, max_episodes=3)
    ex2 = json.loads(rows[0]["semantics_extras"])
    assert rows[0]["semantics_source"] in ("preflight", "preflight_unknown")
    assert "semantics_preflight" in ex2 and "profile_name" not in ex2


def test_report_semantics_line_four_variants():
    from curation.export.report import semantics_line
    base = {"action_space": "ee", "control_mode": "velocity", "proprio_space": "ee"}
    assert semantics_line({**base, "source": "profile", "profile_name": "droid.yaml",
                           "preflight": {"agrees_with_profile": True, "fit": 0.9}}) \
        == "- **动作含义**: 末端速度指令 / 状态:末端位姿(已登记格式 droid)"
    assert semantics_line({**base, "control_mode": "delta", "source": "preflight",
                           "preflight": {"fit": 0.91}}) \
        == "- **动作含义**: 末端位移增量 / 状态:末端位姿(系统判断,吻合度 0.91;如不符请登记格式)"
    assert semantics_line({"action_space": "unknown", "control_mode": "unknown",
                           "proprio_space": "unknown", "source": "preflight_unknown"}) \
        == "- **动作含义**: 无法判断——已跳过运动学与相关运动质量项,请提供动作定义"
    assert semantics_line({**base, "source": "profile", "profile_name": "droid.yaml",
                           "preflight": {"agrees_with_profile": False, "fit": 0.12}}) \
        == "- **动作含义**: 按登记为末端速度指令 / 状态:末端位姿,但与数据吻合度仅 0.12,请核对数据集版本"
    assert semantics_line(None) == ""
