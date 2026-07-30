"""operator_fluency(操作流畅度)验收:指令+实际双静止 = 空闲段,头/中/尾分开报。

动因(2026-07-11 用户视频复核 so101 ep0):空闲头 3s + 中段操作员停顿,smoothness
对此失明(停顿本身"平滑")。判据与 stuck 互补:指令动+实际不动=stuck(故障);
双不动=idle(行为)。默认只报不罚(fluency 不进总分)。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.core.checks.motion_quality import motion_quality

SO101 = "/data03/hao/data/henry-guo/so101-pick-place"
FPS = 30.0


def _traj_with_idle(head=60, mid=(200, 240), tail=30, T=600, dim=4, seed=0):
    """合成轨迹:平滑正弦运动,指定区间双静止(指令=实际,绝对控制)。"""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 4 * np.pi, T)
    q = np.stack([np.sin(t + i) * (10 + 5 * i) for i in range(dim)], axis=1)
    q[:head] = q[head]                        # 空闲头
    q[mid[0]:mid[1]] = q[mid[0]]              # 中段停顿
    if tail:
        q[-tail:] = q[-tail]                  # 空闲尾
    q += rng.normal(0, 1e-6, q.shape)         # 传感噪声(远低于静止阈)
    return q


def test_fluency_head_mid_tail():
    q = _traj_with_idle(head=60, mid=(200, 240), tail=30)
    res = motion_quality(q, q.copy(), FPS, control_mode="absolute")
    d = res.detail
    assert abs(d["idle_head_s"] - 60 / FPS) < 0.2
    assert abs(d["idle_tail_s"] - 30 / FPS) < 0.2
    assert d["idle_mid_count"] == 1
    assert abs(d["idle_mid_segments"][0]["dur_s"] - 40 / FPS) < 0.2
    assert d["idle_mid_segments"][0]["start_frame"] == pytest.approx(200, abs=3)
    assert 0.75 < d["active_ratio"] < 0.82    # 全程口径:(600-130)/600 ≈ 0.78
    # fluency=执行窗口径:头尾掐掉,只算中段 40 帧 / (599-60-30)≈509 → ≈0.92
    assert 0.89 < d["fluency"] < 0.95
    assert d["fluency"] > d["active_ratio"]   # 头尾空闲不再算操作员犹豫


def test_fluency_fully_active():
    q = _traj_with_idle(head=0, mid=(0, 0), tail=0)
    d = motion_quality(q, q.copy(), FPS, control_mode="absolute").detail
    assert d["active_ratio"] > 0.97
    assert d["idle_mid_count"] == 0 and d["idle_head_s"] == 0.0


def test_fluency_not_in_total_score():
    """先报不罚:总分 = 参与维(不含 fluency/stuck/path/stability)的均值。"""
    q = _traj_with_idle(head=200, mid=(300, 340), tail=0)
    res = motion_quality(q, q.copy(), FPS, control_mode="absolute")
    d = res.detail
    assert d["active_ratio"] < 0.8            # fluency 明显低
    included = [d[k] for k in ("smoothness", "spike", "gripper_jitter",
                               "actuator_saturation") if d.get(k) is not None]
    assert res.score == pytest.approx(float(np.mean(included)), abs=2e-4)  # detail存4位舍入
    # 反证:若 fluency 参与,均值必然不同
    with_f = float(np.mean(included + [d["fluency"]]))
    assert abs(res.score - with_f) > 1e-6


def test_fluency_head_only_is_hygiene_not_skill():
    """ep26 模式:手很稳(中段零停顿)但空闲头很长 → fluency≈1,active_ratio 低。

    两个口径讲两件事:fluency=操作技能;active_ratio=修剪价值(录制卫生)。"""
    q = _traj_with_idle(head=200, mid=(0, 0), tail=0)
    d = motion_quality(q, q.copy(), FPS, control_mode="absolute").detail
    assert d["fluency"] > 0.97                # 执行窗内无犹豫
    assert d["active_ratio"] < 0.72           # 但 1/3 的录制被浪费
    assert abs(d["idle_head_s"] - 200 / FPS) < 0.3


def test_fluency_all_idle_no_exec_window():
    """整条无有效动作:无执行窗可评 → fluency 诚实弃权(None),不给假分。

    长片(k=20)全列豁免 = 死录制,必须仍报全程静止——这正是"从未动过"豁免
    规则的初衷,2026-07-29 短片护栏不许波及(护栏限定 k<20)。"""
    q = np.full((300, 4), 5.0) + np.random.default_rng(0).normal(0, 1e-7, (300, 4))
    d = motion_quality(q, q.copy(), FPS, control_mode="absolute").detail
    assert d["fluency"] is None
    assert "fluency_reason" in d
    assert d["active_ratio"] == 0.0


def test_short_pure_noise_makes_no_static_claim():
    """短片纯噪声:豁免判据本身失真(真运动也够不着 k)→ 零证据不主张静止。

    与上一条互补:同样是"全列豁免",长片可信(死录制)、短片不可信(bridge
    ep000199 的真运动就落在这里)——分岔靠 k<20。"""
    q = np.full((21, 4), 5.0) + np.random.default_rng(0).normal(0, 1e-7, (21, 4))
    d = motion_quality(q, q.copy(), 5.0, control_mode="absolute").detail
    assert d["active_ratio"] == 1.0
    assert "fluency_reason" not in d
    assert not d.get("still_segments")


def test_fluency_delta_control():
    """delta 控制(bridge 型):指令活跃度看 |action| 本身,不是差分。"""
    T = 300
    act = np.full((T, 3), 0.02)               # 持续小增量 = 一直在动
    act[100:160] = 0.0                        # 停顿:增量归零
    state = np.cumsum(act, axis=0)
    d = motion_quality(act, state, FPS, control_mode="delta").detail
    assert d["idle_mid_count"] == 1
    assert abs(d["idle_mid_segments"][0]["dur_s"] - 60 / FPS) < 0.25


def test_fluency_short_gap_ignored():
    """<0.15s 的间隙不算停顿(正常动作换向)。"""
    q = _traj_with_idle(head=0, mid=(200, 203), tail=0)   # 3帧@30fps=0.1s
    d = motion_quality(q, q.copy(), FPS, control_mode="absolute").detail
    assert d["idle_mid_count"] == 0


@pytest.mark.skipif(not os.path.exists(os.path.join(SO101, "meta")), reason="无 so101 数据")
def test_fluency_so101_ep0_real():
    """真数据锚定:ep0 视频复核=空闲头~3s+中段多次停顿(用户确认的地面真相)。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows
    r = read_lerobot_rows(SO101, max_episodes=1)[0]
    d = motion_quality(np.asarray(r["action"], float),
                       np.asarray(r["proprio_state"], float), r["fps"],
                       gripper_dims=r["gripper_dims"],
                       control_mode=r["control_mode"]).detail
    assert 2.0 < d["idle_head_s"] < 4.5       # 视频复核 ~3s
    assert d["idle_mid_count"] >= 3           # 操作员多次停顿
    assert 0.55 < d["active_ratio"] < 0.85
