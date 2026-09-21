"""P1 spike1 产物测试:LeRobot v3 reader(需要本地 pusht 数据,缺数据自动 skip)。"""
from __future__ import annotations

import os

import numpy as np
import pytest

PUSHT = "/data03/hao/data/pusht"

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载(scripts/download_test_data.py)",
)


@pytest.fixture(scope="module")
def rows():
    os.environ.setdefault("HF_HOME", "/data03/hao/.hf_home")
    from curation.ingest.lerobot_reader import read_lerobot_rows

    return read_lerobot_rows(PUSHT)


def test_row_count_matches_episodes(rows):
    assert len(rows) == 206


def test_variable_length_actions(rows):
    lens = {len(r["action"]) for r in rows}
    assert len(lens) > 1, "所有 episode 等长,切分可疑"
    assert rows[0]["action"].shape == (161, 2)  # pusht ep0 已知形状
    assert rows[0]["action"].dtype == np.float32


def test_episode_fields(rows):
    r = rows[0]
    assert r["episode_id"] == "ep000000"
    assert r["fps"] == 10.0
    assert len(r["timestamps"]) == len(r["action"]) == len(r["proprio_state"])
    # 时间戳单调递增(M5a-L0 的前置假设)
    assert np.all(np.diff(r["timestamps"]) > 0)


def test_video_pointer_not_bytes(rows):
    """流式设计:M1 只存指针,不读视频字节。"""
    v = rows[0]["video"]["observation.image"]
    assert os.path.exists(v["path"])
    assert v["to_ts"] > v["from_ts"] >= 0.0
    # 同一合并 mp4 中,相邻 episode 边界应首尾相接(v3 语义)
    v1 = rows[1]["video"]["observation.image"]
    assert v1["path"] == v["path"]
    assert abs(v1["from_ts"] - v["to_ts"]) < 1e-6


def test_alignment_with_official_loader(rows):
    """关键验收:抽 2 条与官方 lerobot loader 逐值对齐(慢,加载官方数据集)。"""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds = LeRobotDataset("lerobot/pusht", root=PUSHT)
    hf = ds.hf_dataset.with_format(None)
    action = np.asarray(hf["action"], dtype=np.float32)
    epidx = np.asarray(hf["episode_index"])
    for k in (0, 100):
        assert np.allclose(rows[k]["action"], action[epidx == k]), f"ep{k} 与官方不对齐"


def test_daft_dataframe_roundtrip(rows):
    from curation.ingest.lerobot_reader import read_lerobot

    df = read_lerobot(PUSHT)
    assert df.count_rows() == 206
    schema = {f.name: str(f.dtype) for f in df.schema()}
    assert "Tensor" in schema["action"], f"action 应为变长 tensor,got {schema['action']}"


def test_unknown_version_rejected(tmp_path):
    """未知 codebase_version 要明确报错,而非静默错读(v2 正向覆盖见 test_ingest_v2_and_validate)。"""
    import json
    import shutil

    from curation.ingest.lerobot_reader import read_lerobot_rows

    weird = tmp_path / "pusht_v9"
    shutil.copytree(PUSHT, weird)
    info_path = weird / "meta" / "info.json"
    info = json.load(open(info_path))
    info["codebase_version"] = "v9.0"
    json.dump(info, open(info_path, "w"))

    with pytest.raises(ValueError, match="未知 LeRobot 版本"):
        read_lerobot_rows(str(weird), max_episodes=1)