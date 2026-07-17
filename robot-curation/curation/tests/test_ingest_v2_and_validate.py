"""P2.2 验收:M1 v2 分支(DROID/Bridge)+ 多数据集 + validate 拒收路径。"""
from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pytest

from curation.ingest.validate import (
    IngestValidationError,
    validate_episode_row,
    validate_rows,
)

DATA = "/data03/hao/data"


def _need(path: str):
    return pytest.mark.skipif(
        not os.path.exists(os.path.join(DATA, path, "meta", "info.json")),
        reason=f"{path} 数据未下载")


@_need("droid_lerobot")
def test_droid_v2(  ):
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/droid_lerobot", max_episodes=20)
    assert len(rows) == 20
    r0 = rows[0]
    assert r0["action"].shape == (240, 7)          # DROID ep0 已知形状
    assert r0["embodiment_id"] == "franka"
    assert len(r0["video"]) == 3                    # 2 外部 + 1 腕部
    assert all(os.path.exists(v["path"]) for v in r0["video"].values())
    assert rows[6]["instruction"].startswith("Pick the bottle")  # episodes.jsonl 已知内容


@_need("bridge_orig_lerobot")
def test_bridge_v2():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/bridge_orig_lerobot", max_episodes=10)
    assert len(rows) == 10
    assert rows[0]["embodiment_id"] == "widowx"
    assert rows[0]["fps"] == 5.0
    assert rows[0]["instruction"] == "put small spoon from basket to tray"
    assert len(rows[0]["video"]) == 4


@_need("svla_so100_pickplace")
def test_so100_v3():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/svla_so100_pickplace")
    info = json.load(open(f"{DATA}/svla_so100_pickplace/meta/info.json"))
    assert len(rows) == info["total_episodes"]
    assert rows[0]["action"].shape[1] == info["features"]["action"]["shape"][0]


@_need("aloha_sim_insertion_human")
def test_aloha_sim_v3():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/aloha_sim_insertion_human")
    info = json.load(open(f"{DATA}/aloha_sim_insertion_human/meta/info.json"))
    assert len(rows) == info["total_episodes"]
    assert rows[0]["action"].shape[1] == 14        # 双臂 14 维


@pytest.mark.parametrize("name,repo", [
    ("svla_so100_pickplace", "lerobot/svla_so100_pickplace"),
    ("aloha_sim_insertion_human", "lerobot/aloha_sim_insertion_human"),
])
def test_v3_alignment_with_official_loader(name, repo):
    """P2 验收:so100/aloha 也与官方 loader 逐值对齐(pusht 的在 test_lerobot_reader)。"""
    if not os.path.exists(os.path.join(DATA, name, "meta", "info.json")):
        pytest.skip(f"{name} 未下载")
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/{name}")
    hf = LeRobotDataset(repo, root=f"{DATA}/{name}").hf_dataset.with_format(None)
    action = np.asarray(hf["action"], dtype=np.float32)
    epidx = np.asarray(hf["episode_index"])
    for k in (0, len(rows) - 1):
        assert np.allclose(rows[k]["action"], action[epidx == k]), f"{name} ep{k} 不对齐"


@_need("pusht")
def test_embodiment_override():
    """robot_type=unknown 的数据集,接入方人工指定 embodiment_id(DESIGN.md M2)。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    rows = read_lerobot_rows(f"{DATA}/pusht", max_episodes=3, embodiment_id="pusht")
    assert all(r["embodiment_id"] == "pusht" for r in rows)


# ---------- validate 拒收路径 ----------

@_need("pusht")
def test_corrupt_info_json_rejected(tmp_path):
    """故意改坏 info.json → 报清晰错误(P2 验收明确要求)。"""
    from curation.ingest.lerobot_reader import read_lerobot_rows

    corrupt = tmp_path / "pusht_corrupt"
    shutil.copytree(f"{DATA}/pusht", corrupt)
    info_path = corrupt / "meta" / "info.json"
    info = json.load(open(info_path))
    del info["fps"]
    json.dump(info, open(info_path, "w"))

    with pytest.raises(IngestValidationError, match="fps"):
        read_lerobot_rows(str(corrupt), max_episodes=1)


def _good_row():
    return {
        "episode_id": "ep000000",
        "action": np.zeros((10, 7), dtype=np.float32),
        "proprio_state": np.zeros((10, 8), dtype=np.float32),
        "timestamps": np.arange(10) / 10.0,
        "video": {},
        "fps": 10.0,
    }


def test_validate_good_row_passes():
    validate_episode_row(_good_row())


def test_disordered_timestamps_rejected():
    row = _good_row()
    row["timestamps"] = np.array([0.0, 0.1, 0.05] + list(np.arange(3, 10) / 10.0))
    with pytest.raises(IngestValidationError, match="非严格递增"):
        validate_episode_row(row)


def test_nan_action_rejected():
    row = _good_row()
    row["action"][3, 2] = np.nan
    with pytest.raises(IngestValidationError, match="NaN"):
        validate_episode_row(row)


def test_length_mismatch_rejected():
    row = _good_row()
    row["proprio_state"] = row["proprio_state"][:5]
    with pytest.raises(IngestValidationError, match="proprio_state"):
        validate_episode_row(row)


def test_inconsistent_action_dim_rejected():
    rows = [_good_row(), _good_row()]
    rows[1]["action"] = np.zeros((10, 6), dtype=np.float32)  # 帧数不变,只改维度
    with pytest.raises(IngestValidationError, match="维度跨 episode 不一致"):
        validate_rows(rows)