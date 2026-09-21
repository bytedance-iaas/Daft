"""v3 数据集 episodes 表缺 tasks 列时的标注回退(2026-09-02 libero 教训)。

v3 规范里 meta/episodes 的 tasks 列是可选的;有的转换器只写 data 里的 task_index +
meta/tasks.parquet。此前三条读路都只看 tasks 列,标注全空,系统报"全员缺标注"。"""
from __future__ import annotations

import os

import pytest

PUSHT = next((p for p in ("/data03/hao/data/pusht", "/mnt/tos/datasets/pusht")
              if os.path.exists(p)), None)


def _drop_tasks_column(monkeypatch):
    import curation.ingest.lerobot_reader as LR
    orig = LR._load_episodes_meta

    def no_tasks(dataset_dir):
        df = orig(dataset_dir)
        return df.drop(columns=[c for c in df.columns if c == "tasks"])
    monkeypatch.setattr(LR, "_load_episodes_meta", no_tasks)


def test_tasks_map_reads_official_index_style(tmp_path):
    """官方 v3:任务文本是索引、task_index 是列;兼容 task/task_index 两列写法。"""
    import pandas as pd
    from curation.ingest.lerobot_reader import _load_tasks_map
    meta = tmp_path / "meta"; meta.mkdir()
    pd.DataFrame({"task_index": [0, 1]}, index=["push the block", "pull the block"]) \
        .to_parquet(meta / "tasks.parquet")
    assert _load_tasks_map(str(tmp_path)) == {0: "push the block", 1: "pull the block"}
    pd.DataFrame({"task_index": [3], "task": ["stack"]}).to_parquet(meta / "tasks.parquet")
    assert _load_tasks_map(str(tmp_path)) == {3: "stack"}
    assert _load_tasks_map(str(tmp_path / "nowhere")) == {}


@pytest.mark.skipif(PUSHT is None, reason="pusht 数据未下载")
def test_meta_eager_and_lazy_paths_fall_back_to_task_index(monkeypatch, capsys):
    import curation.ingest.lerobot_reader as LR
    from curation.ingest.daft_source import read_lerobot_lazy
    want = [r["instruction"] for r in LR.read_lerobot_meta(PUSHT, max_episodes=3)]
    assert all(want) and want[0].startswith("Push the T-shaped block")
    _drop_tasks_column(monkeypatch)
    got_meta = [r["instruction"] for r in LR.read_lerobot_meta(PUSHT, max_episodes=3)]
    assert got_meta == want
    assert "回填标注:3/3" in capsys.readouterr().out
    got_rows = [r["instruction"] for r in LR.read_lerobot_rows(PUSHT, max_episodes=3, validate=False)]
    assert got_rows == want
    got_lazy = read_lerobot_lazy(PUSHT, max_episodes=3).select("instruction").to_pydict()["instruction"]
    assert got_lazy == want
