"""多相机视觉质检验收:全路检查/最差路聚合/占位黑帧路豁免。"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest


def _write_mp4(path, frames, fps=10):
    import cv2

    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for f in frames:
        vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    vw.release()


def _textured(T=30, blur=False, black=False):
    rng = np.random.default_rng(0)
    out = []
    for t in range(T):
        if black:
            f = np.zeros((64, 64, 3), np.uint8)
        else:
            f = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)
            if blur:
                import cv2

                f = cv2.GaussianBlur(f, (21, 21), 8)
            f[..., 0] = (f[..., 0] * 0.5 + t * 3) % 255   # 帧间变化,避免误判冻结
        out.append(f)
    return out


def _row(tmp, cams):
    video = {}
    for name, kind in cams.items():
        p = tmp / f"{name}.mp4"
        _write_mp4(p, _textured(blur=(kind == "blur"), black=(kind == "black")))
        video[name] = {"path": str(p), "from_ts": 0.0, "to_ts": 3.0}
    T = 30
    return {"episode_id": "ep0", "embodiment_id": "so100", "instruction": "",
            "action": np.cumsum(np.random.default_rng(1).normal(0, 0.3, (T, 6)),
                                axis=0).astype(np.float32) + 90.0,
            "proprio_state": None, "timestamps": np.arange(T) / 10.0, "fps": 10.0,
            "action_space": "joint", "control_mode": "absolute", "video": video}


def _run(tmp, cams, weights=None):
    from curation.ingest.lerobot_reader import rows_to_daft
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    cfg = load_config()
    for name in ("video_action_sync", "task_success", "motion_quality",
                 "kinematic_limits", "timestamp_check"):
        if name in cfg["checks"]:
            cfg["checks"][name]["enable"] = False
    if weights is not None:
        cfg["checks"]["visual_quality"]["camera_weights"] = weights
    df, _ = run_funnel(rows_to_daft([_row(tmp, cams)]), cfg, EmbodimentRegistry())
    out = df.select("check_visual_quality").to_pydict()["check_visual_quality"][0]
    return out["score"], json.loads(out["detail"])


def test_weighted_average_aggregation(tmp_path):
    """一路清晰一路糊 → 总分=等权平均;worst_camera 仍点名事实。"""
    score, d = _run(tmp_path, {"cam_a": "sharp", "cam_b": "blur"})
    assert d["worst_camera"] == "cam_b"
    assert d["per_camera"]["cam_a"] > 0.9
    expect = (d["per_camera"]["cam_a"] + d["per_camera"]["cam_b"]) / 2
    assert abs(score - expect) < 1e-3                    # 等权平均
    assert d["camera_weights"] == {"cam_a": 1.0, "cam_b": 1.0}
    assert d["params"]["blur_ref_var"] == 100.0          # 参数透明进 detail
    assert d["params"]["frame_max_side"] == 448


def test_camera_weight_override(tmp_path):
    """用户把糊那路权重设 0 → 总分只看清晰路(短名匹配)。"""
    score, d = _run(tmp_path, {"cam_a": "sharp", "cam_b": "blur"},
                    weights={"cam_b": 0.0})
    assert score == d["per_camera"]["cam_a"] > 0.9
    assert d["camera_weights"]["cam_b"] == 0.0


def test_padded_channel_exempt(tmp_path):
    """占位黑帧路:登记进 padded_channels,不打分,不拖垮总分。"""
    score, d = _run(tmp_path, {"cam_a": "sharp", "cam_z": "black"})
    assert d["padded_channels"] == ["cam_z"]
    assert "cam_z" not in d["per_camera"]
    assert score > 0.9                                   # 只按活跃路计


BRIDGE = "/data03/hao/data/bridge_orig_lerobot"


@pytest.mark.skipif(not os.path.exists(os.path.join(BRIDGE, "meta")), reason="无 bridge 数据")
def test_bridge_real_padded_channels(tmp_path):
    """真数据:bridge 的黑帧占位路(image_3 等)被豁免,分照常。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows, rows_to_daft
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    cfg = load_config()
    for name in ("video_action_sync", "task_success", "motion_quality",
                 "kinematic_limits", "timestamp_check"):
        cfg["checks"][name]["enable"] = False
    rows = read_lerobot_rows(BRIDGE, max_episodes=2, validate=False)
    df, _ = run_funnel(rows_to_daft(rows), cfg, EmbodimentRegistry())
    for out in df.select("check_visual_quality").to_pydict()["check_visual_quality"]:
        d = json.loads(out["detail"])
        assert "observation.images.image_3" in d["padded_channels"]
        assert out["score"] == 1.0                       # 活跃路都干净