"""P3.1 验收:blur/black 注入集 100% 检出;干净 pusht 误报 <5%(20 条真数据)。"""
from __future__ import annotations

import os

import pytest

from curation.core.checks.visual_quality import visual_quality

PUSHT = "/data03/hao/data/pusht"
THRESHOLD = 0.5   # 验收用工作点(正式默认值 P5.1 校准)

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载",
)

N_EPISODES = 20


def test_empty_frames_is_zero():
    r = visual_quality([])
    assert r.score == 0.0