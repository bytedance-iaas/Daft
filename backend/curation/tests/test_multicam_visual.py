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

def test_camera_liveness_rides_along_with_visual_quality(tmp_path):
    """★ 相机体检并进视觉质量那一遍:逐相机 detail 里直接带占位/黑帧结论。

    合并前它是报告阶段**再逐条解一遍帧**的独立步骤(串行零输出,200 条时用户
    实见"像卡死")。现在判据吃的就是视觉质量那批采样帧,解码零增长。
    """
    _score, d = _run(tmp_path, {"cam_a": "sharp", "cam_z": "black"})
    assert d["camera_liveness"] == {"live": ["cam_a"], "dead_or_padded": ["cam_z"]}
    assert d["padded_channels"] == ["cam_z"]             # 老键名/老语义原样


def test_all_black_first_camera_is_reported_but_still_scored(tmp_path):
    """首路全黑:体检如实记成占位,但**照常打分**——全黑主相机就该被视觉质量判坏,
    不能靠"跳过占位路"让它蒙混过关(所以它在 dead_or_padded 里、不在 padded_channels)。
    """
    score, d = _run(tmp_path, {"cam_a": "black", "cam_b": "sharp"})
    assert d["camera_liveness"]["dead_or_padded"] == ["cam_a"]
    assert d["padded_channels"] == []
    assert "cam_a" in d["per_camera"] and d["per_camera"]["cam_a"] < 0.1
    assert score < 0.6                                   # 黑路把加权平均拖下来


def test_report_camera_audit_is_pure_assembly():
    """★ 报告那一步只读已有结论:输入里连视频指针都没有,照样出得了 camera_audit
    —— 它要是还想解一帧就只能崩,这就是"不重算"的硬证据。

    另外钉两条口径:①判废但解过帧的条目算进分母(合并后它们也有结论);
    ②相机全活的条目不占版面(与老口径一致)。
    """
    from curation.export.report import collect_camera_audit

    def _vq(live, dead):
        return {"checks": {"visual_quality": {"detail": json.dumps(
            {"camera_liveness": {"live": live, "dead_or_padded": dead}})}}}

    per_episode = {
        "ep000000": {"verdict": "keep", **_vq(["cam_a"], ["cam_z"])},
        "ep000001": {"verdict": "keep", **_vq(["cam_a", "cam_z"], [])},
        "ep000002": {"verdict": "drop", **_vq([], ["cam_a"])},
        # 没走到抽帧那一段(被数值硬门提前杀):没有结论,不进分母
        "ep000003": {"verdict": "drop",
                     "checks": {"timestamp_check": {"detail": "{}"}}},
    }
    audit, n_audited = collect_camera_audit(per_episode)
    assert n_audited == 3
    assert set(audit) == {"ep000000", "ep000002"}
    assert audit["ep000000"] == {"live": ["cam_a"], "dead_or_padded": ["cam_z"]}


def test_live_channel_criterion_uses_sampled_frames_not_just_the_first():
    """判据从"首帧"改成"采样帧统计":开头一帧黑(快门/曝光瞬变)不再把活路误判成占位。"""
    from curation.core.checks.visual_quality import is_live_channel

    rng = np.random.default_rng(3)
    bright = [rng.integers(60, 200, (32, 32, 3), dtype=np.uint8) for _ in range(9)]
    assert is_live_channel([np.zeros((32, 32, 3), np.uint8)] + bright) is True
    assert is_live_channel([np.zeros((32, 32, 3), np.uint8)] * 10) is False
    assert is_live_channel([]) is False                  # 解不出帧 = 不能声称在拍
