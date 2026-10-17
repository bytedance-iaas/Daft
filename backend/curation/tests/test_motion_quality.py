"""P3.2 验收:add_spike/stuck_joint 注入检出>95%;干净集(合成+aloha 真数据)分布合理。"""
from __future__ import annotations

import json
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

# ---- 2026-10-14 RH20T 教训:一列开关、一块四元数,别拖垮其它列 ------------------------------

def _rh20t_like(t: int = 300, flips: bool = True):
    """xyz 平滑 + 单位四元数(每 6 s 换一次符号)+ 夹爪 0/95 两档,8 列无名,10 Hz。"""
    from scipy.spatial.transform import Rotation
    tt = np.linspace(0, 2 * np.pi, t)
    xyz = np.stack([0.5 + 0.1 * np.sin(tt), 0.1 * np.cos(tt), 0.17 + 0.05 * np.sin(2 * tt)], 1)
    rot = Rotation.from_euler("xyz", np.stack([0.1 * np.sin(tt), 0.2 * np.cos(tt), np.pi + 0.3 * np.sin(tt)], 1))
    q = rot.as_quat()
    if flips:
        q[(np.arange(t) // 60) % 2 == 1] *= -1
    grip = np.where(np.arange(t) < 100, 95.0, np.where(np.arange(t) < 200, 0.0, 95.0))
    return np.column_stack([xyz, q, grip]).astype(np.float32)


def test_switch_column_stays_out_of_smoothness_and_spike():
    a = _rh20t_like(flips=False)
    r = motion_quality(a, None, 10.0, control_mode="absolute")      # nothing declared, no gripper known
    assert r.detail["discrete_cols"] == [7]
    assert "7" not in r.detail["smoothness_cols"]
    assert r.detail["smoothness"] > 0.9 and r.detail["spike"] > 0.9


def test_quaternion_block_sign_flips_are_not_motion():
    a = _rh20t_like(flips=True)
    bare = motion_quality(a, None, 10.0, control_mode="absolute")
    lay = motion_quality(a, None, 10.0, control_mode="absolute", rotation_blocks=[(3, 4, "quat")],
                         angle_dims=(3, 4, 5, 6), angle_mode="absolute", gripper_dims=(7,),
                         translation_dims=(0, 1, 2))
    assert lay.detail["smoothness"] > 0.95 and lay.detail["spike"] > 0.95
    assert lay.detail["smoothness"] - bare.detail["smoothness"] > 0.2     # the flips still cost without a layout
    assert set(lay.detail["smoothness_cols"]) == {"0", "1", "2", "3"}   # xyz + the rotation odometer


def test_one_jerky_column_is_named_and_only_costs_its_share():
    row = _clean_synthetic(11)
    a = row["action"].astype(np.float64)
    rng = np.random.default_rng(1)
    a[:, 2] += rng.normal(0, 0.01, len(a))                            # one noisy joint
    r = motion_quality(a, None, row["fps"], gripper_dims=(6,), control_mode="absolute")
    cols = r.detail["smoothness_cols"]
    assert r.detail["smoothness_min_col"] == 2 and cols["2"] < min(v for k, v in cols.items() if k != "2")
    assert r.detail["smoothness"] > 0.5                               # six clean columns keep the mean up


def test_unknown_semantics_abstain_instead_of_scoring():
    from curation.pipeline.episode_checks import make_motion_check

    class _Reg:
        def get(self, _id):
            raise KeyError(_id)
    check = make_motion_check({"checks": {"motion_quality": {"params": {}}}}, _Reg())
    out = check(_rh20t_like(), None, 10.0, "unknown", "unknown", "unknown", "flexiv", "auto", "{}")
    d = json.loads(out["detail"]) if isinstance(out.get("detail"), str) else out["detail"]
    assert out["passed"] is None and out.get("score") is None
    assert d["semantics_undetermined"] is True and "smoothness" not in d
    assert "不算" in d["smoothness_reason"] and d["reason"] == d["spike_reason"]


# ---- F7b (2026-10-14):走走停停不是抖动 -------------------------------------------------------

def _same_motion_at(fps: float, jitter: float = 0.0, seed: int = 0, seconds: float = 20.0):
    """一段 20 s 的平滑动作(几条正弦叠加)按 fps 采样;jitter 是每帧叠加的白噪声幅度(米)。"""
    rng = np.random.default_rng(seed)
    t = np.arange(0.0, seconds, 1.0 / fps)
    base = np.stack([0.2 * np.sin(0.6 * t + k) + 0.05 * np.sin(1.7 * t + 2 * k) for k in range(6)], 1)
    return (base + rng.normal(0, jitter, base.shape)).astype(np.float64)


def _with_pauses(a: np.ndarray, every: int = 40, hold: int = 30) -> np.ndarray:
    """每 every 帧插入 hold 帧的停顿(值不变):同一段动作,只是走走停停。"""
    chunks = []
    for i in range(0, len(a), every):
        chunks.append(a[i:i + every])
        chunks.append(np.repeat(a[min(i + every, len(a)) - 1:min(i + every, len(a))], hold, axis=0))
    return np.concatenate(chunks)


def test_pauses_do_not_make_a_smooth_motion_jagged():
    for fps in (10.0, 30.0):
        a = _same_motion_at(fps)
        cont = motion_quality(a, None, fps, control_mode="absolute").detail["smoothness"]
        paused = motion_quality(_with_pauses(a), None, fps, control_mode="absolute").detail["smoothness"]
        assert abs(cont - paused) < 0.08, (fps, cont, paused)


def test_jitter_still_separates_from_clean_at_every_rate():
    for fps in (10.0, 30.0, 50.0):
        clean = motion_quality(_same_motion_at(fps), None, fps, control_mode="absolute").detail["smoothness"]
        noisy = motion_quality(_same_motion_at(fps, jitter=0.01), None, fps, control_mode="absolute").detail["smoothness"]
        assert clean - noisy > 0.25, (fps, clean, noisy)


# ---- F8 (2026-10-14):列名分块优先于预检的整列假设,只在预检没把握时启用 ------------------------

def test_names_layout_lets_motion_quality_run_on_unknown_semantics_without_velocity_columns():
    from curation.pipeline.episode_checks import make_motion_check
    from curation.tests.test_ee_layout_rot6d import HIW_NAMES, _hiw_like
    from curation.ingest.dataset_semantics import detect_ee_layout, layout_for_extras

    class _Reg:
        def get(self, _id):
            raise KeyError(_id)
    check = make_motion_check({"checks": {"motion_quality": {"params": {}}}}, _Reg())
    a = _hiw_like(); s = np.cumsum(np.random.default_rng(1).normal(size=(len(a), 29)) * 0.01, axis=0)
    extras = json.dumps({"layout": dict(layout_for_extras(detect_ee_layout(HIW_NAMES, 23)), names_only=True)})
    out = check(a, s, 30.0, "unknown", "unknown", "unknown", "unitree_g1", "auto", extras)
    d = json.loads(out["detail"]) if isinstance(out.get("detail"), str) else out["detail"]
    assert d.get("semantics_undetermined") is None and d["smoothness"] > 0.9
    assert d["velocity_cols"] == [0, 1, 2] and not ({"0", "1", "2"} & set(d["smoothness_cols"]))
    assert d["gripper_jitter"] is not None and not ({"19", "20", "21", "22"} & set(d["smoothness_cols"]))  # hand controls are grippers
    assert d["actuator_saturation"] is None                      # 23 commands vs 29 joints: no same-space comparison
    # the same episode without a names layout still abstains (F1 unchanged)
    out2 = check(a, s, 30.0, "unknown", "unknown", "unknown", "unitree_g1", "auto", "{}")
    d2 = json.loads(out2["detail"]) if isinstance(out2.get("detail"), str) else out2["detail"]
    assert d2["semantics_undetermined"] is True


# ---- 2026-10-15 jaggedness (ACT-1): the injector's per-frame sawtooth, read on its own ---------------------------

def _smooth_traj(t: int, dim: int, fps: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    tt = np.arange(t)[:, None] / fps
    return 0.6 * np.sin(tt * (0.5 + 0.3 * np.arange(dim))) + 0.02 * np.cumsum(rng.normal(0, 0.002, (t, dim)), 0)


def _sawtooth(x: np.ndarray, fps: float, col: int = 0, frac: float = 0.05, start_s: float = 3.0, length_s: float = 4.0):
    y = x.copy(); a = int(start_s * fps); z = a + int(length_s * fps) - 1
    rng = float(y[:, col].max() - y[:, col].min()) or 1.0
    y[a:z + 1, col] += frac * rng * np.where(np.arange(z - a + 1) % 2 == 0, 1.0, -1.0)
    return y, a, z


@pytest.mark.parametrize("fps", [10.0, 30.0])
@pytest.mark.parametrize("frac", [0.05, 0.2])
def test_sawtooth_on_the_reading_is_jagged_with_its_frames(fps, frac):
    """The answer key's sawtooth (state column 0, 4 s, 5 % or 20 % of the range) is read as jagged on the state
    stream, located within its frames, at 10 and 30 fps alike; the untouched command has no segment."""
    t = int(12 * fps)
    a_cmd = _smooth_traj(t, 7, fps); st, a, z = _sawtooth(_smooth_traj(t, 7, fps, seed=1), fps, frac=frac)
    r = motion_quality(a_cmd, st, fps)
    segs = r.detail.get("jagged") or []
    state = [s for s in segs if s["stream"] == "state"]
    assert state and all(s["col"] == 0 for s in state)
    lo, hi = min(s["start"] for s in state), max(s["end"] for s in state)
    assert a - 2 * int(fps) <= lo <= a + int(fps) and z - int(fps) <= hi <= z + 2 * int(fps)
    assert max(s["amp_rel"] for s in state) >= (0.09 if frac == 0.05 else 0.25)
    assert not [s for s in segs if s["stream"] == "action"]
    assert r.detail["jagged_amp"] >= 0.09


def test_smooth_motion_and_a_still_noisy_joint_are_not_jagged():
    """A clean trajectory has no segment; a joint that sits still under white sensor noise reverses direction only
    half the time (lag-1 correlation of the step -0.5), which is not jagged motion."""
    fps = 30.0; t = 360
    x = _smooth_traj(t, 7, fps)
    assert not (motion_quality(x, x + 1e-4, fps).detail.get("jagged") or [])
    rng = np.random.default_rng(3)
    y = x.copy(); y[:, 2] = 0.3 + rng.normal(0, 0.01, t)            # still joint, white noise is its whole range
    assert not (motion_quality(y, None, fps).detail.get("jagged") or [])
    assert motion_quality(y, None, fps).detail["jagged_amp"] == 0.0


def test_rest_on_noisy_float_pose_is_seen():
    """2026-10-16 (TASK-11): a float32 cartesian pose at rest jitters at 1e-5 per frame; the old rule (a step under
    10 % of the column's median positive step) never fired on it - HABIT ep 31 read 0 of 529 frames at rest while the
    arms waited 1.3 s. The normalized-speed rule reads the rest."""
    rng = np.random.default_rng(21)
    fps, rest, move = 10.0, 13, 200
    t = rest + move
    pose = np.zeros((t, 7), np.float32)
    pose[rest:, :6] = (0.3 * np.sin(np.linspace(0, np.pi, move))[:, None] * np.linspace(0.4, 1.0, 6)[None, :]).astype(np.float32)
    pose += rng.normal(0, 1e-5, pose.shape).astype(np.float32)        # sensor jitter, also while at rest
    cmd = np.diff(pose, axis=0, prepend=pose[:1])                          # a delta command: zero while at rest
    d = motion_quality(cmd, pose, fps, gripper_dims=(6,), control_mode="delta").detail
    assert abs(d["idle_head_s"] - rest / fps) <= 0.2, d["idle_head_s"]
    assert d["idle_tail_s"] < 0.5, d["idle_tail_s"]


def test_idle_edges_follow_the_arm_not_the_leader():
    """The idle edges (TASK-11) are the robot's own rest: a leader arm creeping in the operator's hand before the
    follower moves (so101 ep 1: the command moves from 0.17 s, the state from 1.2 s) does not shorten them; the
    mid-episode pauses keep the command-and-state rule."""
    fps, t = 30.0, 240
    state = np.zeros((t, 6), np.float32)
    state[36:, 0] = np.linspace(0, 50, t - 36, dtype=np.float32)        # the follower starts at 1.2 s
    cmd = state.copy()
    cmd[5:36, 0] = np.cumsum(np.full(31, 0.05, np.float32))             # the leader creeps from 0.17 s
    d = motion_quality(cmd, state, fps, gripper_dims=(5,), control_mode="absolute").detail
    assert abs(d["idle_head_s"] - 1.2) <= 0.15, d["idle_head_s"]


# ---- 2026-10-17:没登记的本体,夹爪列按列名布局 ----------------------------------------------

def test_an_unregistered_joint_robot_takes_its_gripper_columns_from_the_names_layout():
    """Galaxea r1lite: the semantics preflight is confident (joint / absolute), the embodiment registry does not
    know the robot, and the names layout says columns 0 and 1 are the grippers - ACT-5 is assessed on them."""
    from curation.pipeline.episode_checks import make_motion_check

    class _Reg:
        def get(self, _id):
            raise KeyError(_id)
    check = make_motion_check({"checks": {"motion_quality": {"params": {}}}}, _Reg())
    rng = np.random.default_rng(3)
    t = np.linspace(0, 20, 300)
    arms = np.stack([np.sin(t * (1 + 0.2 * j)) for j in range(12)], axis=1)
    grip = np.stack([(np.sin(t * 0.5) > 0).astype(float), (np.sin(t * 0.7) > 0).astype(float)], axis=1)
    a = np.concatenate([grip, arms], axis=1)
    s = a + rng.normal(scale=0.002, size=a.shape)
    extras = json.dumps({"layout": {"gripper_dims": [0, 1], "angle_dims": [], "rotation_blocks": [], "euler_triplet": False,
                                    "translation_dims": [], "velocity_dims": [], "hand_dims": [], "source": "names",
                                    "informative": True}})
    out = check(a, s, 15.0, "joint", "absolute", "joint", "r1lite", "auto", extras)
    d = json.loads(out["detail"]) if isinstance(out.get("detail"), str) else out["detail"]
    assert d["gripper_jitter"] is not None and "gripper_reason" not in d
    assert not ({"0", "1"} & set(d["smoothness_cols"]))                     # the grippers stay out of the kinematics
    # without a layout the old answer stands
    out2 = check(a, s, 15.0, "joint", "absolute", "joint", "r1lite", "auto", "{}")
    d2 = json.loads(out2["detail"]) if isinstance(out2.get("detail"), str) else out2["detail"]
    assert d2["gripper_jitter"] is None and "没有夹爪列" in d2["gripper_reason"]


# ---- 2026-10-18:增量指令的常值小偏置不是「在推」(HABIT 踏板停顿) ----------------------------

def _delta_episode(bias: float, push: float = 0.0, T: int = 400, fps: float = 10.0):
    """A delta command (±0.03 while moving) with a 6 s halt in the middle where the state freezes; during the halt
    the command holds ``bias`` (a teleop device at rest is never exactly zero) or keeps pushing at ``push``."""
    rng = np.random.default_rng(5)
    t = np.arange(T) / fps
    cmd = 0.03 * np.sin(t * 0.8)[:, None] * np.ones((1, 6))
    halt = slice(150, 210)
    cmd[halt] = push if push else bias
    state = np.cumsum(cmd, axis=0) * 0.9 + rng.normal(scale=1e-4, size=(T, 6))
    state[halt] = state[150]
    state[210:] += state[209] - state[210]
    return cmd, state


def test_a_delta_commands_resting_bias_is_not_a_push():
    cmd, state = _delta_episode(bias=0.0009)
    d = motion_quality(cmd, state, 10.0, control_mode="delta", stuck_strategy="increment_vs_pos").detail
    assert d["stuck"] == 1.0 and not d.get("stuck_joints"), d.get("stuck_joints")
    # a real push at full amplitude during the same frozen stretch is still stuck
    cmd2, state2 = _delta_episode(bias=0.0, push=0.03)
    d2 = motion_quality(cmd2, state2, 10.0, control_mode="delta", stuck_strategy="increment_vs_pos").detail
    assert d2["stuck"] < 1.0 and d2.get("stuck_joints")
