"""执行器饱和的速度域换算(2026-09-02):合成"归一化速度指令 × 米制位姿"数据,已知增益/延迟。"""
from __future__ import annotations

import numpy as np

from curation.core.checks.motion_quality import motion_quality
from curation.core.checks.velocity_calibration import (fit_velocity_gain, saturation_ratio)
from curation.ingest.dataset_semantics import DatasetSemantics, attach_velocity_calibration

G = np.array([0.18, 0.23, 0.28])      # 真增益(米/秒 每单位指令),延迟 1 帧
FPS = 15.0


def _episode(seed: int, n: int = 240, lag: int = 1, clip: float | None = None) -> dict:
    """指令=平滑随机过程(±0.9);实际速度=g·指令(滞后 lag 帧)+噪声;clip=实际速度上限
    (相对期望速度的比例,模拟执行器饱和:指令再大也只跟到这么快)。"""
    rng = np.random.default_rng(seed)
    cmd = np.zeros((n, 7))
    for j in range(3):
        w = rng.normal(0, 1, n)
        cmd[:, j] = np.clip(np.convolve(w, np.ones(12) / 12, mode="same") * 2.5, -0.9, 0.9)
    cmd[:, 3:6] = rng.normal(0, 0.05, (n, 3))
    v = G * cmd[:, :3]
    if clip is not None:
        cap = clip * np.abs(v).max(axis=0)
        v = np.clip(v, -cap, cap)
    v = v + rng.normal(0, 0.01, v.shape)
    # 实际速度序列 w[t] = (pos[t+1]-pos[t])·fps = g·cmd[t-lag]:滞后 lag 帧
    w = np.vstack([np.zeros((lag, 3)), v])[:n - 1]
    pos = np.zeros((n, 3))
    pos[1:] = np.cumsum(w, axis=0) / FPS
    pos += np.array([0.4, 0.0, 0.5])
    state = np.zeros((n, 8))
    state[:, :3] = pos
    state[:, 3:6] = rng.normal(0, 0.01, (n, 3))
    return {"episode_id": f"ep{seed:06d}", "action": cmd.astype(np.float32),
            "proprio_state": state.astype(np.float32), "fps": FPS}


def test_fit_recovers_gain_and_lag():
    calib = fit_velocity_gain([_episode(s) for s in range(12)])
    assert calib is not None and calib["lag_frames"] == 1
    assert np.allclose(calib["gain"], G, rtol=0.12), calib["gain"]
    assert min(calib["r"]) > 0.9 and calib["n_episodes"] == 12
    assert 0 <= calib["baseline"]["median"] < 0.15


def test_saturated_episode_stands_out_from_baseline():
    clean = [_episode(s) for s in range(12)]
    calib = fit_velocity_gain(clean)
    bad = _episode(99, clip=0.5)                     # 只跟到期望速度的一半
    r_bad, _ = saturation_ratio(bad["action"], bad["proprio_state"], FPS,
                                gain=calib["gain"], lag=calib["lag_frames"])
    r_ok, _ = saturation_ratio(clean[0]["action"], clean[0]["proprio_state"], FPS,
                               gain=calib["gain"], lag=calib["lag_frames"])
    assert r_bad > 0.3 and r_ok < 0.15 and r_bad > r_ok + 0.25


def test_motion_quality_scores_saturation_in_velocity_domain():
    clean = [_episode(s) for s in range(12)]
    calib = fit_velocity_gain(clean)
    kw = dict(gripper_dims=(6,), control_mode="velocity", same_space=True,
              stuck_strategy="velocity_dual_scale", velocity_calib=calib,
              angle_dims=(3, 4, 5), angle_mode="delta", euler_triplet=True)
    ok = motion_quality(clean[0]["action"], clean[0]["proprio_state"], FPS, **kw)
    bad_row = _episode(99, clip=0.5)
    bad = motion_quality(bad_row["action"], bad_row["proprio_state"], FPS, **kw)
    assert ok.detail["saturation_method"] == "velocity_domain"
    assert ok.detail["actuator_saturation"] > 0.85
    assert bad.detail["actuator_saturation"] < ok.detail["actuator_saturation"] - 0.3
    assert "saturation_reason" not in ok.detail
    # 没有标定 → 仍诚实不适用,原因点明缺标定(不再笼统说"非绝对目标")
    kw2 = {k: v for k, v in kw.items() if k != "velocity_calib"}
    r = motion_quality(clean[0]["action"], clean[0]["proprio_state"], FPS, **kw2)
    assert r.detail["actuator_saturation"] is None and "缺少速度域标定" in r.detail["saturation_reason"]


def test_semantics_hook_only_for_ee_velocity_datasets():
    rows = [_episode(s) for s in range(6)]
    sem = attach_velocity_calibration(
        DatasetSemantics(action_space="ee", proprio_space="ee", control_mode="velocity"), rows)
    assert sem.extras["velocity_calibration"]["lag_frames"] == 1
    sem2 = attach_velocity_calibration(
        DatasetSemantics(action_space="joint", proprio_space="joint", control_mode="absolute"), rows)
    assert "velocity_calibration" not in sem2.extras
    sem3 = attach_velocity_calibration(
        DatasetSemantics(action_space="ee", proprio_space="ee", control_mode="velocity"),
        [{**r, "proprio_state": None} for r in rows])
    assert "velocity_calibration" not in sem3.extras      # 无读数无从标定


def test_fit_refuses_uncorrelated_data():
    rng = np.random.default_rng(0)
    rows = []
    for s in range(6):
        r = _episode(s)
        r["proprio_state"][:, :3] = rng.normal(0, 0.1, (len(r["action"]), 3))  # 读数与指令无关
        rows.append(r)
    assert fit_velocity_gain(rows) is None


def test_profile_matched_ee_velocity_dataset_still_samples_for_calibration(monkeypatch):
    """profile 命中本来"零数据读";末端速度指令数据集(droid 型)例外——急切 meta 路与懒读路
    都要读样本把标定塞进 semantics_extras,否则执行器饱和永远不适用(2026-09-02 实犯)。"""
    import json
    import os
    import pytest
    pusht = next((p for p in ("/data03/hao/data/pusht", "/mnt/tos/datasets/pusht")
                  if os.path.exists(p)), None)
    if pusht is None:
        pytest.skip("pusht 数据未下载")
    import curation.ingest.dataset_semantics as DS
    import curation.ingest.lerobot_reader as LR
    import curation.ingest.daft_source as DFS
    forced = DatasetSemantics(action_space="ee", proprio_space="ee", control_mode="velocity",
                              gripper_dims=(6,), source="profile", profile_name="fake.yaml")
    monkeypatch.setattr(DS, "resolve_semantics", lambda info, sample=None, dataset_name="": forced)
    monkeypatch.setattr(LR, "read_lerobot_rows",
                        lambda *a, **k: [_episode(s) for s in range(6)])
    metas = LR.read_lerobot_meta(pusht, max_episodes=2)
    vc = json.loads(metas[0]["semantics_extras"]).get("velocity_calibration")
    assert vc and vc["lag_frames"] == 1 and len(vc["gain"]) == 3
    src = DFS.LeRobotDataSource(pusht, max_episodes=2)
    assert src._sem.extras.get("velocity_calibration", {}).get("lag_frames") == 1
