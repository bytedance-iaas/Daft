"""同步测量按肢体分组求一致 + 两把尺各自的「稳」(2026-10-14,G1 ep7/42/108 的教训)。

钉死的事:
- 布局里的肢体组由列名的 left / right 决定;代理按组各出一根速度曲线,并集与老的 sync_speed_columns 相同;
- 只有一组时 grouped_lag 与 global_lag 读数一致;两组一致 → 可信、取中位数;两组可信却矛盾 → 测不准(limb_conflict);
- 整段的峰靠一小段数据撑着(留一法不稳)且分窗不稳 → 测不准(whole_unstable);
- 整段不稳而分窗稳 → 分窗接管(basis=windowed_median),读数是逐窗中位数;
- 干净的整段错位(大于半窗)不受影响:留一法稳,整段照常可信。
"""
from __future__ import annotations

import numpy as np

from curation.core.checks.video_action_sync import camera_reading, global_lag, grouped_lag, joint_speed
from curation.ingest.dataset_semantics import detect_ee_layout, layout_for_extras
from curation.pipeline.episode_checks import sync_speed_columns, sync_speed_groups, sync_speed_proxies

G1 = ["kLeftShoulderPitch", "kLeftShoulderRoll", "kLeftShoulderYaw", "kLeftElbow", "kLeftWristRoll", "kLeftWristPitch",
      "kLeftWristYaw", "kLeftHandPinky", "kLeftHandRing", "kLeftHandMiddle", "kLeftHandIndex", "kLeftHandThumbBend",
      "kLeftHandThumbRotation", "kRightShoulderPitch", "kRightShoulderRoll", "kRightShoulderYaw", "kRightElbow",
      "kRightWristRoll", "kRightWristPitch", "kRightWristYaw", "kRightHandPinky", "kRightHandRing", "kRightHandMiddle",
      "kRightHandIndex", "kRightHandThumbBend", "kRightHandThumbRotation"]
SO101 = ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos", "wrist_flex.pos", "wrist_roll.pos", "gripper.pos"]


def test_layout_carries_limbs_by_side_and_groups_follow_it():
    lay = layout_for_extras(detect_ee_layout(G1, 26))
    assert lay["limbs"] == [["left", list(range(0, 7))], ["right", list(range(13, 20))]]
    g = sync_speed_groups(26, "joint", lay, ())
    assert g == {"left": list(range(0, 7)), "right": list(range(13, 20))}
    assert sync_speed_columns(26, "joint", lay, ()) == list(range(0, 7)) + list(range(13, 20))
    # 单臂:没有侧别词 → 一组,夹爪照旧剔除
    lay1 = layout_for_extras(detect_ee_layout(SO101, 6))
    assert lay1["limbs"] == [["", [0, 1, 2, 3, 4]]]
    assert sync_speed_groups(6, "joint", lay1, ()) == {"arm": [0, 1, 2, 3, 4]}
    assert sync_speed_groups(6, "joint", None, (5,)) == {"arm": [0, 1, 2, 3, 4]}
    # 末端:两只手的平移列按侧别分组;没布局的 7 列位姿只取前三列
    names = [f"{s}_{a}" for s in ("left", "right") for a in ("pos_x", "pos_y", "pos_z", "roll", "pitch", "yaw", "gripper")]
    lay2 = layout_for_extras(detect_ee_layout(names, 14))
    assert sync_speed_groups(14, "ee", lay2, ()) == {"left": [0, 1, 2], "right": [7, 8, 9]}
    assert sync_speed_groups(7, "ee", None, ()) == {"ee": [0, 1, 2]}
    prox = sync_speed_proxies(np.random.default_rng(0).normal(size=(50, 26)), 30.0, "joint", lay, ())
    assert set(prox) == {"left", "right"} and all(len(v) == 49 for v in prox.values())


def _pair(shift_steps: int, n: int = 600, dt: float = 1 / 30, seed: int = 0):
    rng = np.random.default_rng(seed)
    base = np.clip(np.convolve(rng.normal(0, 1, n + 100), np.ones(12) / 12, "same"), 0, None)
    speed = base[50:50 + n]
    flow = base[50 - shift_steps:50 - shift_steps + n] + rng.normal(0, 0.02, n)
    t = np.arange(n) * dt
    return flow, t, speed, t


