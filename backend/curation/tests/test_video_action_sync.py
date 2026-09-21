"""P1 spike2 产物单测:光流能量 + 关节速度(合成数据,不依赖真实数据集)。"""
from __future__ import annotations

import numpy as np
import pytest

from curation.core.checks.video_action_sync import joint_speed, optical_flow_energy


def _square_frames(n: int, step: int, size: int = 96) -> list[np.ndarray]:
    """一个白方块每帧右移 step 像素的合成序列。"""
    frames = []
    for i in range(n):
        f = np.zeros((size, size, 3), dtype=np.uint8)
        x = 10 + i * step
        f[40:56, x:x + 16] = 255
        frames.append(f)
    return frames


def test_static_frames_near_zero_energy():
    energy = optical_flow_energy(_square_frames(6, step=0))
    assert energy.shape == (5,)
    assert energy.max() < 0.05, f"静止画面能量应≈0,got {energy.max()}"


def test_motion_energy_scales_with_speed():
    slow = optical_flow_energy(_square_frames(6, step=2)).mean()
    fast = optical_flow_energy(_square_frames(6, step=6)).mean()
    static = optical_flow_energy(_square_frames(6, step=0)).mean()
    assert static < slow < fast, f"能量应随运动速度递增: {static:.4f} / {slow:.4f} / {fast:.4f}"


def test_short_input_returns_empty():
    assert optical_flow_energy([]).shape == (0,)
    assert optical_flow_energy(_square_frames(1, step=0)).shape == (0,)


def test_grayscale_frames_accepted():
    gray = [f[:, :, 0] for f in _square_frames(4, step=3)]
    assert optical_flow_energy(gray).shape == (3,)


def test_joint_speed_known_values():
    # 每步位移 (3,4) → 模长 5;fps=10 → 速度 50
    action = np.array([[0, 0], [3, 4], [6, 8]], dtype=np.float64)
    speed = joint_speed(action, fps=10.0)
    assert speed.shape == (2,)
    np.testing.assert_allclose(speed, [50.0, 50.0])


def test_joint_speed_short_input():
    assert joint_speed(np.zeros((1, 7)), fps=10.0).shape == (0,)


def test_correlation_pipeline_on_synthetic_pair():
    """闭环:合成'走走停停'运动,光流能量与速度曲线应强相关(通路的最小数学验证)。"""
    steps = [0] * 5 + [4] * 8 + [0] * 5 + [7] * 8 + [0] * 4
    frames, x = [], 5
    for s in steps:
        f = np.zeros((96, 96, 3), dtype=np.uint8)
        f[40:56, x:x + 16] = 255
        frames.append(f)
        x += s
    energy = optical_flow_energy(frames)
    speed = joint_speed(np.cumsum([[s, 0] for s in steps], axis=0).astype(float), fps=1.0)
    r = np.corrcoef(energy, speed[: len(energy)])[0, 1]
    # Farneback 在急起急停的转折帧低估大位移(合成方块 0→7px 突变),0.79 左右是正常水平;
    # 真实数据的锚点见 spike2:DROID 真机 r=0.84。阈值取 0.7 检验"强相关"而不过拟合光流特性。
    assert r > 0.7, f"合成数据光流×速度相关性应很强,got r={r:.3f}"