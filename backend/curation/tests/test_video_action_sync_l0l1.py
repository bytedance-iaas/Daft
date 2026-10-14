"""P3.4 验收:L0 时间戳硬门;L1 shift_video ±5/±10 帧 lag 误差≤2帧、检出>90%;
干净集误报<5%;不可判率如实报告。"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.video_action_sync import (
    camera_reading,
    global_lag,
    timestamp_check,
)
from curation.tests import corrupt

PUSHT = "/data03/hao/data/pusht"
FPS = 10.0


# ---------- L0 时间戳(纯数值,合成) ----------

def test_l0_clean_passes():
    ts = np.arange(200) / FPS
    r = timestamp_check(ts, FPS)
    assert r.passed is True and r.detail["jitter_ratio"] == 0.0


def test_l0_drop_frames_caught():
    row = {"action": np.zeros((200, 2), dtype=np.float32),
           "proprio_state": None, "video": {}, "timestamps": np.arange(200) / FPS, "fps": FPS}
    bad, _ = corrupt.drop_frames(row, start=90, n=10)
    r = timestamp_check(bad["timestamps"], FPS)
    # 理由挂语义词 + 数字:客户读的是"第几帧后断了、断了多久"
    assert r.passed is False
    for word in ("间隔", "秒", "丢帧"):
        assert word in r.detail["reason"], r.detail["reason"]
    assert "89" in r.detail["reason"] or "90" in r.detail["reason"]
    assert r.detail["gap_frames"][0]["frame"] == 89   # 定位到空洞位置


def test_l0_disorder_caught():
    ts = np.arange(100) / FPS
    ts[50] = ts[49] - 0.01
    r = timestamp_check(ts, FPS)
    assert r.passed is False
    for word in ("倒退", "重复", "帧"):
        assert word in r.detail["reason"], r.detail["reason"]


def test_l0_stub_episode_killed():
    """残段硬杀(droid-200 ep000018 实证:8 帧 ≈0.5s,时间戳干干净净,原四道判据
    全放行;同步/VLM 对半秒画面只会弃权,而弃权默认"通过"→ 碎片溜进交付)。
    判废责任必须在结构层,不靠语义判定、也不靠真值集标"失败"兜底。"""
    ts = np.arange(8) / 15.0                    # 复刻 ep000018:8 帧 @15fps ≈ 0.47s
    r = timestamp_check(ts, 15.0)
    assert r.passed is False
    # 措辞人话化(2026-08-11 用户定):数字留着,数学符号去掉——"0.47s < 1.0s"
    # 那种写法客户读不出"这条只有半秒"。锚点挂语义词,不挂修辞。
    for word in ("全长", "0.47", "秒", "不足"):
        assert word in r.detail["reason"], r.detail["reason"]
    assert "<" not in r.detail["reason"]
    assert r.detail["duration_s"] < 1.0


def test_l0_min_duration_configurable_and_boundary():
    """阈值可配;超过阈值的短条照常通过(1.2s@FPS 干净序列不误杀)。"""
    ts = np.arange(int(1.2 * FPS) + 1) / FPS    # 1.2s
    assert timestamp_check(ts, FPS).passed is True
    # 收紧到 2s:同一条就该被杀,且理由写明阈值
    r = timestamp_check(ts, FPS, min_duration_s=2.0)
    assert r.passed is False and "不足 2 秒" in r.detail["reason"]
    # 放宽到 0.3s:连 8 帧碎片都放行(客户显式要短片段时不拦)
    assert timestamp_check(np.arange(8) / 15.0, 15.0, min_duration_s=0.3).passed is True


# ---------- L1 符号锚定(合成,known shift) ----------

def _synthetic_pair(shift_steps: int, n: int = 300, dt: float = 0.1, seed: int = 0):
    """flow[i] = speed[i - shift](shift>0 → 视觉事件晚于动作)。"""
    rng = np.random.default_rng(seed)
    base = np.clip(np.convolve(rng.normal(0, 1, n + 100), np.ones(12) / 12, "same"), 0, None)
    speed = base[50:50 + n]
    flow = base[50 - shift_steps:50 - shift_steps + n] + rng.normal(0, 0.02, n)
    t = np.arange(n) * dt
    return flow, t, speed, t


@pytest.mark.parametrize("shift", [-8, -3, 0, 3, 8])
def test_l1_sign_and_accuracy_synthetic(shift):
    flow, ft, speed, st = _synthetic_pair(shift)
    r = global_lag(flow, ft, speed, st)
    assert r.detail["corr_peak"] > 0.7    # 大 shift 边缘截断会略降相关(0.76@shift8),仍属强相关
    est_steps = r.detail["lag_s"] / 0.1
    assert abs(est_steps - shift) <= 1, f"shift={shift} est={est_steps}"
    assert r.passed is (abs(shift) <= 2)          # lag_tol 0.25s = 2.5 步


def test_l1_curve_recorded_with_reading():
    """The scanned correlation curve travels with the reading (MV-3 borrowed-peak test reads it): same length
    arrays, lags ascending within the scan window, the curve's maximum is the reported peak."""
    r = global_lag(*_synthetic_pair(3))
    curve = r.detail["xc_curve"]
    lags, corr = curve["lags_s"], curve["corr"]
    assert len(lags) == len(corr) and 20 <= len(lags) <= 241
    assert lags == sorted(lags) and lags[0] >= -2.0 - 1e-6 and lags[-1] <= 2.0 + 1e-6
    assert abs(max(corr) - r.detail["corr_peak"]) < 1e-3
    assert abs(lags[corr.index(max(corr))] - r.detail["lag_s"]) < 0.1 + 1e-6
    # the reading of a camera keeps it as an optional key
    from curation.core.checks.video_action_sync import camera_reading
    assert camera_reading(r)["xc_curve"] == curve


