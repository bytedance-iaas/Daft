"""P3.2 验收:add_spike/stuck_joint 注入检出>95%;干净集(合成+aloha 真数据)分布合理。"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.motion_quality import motion_quality
from curation.tests import corrupt

DATA = "/data03/hao/data"
N = 20


def _clean_synthetic(seed: int, t: int = 200, dim: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    tt = np.linspace(0, 2 * np.pi, t)[:, None]
    smooth = np.cumsum(rng.normal(0, 0.003, (t, dim)), axis=0) + 0.3 * np.sin(tt + rng.uniform(0, 6, dim))
    smooth[-30:] = smooth[-30]                       # 末态稳定
    return {
        "episode_id": f"syn{seed}",
        "action": smooth.astype(np.float32),
        "proprio_state": (smooth + rng.normal(0, 0.0005, (t, dim))).astype(np.float32),
        "timestamps": np.arange(t) / 10.0,
        "video": {},
        "fps": 10.0,
    }


def _score(row: dict) -> object:
    # 合成轨迹平滑 → spike 孤立度拐点取低值(默认 30 锚定真机 aloha;阈值按 embodiment 配置是设计本身)
    return motion_quality(row["action"], row["proprio_state"], row["fps"],
                          gripper_dims=(6,), spike_iso_knee=3.0, spike_iso_scale=1.0,
                          control_mode="absolute")


def test_clean_synthetic_scores_high():
    scores = [_score(_clean_synthetic(s)).score for s in range(N)]
    assert min(scores) > 0.6, f"干净合成集最低分 {min(scores):.2f} 过低: {np.round(scores, 2)}"


def test_spike_injection_detected_over_95pct():
    caught = []
    for s in range(N):
        row = _clean_synthetic(s)
        bad, _ = corrupt.add_spike(row, frame=100, magnitude=10.0)
        r = _score(bad)
        localized = r.detail["spike_frames"] and min(
            abs(f - 100) for f in r.detail["spike_frames"]) <= 2
        caught.append(r.detail["spike"] < 0.15 and localized)
    assert np.mean(caught) > 0.95, f"spike 检出率 {np.mean(caught):.0%}"


def test_stuck_injection_detected_over_95pct():
    caught = []
    for s in range(N):
        row = _clean_synthetic(s)
        bad, _ = corrupt.stuck_joint(row, joint=2, from_frame=50)
        r = _score(bad)
        caught.append(r.detail["stuck"] == 0.0
                      and any(d["joint"] == 2 for d in r.detail["stuck_joints"]))
    assert np.mean(caught) > 0.95, f"stuck 检出率 {np.mean(caught):.0%}"


def test_intentional_hold_not_stuck():
    """有意保持(指令+实际同时恒值,如 aloha 单臂持物)≠卡死——真数据教训。"""
    row = _clean_synthetic(2)
    row["action"][80:160, 2] = row["action"][80, 2]          # 指令保持
    row["proprio_state"][80:160, 2] = row["proprio_state"][80, 2]  # 实际也稳(正常伺服)
    assert _score(row).detail["stuck"] == 1.0


def test_saturation_injection_detected():
    row = _clean_synthetic(3)
    bad, _ = corrupt.saturate_actuator(row, joint=1)
    clean_r, bad_r = _score(row), _score(bad)
    assert bad_r.detail["actuator_saturation"] < clean_r.detail["actuator_saturation"] - 0.2


def test_gripper_jitter_detected():
    row = _clean_synthetic(4)
    rng = np.random.default_rng(0)
    row["action"][:, 6] = rng.integers(0, 2, len(row["action"])).astype(np.float32)
    assert _score(row).detail["gripper_jitter"] < 0.3


def test_missing_proprio_is_honest():
    row = _clean_synthetic(5)
    r = motion_quality(row["action"], None, row["fps"], gripper_dims=(6,))
    assert r.detail["actuator_saturation"] is None
    assert r.detail["stuck"] is None                 # 无 achieved 无从判卡死
    assert r.score > 0


@pytest.mark.skipif(not os.path.exists(f"{DATA}/aloha_sim_insertion_human/meta/info.json"),
                    reason="aloha_sim 未下载")
def test_clean_aloha_distribution_sane():
    """干净真数据(aloha_sim 双臂,含真实接触尖峰/单臂长保持)分布合理。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/aloha_sim_insertion_human", max_episodes=20)
    res = [motion_quality(r["action"], r["proprio_state"], r["fps"], gripper_dims=(6, 13),
                          control_mode="absolute")   # aloha=绝对关节角(生产由reader推断传入)
           for r in rows]
    scores = [r.score for r in res]
    assert min(scores) > 0.5 and np.median(scores) > 0.7, f"分布异常: {np.round(scores, 2)}"
    # 有意保持不应触发 stuck(本数据集大量单臂保持)
    assert all(r.detail["stuck"] == 1.0 for r in res), "干净 aloha 出现 stuck 误报"