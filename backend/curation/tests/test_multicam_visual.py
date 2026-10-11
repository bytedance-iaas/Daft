"""多相机视觉质检:活路判据(占位黑帧路豁免)。"""
from __future__ import annotations

import numpy as np


def test_live_channel_criterion_uses_sampled_frames_not_just_the_first():
    """判据从"首帧"改成"采样帧统计":开头一帧黑(快门/曝光瞬变)不再把活路误判成占位。"""
    from curation.core.checks.visual_quality import is_live_channel

    rng = np.random.default_rng(3)
    bright = [rng.integers(60, 200, (32, 32, 3), dtype=np.uint8) for _ in range(9)]
    assert is_live_channel([np.zeros((32, 32, 3), np.uint8)] + bright) is True
    assert is_live_channel([np.zeros((32, 32, 3), np.uint8)] * 10) is False
    assert is_live_channel([]) is False                  # 解不出帧 = 不能声称在拍
