"""episode 选择的解析/对账守门(issue #110,2026-08-30 八洞齐堵)。

洞的编号见当日审计:①全超界静默空交付 ②部分超界日志印请求数 ③review-page
株连 ④批量逐集空跑 ⑤负数被接受 ⑥巨区间内存炸弹 ⑦--max-episodes 0/负数
语义分裂 ⑧UI 的 N 校验还是任务区小字。三层守门同源:episode_select 是唯一
判据,CLI/管道/UI 只是在不同时刻引用它。
"""
from __future__ import annotations

import json
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

def test_cli_run_rejects_zero_and_negative_max(tmp_path, capsys):
    """洞⑦:--max-episodes 0 此前在 run 里静默空跑;负数落进 iloc[:-N] 变成
    "去掉末尾 N 条"的意外语义。校验在碰输入之前,故意给不存在的路径。"""
    from curation.cli import main
    for bad in ("0", "-3"):
        rc = main(["run", "--input", "/no/such/dataset",
                   "--output", str(tmp_path / "o"), "--max-episodes", bad])
        assert rc == 2
        assert "正整数" in capsys.readouterr().err


def test_cli_run_rejects_bad_expressions_early(tmp_path, capsys):
    from curation.cli import main
    for bad, kw in (("-5", "不能为负"), ("1-999999999", "跨度")):
        rc = main(["run", "--input", "/no/such/dataset",
                   "--output", str(tmp_path / "o"), "--episodes", bad])
        assert rc == 2
        assert kw in capsys.readouterr().err


def test_cli_review_page_same_guards(tmp_path, capsys):
    """洞③⑦:review-page 与 run 同判据 —— 0 此前被真值判断当"没给"(跑全部),
    表达式语法错此前裸 traceback。"""
    from curation.cli import main
    rc = main(["review-page", "--input", "/no/such/dataset",
               "--output", str(tmp_path / "o"), "--max-episodes", "0"])
    assert rc == 2
    assert "正整数" in capsys.readouterr().err
    rc = main(["review-page", "--input", "/no/such/dataset",
               "--output", str(tmp_path / "o"), "--episodes", "abc"])
    assert rc == 2
    assert "解析失败" in capsys.readouterr().err


# ── 管道层接线(洞①②④;有真数据集才跑,pod/H200 各有一份 pusht)──────────

def test_pipeline_reconciles_before_running(tmp_path, capsys):
    pusht = _pusht()
    if not pusht:
        pytest.skip("没有 pusht 真数据集")
    from curation.pipeline.run import run_pipeline
    # 全超界:开跑前就死,绝不产出空交付
    with pytest.raises(EpisodesOutOfRange, match="全部不存在"):
        run_pipeline(None, pusht, str(tmp_path / "o1"), embodiment_id="pusht",
                     episode_indices={99999}, only_checks="timestamp_check")
    assert not (tmp_path / "o1" / "passed.json").exists()
    # 部分超界:跑交集,日志印实跑数(洞②:此前印请求数)
    s = run_pipeline(None, pusht, str(tmp_path / "o2"), embodiment_id="pusht",
                     episode_indices={0, 99999}, only_checks="timestamp_check")
    txt = capsys.readouterr().out
    assert "2 条里有 1 条不存在" in txt and "只跑存在的 1 条" in txt
    assert "实跑 1 条" in txt
    assert s["stats"].get("input") == 1


# ── UI 开跑闸帮手(洞⑧ + 洞①的前哨;判据同源)─────────────────────────────

def test_ui_episode_helpers(tmp_path):
    from curation.ui import runner
    # 手滑类:N=0/非数字/负数、表达式语法错,全部字段名开头说人话
    assert "正整数" in runner.episodes_input_error("", "0")
    assert "正整数" in runner.episodes_input_error("", "-3")
    assert "正整数" in runner.episodes_input_error("", "abc")
    assert "解析失败" in runner.episodes_input_error("-5", "")
    assert "跨度" in runner.episodes_input_error("1-999999999", "")
    assert runner.episodes_input_error("3,10-12", "5") == ""
    # 全超界前哨:total_episodes 来自 meta;读不到→放行交给管道
    ds = tmp_path / "tiny"
    (ds / "meta").mkdir(parents=True)
    (ds / "meta" / "info.json").write_text(
        json.dumps({"total_episodes": 5}), encoding="utf-8")
    err = runner.episodes_range_error("10-20", str(tmp_path), ["tiny"])
    assert "共 5 条" in err and "0-4" in err
    assert runner.episodes_range_error("3-20", str(tmp_path), ["tiny"]) == ""
    assert runner.episodes_range_error("10-20", str(tmp_path), ["nosuch"]) == ""
    assert runner.episodes_range_error("", str(tmp_path), ["tiny"]) == ""