def test_l1_windowed_median_agrees_with_the_global_lag_on_a_clean_shift():
    """The second estimator (3 s windows, the trusted windows' median) reads the same shift as the whole
    curve on a synthetic pair, with several trusted windows and a small spread; a window-level reading
    never recurses into another windowed estimate."""
    from curation.core.checks.video_action_sync import windowed_lag

    flow, ft, speed, st = _synthetic_pair(3, n=600)
    r = global_lag(flow, ft, speed, st)
    lw = r.detail["lag_windows"]
    assert lw["n_windows"] >= 5 and lw["n_trusted"] >= 3
    assert abs(lw["lag_s"] - 0.3) <= 0.1 and lw["iqr_s"] <= 0.2
    assert all("lag_windows" not in w for w in lw["windows"])
    direct = windowed_lag(flow, ft, speed, st)
    assert direct["lag_s"] == lw["lag_s"] and direct["n_trusted"] == lw["n_trusted"]
    from curation.core.checks.video_action_sync import camera_reading
    assert camera_reading(r)["lag_windows"]["lag_s"] == lw["lag_s"]


def test_l1_window_length_follows_the_sample_rate_and_the_config():
    """The window is ``max(window_s, window_min_frames)`` samples: 2 s at 30 fps, 64 frames (4.27 s) at 15 fps,
    and the site's config can move both (``global_lag(window=...)``, routed from default.yaml)."""
    from curation.core.checks.video_action_sync import windowed_lag

    flow, ft, speed, st = _synthetic_pair(3, n=600, dt=1 / 30)   # 30 fps, 20 s
    assert abs(windowed_lag(flow, ft, speed, st)["win_s"] - 64 / 30) < 1e-3        # 64 frames beat 2 s at 30 fps
    assert abs(windowed_lag(flow, ft, speed, st, win_s=3.0)["win_s"] - 3.0) < 1e-3
    assert abs(windowed_lag(flow, ft, speed, st, min_frames=90)["win_s"] - 3.0) < 1e-3
    half = windowed_lag(flow[::2], ft[::2], speed[::2], st[::2])  # 15 fps: the frame floor stretches the window
    assert abs(half["win_s"] - 64 / 15) < 1e-3 and abs(half["hop_s"] - 32 / 15) < 1e-3
    slow = windowed_lag(*_synthetic_pair(3, n=600))               # 10 Hz: 6.4 s
    assert abs(slow["win_s"] - 6.4) < 1e-3
    r = global_lag(flow, ft, speed, st, window={"win_s": 4.0})
    assert abs(r.detail["lag_windows"]["win_s"] - 4.0) < 1e-3


def test_l1_undecidable_on_static():
    n = 200
    t = np.arange(n) * 0.1
    r = global_lag(np.zeros(n), t, np.zeros(n), t)
    assert r.passed is None                        # 不可判 ≠ 判坏
    assert r.detail["code"] == "no_motion" and r.detail["trusted"] is False


@pytest.mark.parametrize("shift", [-8, -3, 0, 3, 8])
def test_l1_reading_is_trusted_on_clean_synthetic(shift):
    """峰可信度判据(2026-08-07 新增)不得误伤干净信号:合成注入位移的峰又高又尖,
    必须判 trusted——否则新判据会把所有真错位一起打成"测不准",judgement 层就瞎了。"""
    r = global_lag(*_synthetic_pair(shift))
    assert r.detail["trusted"] is True, r.detail
    assert r.detail["peak_ratio"] >= 1.25 and r.detail["peak_width_s"] <= 1.0
    rd = camera_reading(r)
    assert rd["code"] == ("aligned" if abs(shift) <= 2 else "misaligned")
    assert rd["trusted"] is True


# ---------- L1 真数据(pusht + shift_video 注入) ----------

pusht_needed = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载")

N_EP = 12


@pytest.fixture(scope="module")
def pusht_rows():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    # 从 ep1 起取(ep0 的 from_ts=0,负向平移会出界)
    return read_lerobot_rows(PUSHT, max_episodes=N_EP + 1)[1:]


