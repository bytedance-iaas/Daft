"""episode 选择的解析/对账守门(issue #110,2026-08-30 八洞齐堵)。

洞的编号见当日审计:①全超界静默空交付 ②部分超界日志印请求数 ③review-page
株连 ④批量逐集空跑 ⑤负数被接受 ⑥巨区间内存炸弹 ⑦--max-episodes 0/负数
语义分裂 ⑧UI 的 N 校验还是任务区小字。三层守门同源:episode_select 是唯一
判据,CLI/管道/UI 只是在不同时刻引用它。
"""
from __future__ import annotations

import os

import pytest

from curation.episode_select import (EpisodesOutOfRange, parse_episodes,
                                     reconcile_episodes)

PUSHT_CANDIDATES = ("/data03/hao/data/pusht", "/mnt/tos/datasets/pusht")


def _pusht() -> str | None:
    for p in PUSHT_CANDIDATES:
        if os.path.isdir(os.path.join(p, "meta")):
            return p
    return None


# ── 解析层(洞⑤⑥)───────────────────────────────────────────────────────

def test_parse_normal_forms():
    assert parse_episodes(None) is None
    assert parse_episodes("") is None
    assert parse_episodes("34") == {34}
    assert parse_episodes("34,56") == {34, 56}
    assert parse_episodes("10-12") == {10, 11, 12}
    assert parse_episodes("3,10-12") == {3, 10, 11, 12}
    assert parse_episodes("3,3,3") == {3}


def test_parse_rejects_negatives():
    """洞⑤:-5 此前能通过解析,进筛选永不匹配 → 静默空跑。"""
    with pytest.raises(ValueError, match="不能为负"):
        parse_episodes("-5")
    with pytest.raises(ValueError, match="不能为负"):
        parse_episodes("3,-5")


def test_parse_rejects_huge_span_before_expanding():
    """洞⑥:1-999999999 此前会当场展开成十亿元素 set(内存炸弹);
    必须在展开**之前**按跨度拒掉。"""
    with pytest.raises(ValueError, match="跨度"):
        parse_episodes("1-999999999")


def test_parse_rejects_reversed_and_garbage():
    with pytest.raises(ValueError, match="颠倒"):
        parse_episodes("20-10")
    with pytest.raises(ValueError):
        parse_episodes("abc")


# ── 对账层(洞①②)───────────────────────────────────────────────────────

def test_reconcile_passthrough_and_clean():
    assert reconcile_episodes(None, {1, 2}) == (None, "")
    kept, warn = reconcile_episodes({1, 2}, {0, 1, 2, 3})
    assert kept == {1, 2} and warn == ""


def test_reconcile_partial_runs_intersection_with_honest_count():
    """洞②:部分超界 → 跑交集,警告里印**实跑数**与缺失清单
    (2026-08-30 用户拍板:跑交集+警告,不一律报错)。"""
    kept, warn = reconcile_episodes(set(range(90, 111)), set(range(100)))
    assert kept == set(range(90, 100))
    assert "21 条里有 11 条不存在" in warn and "只跑存在的 10 条" in warn


def test_reconcile_all_out_of_range_raises():
    """洞①:全超界必须抛错 —— 绝不产出标着成功的空交付(issue #110 本体)。"""
    with pytest.raises(EpisodesOutOfRange, match="全部不存在"):
        reconcile_episodes(set(range(200, 301)), set(range(100)))
    with pytest.raises(EpisodesOutOfRange):
        reconcile_episodes({1}, set())


# ── CLI 层(洞⑦ + review-page 株连③;校验必须先于任何重活)────────────────

# ── 管道层接线(洞①②④;有真数据集才跑,pod/H200 各有一份 pusht)──────────


# ── UI 开跑闸帮手(洞⑧ + 洞①的前哨;判据同源)─────────────────────────────
