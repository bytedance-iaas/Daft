"""C1(角度解绕) + B组(单位守卫/增量弃权/相机探针/stats预警) 验收。"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from curation.core.checks.motion_quality import motion_quality
from curation.ingest.lerobot_reader import _infer_control_mode
from curation.ingest.validate import stats_prior_warnings


def _smooth_yaw_crossing(T=120):
    """绝对 EE 位姿:yaw 匀速慢转,中途跨越 ±π——物理完全平滑。"""
    a = np.zeros((T, 7), dtype=np.float64)
    yaw = np.linspace(2.9, 3.5, T)                       # 跨 π≈3.1416
    a[:, 5] = (yaw + np.pi) % (2 * np.pi) - np.pi        # 存储时回卷 → 出现 +3.1→-3.1 跳变
    a[:, 0] = np.linspace(0, 0.2, T)                     # xyz 平缓
    return a


def test_c1_wrap_false_spike_fixed():
    a = _smooth_yaw_crossing()
    naive = motion_quality(a, None, 15.0, gripper_dims=(6,))
    fixed = motion_quality(a, None, 15.0, gripper_dims=(6,),
                           angle_dims=(3, 4, 5), angle_mode="absolute")
    # 未解绕:回绕假跳变被当成尖刺(分数被砸);解绕后:平滑轨迹拿高分
    assert fixed.detail["spike"] > 0.9
    assert fixed.detail["spike"] > naive.detail["spike"]
    assert naive.detail["spike"] < 0.5             # 证明这曾是真误报


def test_c1_delta_wrap_artifact_rewrapped():
    """增量表示:回绕伪影=单帧 ≈2π 的假增量,回卷后应不再触发尖刺。"""
    a = np.random.default_rng(0).normal(0, 0.004, (100, 7))
    a[:, 6] = 1.0
    a[50, 5] = 6.27                                       # 假 +2π 增量(真实转动≈-0.013)
    naive = motion_quality(a, None, 5.0, gripper_dims=(6,))
    fixed = motion_quality(a, None, 5.0, gripper_dims=(6,),
                           angle_dims=(3, 4, 5), angle_mode="delta")
    assert fixed.detail["spike"] > 0.9
    assert naive.detail["spike"] < 0.5


def test_b2_control_mode_fingerprint():
    T = 200
    absolute = np.cumsum(np.random.default_rng(1).normal(0, 0.01, (T, 6)), axis=0) + 100.0
    delta = np.random.default_rng(2).normal(0, 0.005, (T, 7))
    assert _infer_control_mode(absolute) == "absolute"
    assert _infer_control_mode(delta) == "delta"
    assert _infer_control_mode(np.zeros((2, 3))) == "unknown"     # 太短
    assert _infer_control_mode(np.zeros((50, 3))) == "unknown"    # 全零


def test_b1_b2_kinematics_abstain_in_funnel():
    """funnel 级:关节增量→弃权;单位错配(弧度数据×度制极限)→弃权;都不杀数据。"""
    from curation.ingest.lerobot_reader import rows_to_daft
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    cfg = load_config()
    for name in ("visual_quality", "video_action_sync", "task_success", "motion_quality"):
        cfg["checks"][name]["enable"] = False
    reg = EmbodimentRegistry()

    def row(eid, action, cmode):
        return {"episode_id": eid, "embodiment_id": "so100", "instruction": "",
                "action": action.astype(np.float32), "proprio_state": None,
                "timestamps": np.arange(action.shape[0]) / 30.0, "fps": 30.0,
                "action_space": "joint",
                "control_mode": cmode,
                "video": {"cam": {"path": "/nonexistent.mp4", "from_ts": 0.0, "to_ts": 1.0}}}

    delta_a = np.random.default_rng(0).normal(0, 0.01, (100, 6))          # 关节增量
    rad_a = np.clip(np.random.default_rng(1).normal(0, 1.0, (100, 6)), -3, 3)  # 弧度 × 度制表
    rows = [row("epA", delta_a, "delta"), row("epB", rad_a, "absolute")]
    df, stats = run_funnel(rows_to_daft(rows), cfg, reg)
    out = df.select("episode_id", "check_kinematic_limits").to_pydict()
    res = dict(zip(out["episode_id"], out["check_kinematic_limits"]))
    assert res["epA"]["passed"] is None and "增量" in res["epA"]["detail"]
    assert res["epB"]["passed"] is None and "错配" in res["epB"]["detail"]
    assert stats["output"] == 2                            # 弃权≠杀


def test_b4_stats_prior_warnings(tmp_path):
    meta = tmp_path / "meta"
    meta.mkdir()
    stats = {"observation.images.cam_dead": {"mean": [[[0.03]], [[0.02]], [[0.04]]]},
             "observation.images.cam_ok": {"mean": [[[0.45]], [[0.4]], [[0.5]]]},
             "action": {"mean": [0.0]}}
    (meta / "stats.json").write_text(json.dumps(stats))
    warns = stats_prior_warnings(str(tmp_path))
    assert len(warns) == 1 and "cam_dead" in warns[0]
    assert stats_prior_warnings("/nonexistent_dir") == []


BRIDGE = "/data03/hao/data/bridge_orig_lerobot"


@pytest.mark.skipif(not os.path.exists(os.path.join(BRIDGE, "meta")), reason="无 bridge 数据")
def test_live_camera_channels_on_bridge():
    """真数据:image_0 恒活,image_3 是黑帧占位。

    判据 2026-08-14 从"独立再解一遍帧的相机体检"搬进视觉质量那一遍
    (is_live_channel 吃的就是那批采样帧),这里改成直接喂帧验同一个结论。
    """
    from curation.adapters.decode import decode_window
    from curation.core.checks.visual_quality import is_live_channel
    from curation.ingest.lerobot_reader import read_lerobot_rows

    r = read_lerobot_rows(BRIDGE, max_episodes=1, validate=False)[0]
    live = {}
    for cam, v in sorted(r["video"].items()):
        fr, _ = decode_window(v["path"], v["from_ts"], v["to_ts"],
                              sample_interval_s=0.5, max_side=224)
        live[cam] = is_live_channel(fr)
    assert live["observation.images.image_0"] is True
    assert live["observation.images.image_3"] is False


@pytest.mark.skipif(not os.path.exists(os.path.join(BRIDGE, "meta")), reason="无 bridge 数据")
def test_b2_real_datasets_fingerprint():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    br = read_lerobot_rows(BRIDGE, max_episodes=1, validate=False)[0]
    assert br["control_mode"] == "delta"                   # Bridge=EE 增量
    al = read_lerobot_rows("/data03/hao/data/aloha_sim_insertion_human",
                           max_episodes=1, validate=False)[0]
    assert al["control_mode"] == "absolute"                # aloha=绝对关节角
