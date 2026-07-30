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


def _still_total_s(r: object) -> float:
    return sum(s["end_s"] - s["start_s"] for s in (r.detail.get("still_segments") or []))


def _steady_motion(t: int, dim: int = 7, span: float = 0.1,
                   noise: float = 1e-5, seed: int = 0) -> np.ndarray:
    """近匀速运动(轻微加减速)+ 小噪声。关键性质:量程/步长 比值 ≈ 步数 T-1
    ——短片下这个比值天然只有十几,正是 bridge 全列被豁免的成因。"""
    rng = np.random.default_rng(seed)
    phase = np.linspace(0.5, 0.7, dim)[None, :]              # 每列 17~18 倍比值
    base = span * np.sin(np.pi * phase * np.arange(t)[:, None] / (t - 1))
    return (base + rng.normal(0, noise, (t, dim))).astype(np.float32)


def test_short_episode_real_motion_not_idle():
    """短片真运动不得被判静止(2026-07-29 bridge ep000199:21 帧 fps=5,
    量程/步长比值只有 ~17,固定豁免倍率 k=20 把全部列豁免→全程假 idle)。"""
    traj = _steady_motion(21)
    r = motion_quality(traj, traj.copy(), 5.0, gripper_dims=(6,), control_mode="absolute")
    assert r.detail["active_ratio"] > 0.8, f"短片真运动 active_ratio={r.detail['active_ratio']}"
    assert _still_total_s(r) < 0.5, f"短片真运动被判静止: {r.detail.get('still_segments')}"


def test_all_columns_exempt_makes_no_claim():
    """全列豁免=零证据,不得反过来主张"全程静止"(np.all(全True) 的第二层缺陷)。"""
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 1e-4, (21, 7)).astype(np.float32)
    r = motion_quality(noise, noise.copy(), 5.0, gripper_dims=(6,), control_mode="absolute")
    assert not r.detail.get("still_segments"), \
        f"全豁免仍报出静止段: {r.detail['still_segments']}"


def test_long_episode_partial_stop_still_reported():
    """长片里只要有一列有否决资格,停住的尾段仍要报出(护栏不能把真 still 也吃掉)。"""
    rng = np.random.default_rng(3)
    traj = rng.normal(0, 1e-4, (300, 7)).astype(np.float32)   # 其余列纯噪声(必豁免)
    traj[:, 0] += np.concatenate([np.arange(200) * 0.01,
                                  np.full(100, 199 * 0.01)]).astype(np.float32)
    r = motion_quality(traj, traj.copy(), 10.0, gripper_dims=(6,), control_mode="absolute")
    segs = r.detail.get("still_segments") or []
    assert any(s["start_s"] <= 21.0 and s["end_s"] >= 29.0 for s in segs), \
        f"长片尾段停住未报出: {segs}"


def test_long_episode_continuous_motion_no_regression():
    """长片连续运动 k 仍为 20,行为与修复前一致(不报静止)。"""
    traj = _steady_motion(300, seed=1)
    r = motion_quality(traj, traj.copy(), 10.0, gripper_dims=(6,), control_mode="absolute")
    assert _still_total_s(r) < 0.5, f"长片连续运动被判静止: {r.detail.get('still_segments')}"
    assert r.detail["active_ratio"] > 0.95


def _noise_cols(t: int, dim: int, seed: int, sigma: float = 1e-4) -> np.ndarray:
    """iid 传感噪声列:量程/步长 ≈ 6~7(幅度路径必豁免),增量 lag-1 自相关 ≈ -0.5
    (白噪声位置序列差分后天生反相关)→ 连贯路径也必豁免。"""
    return np.random.default_rng(seed).normal(0, sigma, (t, dim))


def test_long_episode_small_envelope_oscillation_gets_vote():
    """小包络持续往复(擦拭/搅拌类)必须拿到投票权——连贯路径的存在理由。

    ⚠️ 本用例在旧码(单一幅度判据)上**必挂**:周期 20 帧的往复量程/步长 ≈ 9,
    够不着 k=20 → 该列被当噪声豁免;长片全列豁免 = "死录制"语义 → 报全程静止,
    而这条其实一直在动(与 bridge ep000199 同构的冤案,只是成因换成了小包络)。
    新码走连贯路径:ρ=cos(2π/20)=0.95 ≥ 0.5 且量程 ≥ 4×步长 → 有否决资格。"""
    traj = _noise_cols(300, 7, seed=11)
    traj[:, 0] += 0.05 * np.sin(2 * np.pi * np.arange(300) / 20.0)   # 量程/步长≈9
    traj = traj.astype(np.float32)
    r = motion_quality(traj, traj.copy(), 15.0, gripper_dims=(6,), control_mode="absolute")
    assert r.detail["active_ratio"] > 0.9, f"往复运动被判静止 active_ratio={r.detail['active_ratio']}"
    assert _still_total_s(r) < 0.5, f"往复运动报出静止段: {r.detail.get('still_segments')}"


def test_long_episode_pure_noise_dead_recording_unchanged():
    """长片全列 iid 噪声 = 死录制,仍须报全程静止(连贯路径不得放水)。"""
    traj = _noise_cols(300, 7, seed=12).astype(np.float32)
    d = motion_quality(traj, traj.copy(), 15.0, gripper_dims=(6,),
                       control_mode="absolute").detail
    assert d["active_ratio"] == 0.0, f"死录制未报全程静止: active_ratio={d['active_ratio']}"


def test_correlated_noise_small_envelope_still_exempt():
    """结构性噪声不得靠"相关"混进投票权——两道闸各挡一种:

    ①AR(1) 漂移(φ=0.7):位置序列自相关高,但**增量** lag-1 ρ≈-0.21(位置的
      平滑漂移差分后仍反相关)→ 被 ρ 阈挡住;
    ②小包络快抖(周期 7 帧,ρ≈0.62 够格)量程只有步长的 3.6 倍 → 被幅度地板
      4.0 挡住(死区抖动/舵机嗡鸣不算"在动")。
    两种情形都应回到纯噪声死录制的行为(全列豁免 → 全程静止)。"""
    rng = np.random.default_rng(13)
    ar = np.zeros(300)
    e = rng.normal(0, 1e-4, 300)
    for i in range(1, 300):
        ar[i] = 0.7 * ar[i - 1] + e[i]
    t1 = _noise_cols(300, 7, seed=14)
    t1[:, 0] = ar
    d1 = motion_quality(t1.astype(np.float32), t1.astype(np.float32), 15.0,
                        gripper_dims=(6,), control_mode="absolute").detail
    assert d1["active_ratio"] == 0.0, f"AR(1) 噪声骗到投票权: {d1['active_ratio']}"
    t2 = _noise_cols(300, 7, seed=15)
    t2[:, 0] += 0.05 * np.sin(2 * np.pi * np.arange(300) / 7.0)     # 量程/步长≈3.6
    d2 = motion_quality(t2.astype(np.float32), t2.astype(np.float32), 15.0,
                        gripper_dims=(6,), control_mode="absolute").detail
    assert d2["active_ratio"] == 0.0, f"噪声带内的相干抖动骗到投票权: {d2['active_ratio']}"


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