"""P3.0 验收:每个注入器有单测;注入后数据仍过 M1 校验(格式合法,内容有病)。"""
from __future__ import annotations

import numpy as np
import pytest

from curation.ingest.validate import validate_episode_row
from curation.tests import corrupt


@pytest.fixture()
def clean_row():
    t = 100
    rng = np.random.default_rng(7)
    walk = np.cumsum(rng.normal(0, 0.02, (t, 7)), axis=0).astype(np.float32)
    return {
        "episode_id": "ep000000",
        "embodiment_id": "franka",
        "instruction": "pick the cube",
        "action": walk,
        "proprio_state": walk + rng.normal(0, 0.001, (t, 7)).astype(np.float32),
        "video": {"cam": {"path": __file__, "from_ts": 0.0, "to_ts": t / 10.0}},  # 存在的文件即可
        "timestamps": np.arange(t) / 10.0,
        "fps": 10.0,
    }


def test_drop_frames(clean_row):
    out, inj = corrupt.drop_frames(clean_row, start=40, n=10)
    assert len(out["action"]) == 90
    validate_episode_row(out)  # 格式仍合法(时间戳仍严格递增,只是有空洞)
    gaps = np.diff(out["timestamps"])
    assert gaps.max() > 2.0 * np.median(gaps), "应产生时间空洞"
    assert inj.should_be_caught_by == "video_action_sync"
    # 原行不被污染
    assert len(clean_row["action"]) == 100


def test_shift_video(clean_row):
    out, inj = corrupt.shift_video(clean_row, shift_frames=5)
    assert out["video"]["cam"]["from_ts"] == pytest.approx(0.5)
    assert inj.params["shift_s"] == pytest.approx(0.5)
    assert clean_row["video"]["cam"]["from_ts"] == 0.0


def test_exceed_limits(clean_row):
    out, inj = corrupt.exceed_limits(clean_row, joint=3, frame=50, limit_value=2.8973)
    assert out["action"][50, 3] == pytest.approx(2.8973 * 1.5)
    validate_episode_row(out)  # 超限但格式合法(非 NaN)
    assert inj.params == {"joint": 3, "frame": 50, "value": pytest.approx(2.8973 * 1.5)}


def test_add_spike(clean_row):
    out, _ = corrupt.add_spike(clean_row, frame=30, magnitude=10.0)
    validate_episode_row(out)
    accel = np.abs(np.diff(out["action"], n=2, axis=0))
    assert np.unravel_index(np.argmax(accel), accel.shape)[0] in (28, 29, 30), "尖刺应在注入帧附近"


def test_stuck_joint(clean_row):
    out, _ = corrupt.stuck_joint(clean_row, joint=2, from_frame=20)
    validate_episode_row(out)
    # dead actuator 语义:指令继续动,实际(proprio)冻结
    assert np.all(out["proprio_state"][20:, 2] == out["proprio_state"][20, 2])
    assert not np.all(out["action"][20:, 2] == out["action"][20, 2]), "指令应保持在动"
    assert not np.all(out["proprio_state"][20:, 3] == out["proprio_state"][20, 3]), "其它关节不受影响"


def test_saturate_actuator(clean_row):
    out, _ = corrupt.saturate_actuator(clean_row, joint=1)
    validate_episode_row(out)
    gap = np.abs(out["action"][:, 1] - out["proprio_state"][:, 1])
    clean_gap = np.abs(clean_row["action"][:, 1] - clean_row["proprio_state"][:, 1])
    assert gap.mean() > 10 * clean_gap.mean(), "command-achieved 偏差应显著放大"


def test_truncate(clean_row):
    out, _ = corrupt.truncate(clean_row, keep_fraction=0.4)
    assert len(out["action"]) == 40
    validate_episode_row(out)
    assert out["video"]["cam"]["to_ts"] == pytest.approx(4.0)


def test_duplicate(clean_row):
    rows, inj = corrupt.duplicate([clean_row], 0)
    assert len(rows) == 2
    assert rows[1]["episode_id"] == "ep000000_dup"
    np.testing.assert_array_equal(rows[0]["action"], rows[1]["action"])


def _frames(n=6):
    rng = np.random.default_rng(3)
    return [rng.integers(0, 255, (64, 64, 3), dtype=np.uint8) for _ in range(n)]


def test_blur_frames():
    frames = _frames()
    out, _ = corrupt.blur_frames(frames, kernel=21)
    import cv2

    sharp = cv2.Laplacian(cv2.cvtColor(frames[0], cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()
    blurred = cv2.Laplacian(cv2.cvtColor(out[0], cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()
    assert blurred < sharp * 0.2, "拉普拉斯方差应大幅下降"


def test_black_frames():
    out, _ = corrupt.black_frames(_frames(), start=2, n=2)
    assert out[2].max() == 0 and out[3].max() == 0
    assert out[0].max() > 0 and out[4].max() > 0


def test_overexpose_frames():
    frames = _frames()
    out, _ = corrupt.overexpose_frames(frames, delta=150)
    assert out[0].mean() > frames[0].mean() + 100