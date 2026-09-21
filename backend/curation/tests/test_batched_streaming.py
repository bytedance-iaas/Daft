"""P5.2 分块流式正确性:分批结果与整批一致;批大小不改变语义。"""
from __future__ import annotations

import os

import numpy as np
import pytest

PUSHT = "/data03/hao/data/pusht"

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载")


def test_batches_cover_exactly_once():
    from curation.ingest.lerobot_reader import iter_lerobot_batches, read_lerobot_rows

    whole = read_lerobot_rows(PUSHT, max_episodes=30)
    seen = []
    for start, rows in iter_lerobot_batches(PUSHT, batch_size=7, max_episodes=30):
        assert rows[0]["episode_id"] == whole[start]["episode_id"]
        seen.extend(r["episode_id"] for r in rows)
    assert seen == [r["episode_id"] for r in whole]        # 不重不漏且有序


def test_batch_rows_identical_to_full_read():
    from curation.ingest.lerobot_reader import read_lerobot_rows

    whole = read_lerobot_rows(PUSHT, max_episodes=20)
    part = read_lerobot_rows(PUSHT, max_episodes=5, start_episode=10)
    for i in range(5):
        assert part[i]["episode_id"] == whole[10 + i]["episode_id"]
        assert np.allclose(part[i]["action"], whole[10 + i]["action"])


def test_batched_funnel_stats_equal_single_run():
    """数值段漏斗:分 3 批跑与一把跑,聚合统计必须一致(分块不改变判决语义)。"""
    from curation.ingest.lerobot_reader import iter_lerobot_batches, read_lerobot_rows, rows_to_daft
    from curation.pipeline.config import load_config
    from curation.pipeline.funnel import run_funnel
    from curation.registry.registry import EmbodimentRegistry

    cfg = load_config()
    for name in ("visual_quality", "video_action_sync", "task_success"):
        cfg["checks"][name]["enable"] = False              # 只跑数值段,秒级
    reg = EmbodimentRegistry()

    whole = read_lerobot_rows(PUSHT, max_episodes=24, embodiment_id="pusht")
    _, s_all = run_funnel(rows_to_daft(whole), cfg, reg)

    agg = {"input": 0, "output": 0}
    for _, rows in iter_lerobot_batches(PUSHT, batch_size=8, max_episodes=24,
                                        embodiment_id="pusht"):
        _, s = run_funnel(rows_to_daft(rows), cfg, reg)
        agg["input"] += s["input"]
        agg["output"] += s["output"]
    assert agg == {"input": s_all["input"], "output": s_all["output"]}