def test_single_group_equals_global_lag_and_a_clean_big_shift_stays_trusted():
    flow, ft, speed, st = _pair(45)            # 1.5 s:大于半窗,只有整段读得出
    one = global_lag(flow, ft, speed, st)
    grp = grouped_lag(flow, ft, {"arm": speed}, st)
    assert grp.detail["lag_s"] == one.detail["lag_s"] and grp.detail["trusted"] and grp.detail["code"] == "lag_exceeds_tol"
    assert abs(grp.detail["lag_s"] - 1.5) <= 0.07
    assert grp.detail["whole_stable"] is True and grp.detail["jackknife"]["n_chunks"] >= 3
    assert grp.detail.get("basis") is None and grp.detail["group"] == "arm"


def test_two_limbs_that_agree_are_trusted_and_two_that_conflict_are_not():
    flow, ft, left, st = _pair(3)              # 0.1 s,对齐
    right = np.interp(st, st + 0.03, left)      # 同一条流再抖一帧
    r = grouped_lag(flow, ft, {"left": left, "right": right}, st)
    assert r.detail["trusted"] and r.detail["code"] == "aligned" and r.detail["group_agreement"]["n_trusted"] == 2
    assert set(r.detail["groups"]) == {"left", "right"}
    # 右臂列换成把同一条流错开 1.5 s 的信号:两组各自可信却矛盾 → 测不准,不进可信集
    right_shifted = np.interp(st, st + 1.5, left)     # 右臂列 = 同一条流提前 1.5 s(画面相对它晚 1.6 s)
    r2 = grouped_lag(flow, ft, {"left": left, "right": right_shifted}, st)
    assert r2.detail["trusted"] is False and r2.detail["instability"] == "limb_conflict"
    rd = camera_reading(r2)
    assert rd["code"] == "ambiguous_peak" and rd["diagnosis"]["cause"] == "unstable_estimate"
    assert "右臂" in rd["diagnosis"]["text"] and "左臂" in rd["diagnosis"]["text"]


def _burst(n, at, width=12, amp=1.0):
    x = np.zeros(n)
    x[at:at + width] = amp
    return x


def test_a_peak_carried_by_one_burst_is_unstable_and_untrusted():
    """动作只有一次爆发、画面的爆发在 1.5 s 之后:整段互相关给出尖峰,留一法抹掉那一段峰就散;
    其它时段没有运动,分窗也不稳 → whole_unstable。"""
    n, dt = 600, 1 / 30
    t = np.arange(n) * dt
    rng = np.random.default_rng(1)
    speed = _burst(n, 200) + 0.02 * np.abs(rng.normal(size=n))
    flow = _burst(n, 245) + 0.02 * np.abs(rng.normal(size=n))
    r = global_lag(flow, t, speed, t)
    assert r.detail["trusted"] is False and r.detail["instability"] == "whole_unstable"
    assert r.detail["whole_stable"] is False and r.detail["jackknife"]["max_dev_s"] > 0.25
    assert camera_reading(r)["diagnosis"]["cause"] == "unstable_estimate"


def test_steady_windows_take_over_an_unstable_whole_peak():
    """全程许多小爆发画面与动作对齐(逐窗都读 0),只有一处巨大的爆发在画面里晚了 1.5 s:
    整段峰被它拖到 1.5 s,留一法抹掉那一段峰就回到 0 → 分窗接管,读数 0、可信、basis=windowed_median。"""
    n, dt = 1500, 1 / 30
    t = np.arange(n) * dt
    rng = np.random.default_rng(2)
    speed = 0.02 * np.abs(rng.normal(size=n))
    flow = 0.02 * np.abs(rng.normal(size=n))
    for at in range(20, n - 60, 50):
        speed = speed + _burst(n, at, width=10, amp=0.3 + 0.2 * rng.random())
    flow = flow + speed
    speed = speed + _burst(n, 700, width=15, amp=6.0)
    flow = flow + _burst(n, 745, width=15, amp=6.0)
    r = global_lag(flow, t, speed, t)
    assert r.detail["trusted"] is True and r.detail["basis"] == "windowed_median"
    assert abs(r.detail["lag_s"]) <= 0.1 and r.detail["code"] == "aligned"
    assert abs(r.detail["whole_lag_s"] - 1.5) <= 0.1
    rd = camera_reading(r)
    assert rd["basis"] == "windowed_median" and rd["diagnosis"]["cause"] == "aligned"
