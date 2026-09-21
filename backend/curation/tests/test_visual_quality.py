"""P3.1 验收:blur/black 注入集 100% 检出;干净 pusht 误报 <5%(20 条真数据)。"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.visual_quality import visual_quality
from curation.tests import corrupt

PUSHT = "/data03/hao/data/pusht"
THRESHOLD = 0.5   # 验收用工作点(正式默认值 P5.1 校准)

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载",
)

N_EPISODES = 20


@pytest.fixture(scope="module")
def clean_frame_sets():
    """20 条干净 pusht episode 的抽样帧(2fps)。"""
    from curation.adapters.frames import extract_frames
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(PUSHT, max_episodes=N_EPISODES)
    sets = []
    for r in rows:
        frames, _ = extract_frames(r["video"]["observation.image"],
                                   sample_interval_seconds=0.5)
        sets.append(frames)
    return sets


def test_clean_pusht_low_false_positive(clean_frame_sets):
    scores = [visual_quality(f).score for f in clean_frame_sets]
    fp = np.mean([s < THRESHOLD for s in scores])
    assert fp < 0.05, f"干净集误报率 {fp:.0%} ≥5%;分数={np.round(scores, 2)}"


def test_blur_injection_100pct_detected(clean_frame_sets):
    detected = []
    for frames in clean_frame_sets:
        bad, _ = corrupt.blur_frames(frames, kernel=21)
        r = visual_quality(bad)
        detected.append(r.score < THRESHOLD)
        assert r.detail["sharpness"] < 0.5, "糊应体现在 sharpness 子分"
    assert all(detected), f"blur 检出 {np.mean(detected):.0%} < 100%"


def test_black_injection_100pct_detected(clean_frame_sets):
    detected = []
    for frames in clean_frame_sets:
        bad, _ = corrupt.black_frames(frames)   # 全黑
        r = visual_quality(bad)
        detected.append(r.score < THRESHOLD)
    assert all(detected), f"black 检出 {np.mean(detected):.0%} < 100%"


def test_partial_black_lowers_integrity(clean_frame_sets):
    frames = clean_frame_sets[0]
    bad, _ = corrupt.black_frames(frames, start=0, n=len(frames) // 2)
    r = visual_quality(bad)
    assert r.detail["dead_ratio"] == pytest.approx(0.5, abs=0.1)
    assert r.detail["integrity"] <= 0.6


def test_overexposure_detected(clean_frame_sets):
    frames = clean_frame_sets[0]
    bad, _ = corrupt.overexpose_frames(frames, delta=200)
    r = visual_quality(bad)
    assert r.detail["exposure"] < visual_quality(frames).detail["exposure"] + 1e-9 or \
        r.score < THRESHOLD, "过曝应显著拉低曝光子分或总分"


def test_empty_frames_is_zero():
    r = visual_quality([])
    assert r.score == 0.0