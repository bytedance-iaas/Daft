"""P4.3 验收:导出的 v3 数据集被官方 lerobot loader 无警告加载(关键回环);报告对账。"""
from __future__ import annotations

import os
import warnings

import numpy as np
import pytest

PUSHT = "/data03/hao/data/pusht"

pusht_needed = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载")

KEEP = [0, 3, 5, 10, 15]


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    from curation.export.lerobot_writer import export_lerobot_v3

    out = str(tmp_path_factory.mktemp("export") / "pusht_curated")
    stats = export_lerobot_v3(PUSHT, KEEP, out)
    return out, stats


@pusht_needed
def test_export_stats(exported):
    out, stats = exported
    assert stats["episodes"] == 5
    from curation.ingest.lerobot_reader import read_lerobot_rows

    src = read_lerobot_rows(PUSHT, max_episodes=16)
    assert stats["frames"] == sum(len(src[i]["action"]) for i in KEEP)


@pusht_needed
def test_roundtrip_with_our_reader(exported):
    """先用自家 M1 读回:行数/action 逐值对齐。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    out, _ = exported
    back = read_lerobot_rows(out)
    src = read_lerobot_rows(PUSHT, max_episodes=16)
    assert len(back) == 5
    for new_i, old_i in enumerate(KEEP):
        assert np.allclose(back[new_i]["action"], src[old_i]["action"]), f"ep{old_i} 不对齐"


@pusht_needed
def test_official_loader_roundtrip_no_warnings(exported):
    """P4.3 关键回环:官方 LeRobotDataset 无警告加载,帧数对,取样可解码视频。"""
    out, _ = exported
    os.environ.setdefault("HF_HOME", "/data03/hao/.hf_home")
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    with warnings.catch_warnings():
        warnings.simplefilter("error")           # 任何警告都算失败
        ds = LeRobotDataset("lerobot/pusht", root=out)
        assert ds.meta.total_episodes == 5
        item = ds[0]                              # 触发视频解码(验证重编码 mp4+新边界)
    assert "observation.image" in item
    img = item["observation.image"]
    assert tuple(img.shape[-2:]) == (96, 96)

    # action 与源逐值对齐(通过官方 loader 的通道)
    hf = ds.hf_dataset.with_format(None)
    action = np.asarray(hf["action"], dtype=np.float32)
    epidx = np.asarray(hf["episode_index"])
    from curation.ingest.lerobot_reader import read_lerobot_rows

    src = read_lerobot_rows(PUSHT, max_episodes=16)
    for new_i, old_i in enumerate(KEEP):
        assert np.allclose(action[epidx == new_i], src[old_i]["action"])


@pusht_needed
def test_refuse_nonempty_output(exported, tmp_path):
    from curation.export.lerobot_writer import export_lerobot_v3

    d = tmp_path / "occupied"
    d.mkdir()
    (d / "junk.txt").write_text("x")
    with pytest.raises(FileExistsError):
        export_lerobot_v3(PUSHT, [0], str(d))


# ---------- 报告对账 ----------

def test_report_reconciliation():
    from curation.export.report import build_report, to_markdown

    verdicts = {
        "ep0": {"verdict": "keep", "reason": "", "hard_fails": [], "soft_score": 0.9},
        "ep1": {"verdict": "drop", "reason": "硬门违规: kinematic_limits",
                "hard_fails": ["kinematic_limits"], "soft_score": None},
        "ep2": {"verdict": "keep", "reason": "", "hard_fails": [], "soft_score": 0.8},
        "ep3": {"verdict": "drop", "reason": "软分 0.30 < 0.5",
                "hard_fails": [], "soft_score": 0.3},
    }
    stats = {"input": 6, "after_numeric_gates": 5, "survivors_for_vlm": 4, "output": 4}
    dedup = [{"episode_id": "ep2_dup", "duplicate_of": "ep2", "fingerprint": "abc"}]
    profile = {"n_episodes": 2, "n_skills": 1,
               "skills": {"push": {"count": 2, "pct": 100.0, "avg_len_s": 10.0}},
               "undersampled": []}
    r = build_report(verdicts, stats, dedup, profile)

    d = r["dataset"]
    assert d["verdict_keep"] == 2 and d["verdict_drop"] == 2
    assert d["hard_gate_filtered"] == 2                  # input 6 - output 4
    assert d["dedup_removed"] == 1 and d["delivered"] == 1
    assert d["hard_fail_breakdown"] == {"kinematic_limits": 1}
    md = to_markdown(r)
    assert "交付:1 条" in md and "kinematic_limits" in md and "push" in md
    # 数据集注记(2026-07-29):有才出这行,没有不占位
    assert "数据集注记" not in md
    r["dataset"]["dataset_note"] = "state 由 action 累加合成"
    assert "- **数据集注记**: state 由 action 累加合成" in to_markdown(r)