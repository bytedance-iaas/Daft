"""P4.1 验收:duplicate 注入 100% 检出;非重复 0 误删(真数据)。"""
from __future__ import annotations

import os

import numpy as np
import pytest

from curation.dataset_level.dedup import episode_fingerprint, exact_dedup
from curation.tests import corrupt

PUSHT = "/data03/hao/data/pusht"

pusht_needed = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载")


@pytest.fixture(scope="module")
def rows():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    return read_lerobot_rows(PUSHT, max_episodes=20)


@pusht_needed
def test_clean_zero_false_deletion(rows):
    kept, dropped = exact_dedup(rows)
    assert len(kept) == 20 and not dropped, f"非重复被误删: {dropped}"


@pusht_needed
def test_injected_duplicates_100pct(rows):
    mixed = list(rows)
    for i in (2, 7, 11):
        mixed, _ = corrupt.duplicate(mixed, i)
    kept, dropped = exact_dedup(mixed)
    assert len(kept) == 20 and len(dropped) == 3
    assert {d["duplicate_of"] for d in dropped} == \
        {rows[2]["episode_id"], rows[7]["episode_id"], rows[11]["episode_id"]}


@pusht_needed
def test_v3_shared_file_not_confused(rows):
    """v3 合并 mp4:所有 episode 共享同一文件 → 只比文件会全灭;时间窗必须参与指纹。"""
    paths = {r["video"]["observation.image"]["path"] for r in rows}
    assert len(paths) == 1, "前提:pusht 前 20 条共享一个合并 mp4"
    fps = {episode_fingerprint(r) for r in rows}
    assert len(fps) == 20, "共享文件下指纹应仍两两不同(时间窗区分)"


@pusht_needed
def test_action_tweak_breaks_fingerprint(rows):
    """action 改一个值就不算重复(绝不误删的底线)。"""
    import copy

    twin = copy.deepcopy(rows[0])
    twin["episode_id"] = "ep_twin"
    twin["action"] = np.array(twin["action"], copy=True)
    twin["action"][0, 0] += 1e-3
    kept, dropped = exact_dedup([rows[0], twin])
    assert len(kept) == 2 and not dropped


def test_dedup_no_video_rows():
    r1 = {"episode_id": "a", "action": np.zeros((5, 2), dtype=np.float32), "video": {}}
    r2 = {"episode_id": "b", "action": np.zeros((5, 2), dtype=np.float32), "video": {}}
    r3 = {"episode_id": "c", "action": np.ones((5, 2), dtype=np.float32), "video": {}}
    kept, dropped = exact_dedup([r1, r2, r3])
    assert [r["episode_id"] for r in kept] == ["a", "c"]
    assert dropped[0]["duplicate_of"] == "a"