"""codebase_version 声明与 meta/ 布局不符时,报错必须指名道姓(2026-08-09)。

背景:doctor 拿 v3 文件名找 v2.1 任务表产出误导性 FAIL;我们按声明分派不犯那个错,
但声明本身写错时旧报错只说"找不到元数据"——要说清是 info.json 标错,不是数据坏。
"""
from __future__ import annotations

import json
import os

import pytest

from curation.ingest.lerobot_reader import NotADatasetError, read_lerobot_meta


def _mk(tmp_path, version: str, layout: str):
    meta = tmp_path / "meta"
    meta.mkdir()
    (meta / "info.json").write_text(json.dumps(
        {"codebase_version": version, "fps": 15, "robot_type": "franka",
         "features": {}}))
    if layout == "v2":
        (meta / "episodes.jsonl").write_text("")
        (meta / "tasks.jsonl").write_text("")
    else:
        (meta / "episodes").mkdir()
    return str(tmp_path)


def test_declared_v3_but_v2_layout_names_the_culprit(tmp_path):
    d = _mk(tmp_path, "v3.0", "v2")
    with pytest.raises(NotADatasetError, match="疑似 codebase_version 标错"):
        read_lerobot_meta(d)


def test_declared_v2_but_v3_layout_names_the_culprit(tmp_path):
    d = _mk(tmp_path, "v2.1", "v3")
    with pytest.raises(NotADatasetError, match="疑似 codebase_version 标错"):
        read_lerobot_meta(d)


def test_consistent_declaration_not_intercepted(tmp_path):
    """声明与布局一致时,核对器放行——后续该怎么读怎么读(缺文件是另一种报错)。"""
    d = _mk(tmp_path, "v2.1", "v2")
    try:
        read_lerobot_meta(d)          # 空 episodes.jsonl → 读出 0 条,不该抛"标错"
    except NotADatasetError as e:
        assert "标错" not in str(e)
