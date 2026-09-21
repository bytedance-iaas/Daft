"""M1 懒扫描(daft_source)金对齐测试:懒/急两路必须逐值一致。

标准(2026-07-10 /loop 定):
① schema 一致;② 行集合一致;③ action/proprio/timestamps 逐值 bit 级相等;
④ video 指针/语义列相等;⑤ limit 下推不破坏正确性;⑥ 构造 df 零数据读取(懒的本义)。
真数据集覆盖 v3(pusht)与 v2(bridge);缺数据集时 skip(CI 无数据环境)。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

PUSHT = "/data03/hao/data/pusht"
BRIDGE = "/data03/hao/data/bridge_orig_lerobot"
DROID = "/data03/hao/data/droid_lerobot"

需要 = lambda p: pytest.mark.skipif(  # noqa: E731
    not os.path.exists(os.path.join(p, "meta", "info.json")), reason=f"无数据集 {p}")


def _eager_df(path, n):
    from curation.ingest.lerobot_reader import read_lerobot_rows, rows_to_daft
    return rows_to_daft(read_lerobot_rows(path, max_episodes=n, skip_missing=True))


def _lazy_df(path, n):
    from curation.ingest.daft_source import read_lerobot_lazy
    return read_lerobot_lazy(path, max_episodes=n)


def _assert_identical(path, n):
    e = _eager_df(path, n).to_pydict()
    l = _lazy_df(path, n).to_pydict()
    assert set(e.keys()) == set(l.keys()), "列集合不一致"
    assert e["episode_id"] == l["episode_id"], "行集合/顺序不一致"
    for i in range(len(e["episode_id"])):
        np.testing.assert_array_equal(np.asarray(e["action"][i]),
                                      np.asarray(l["action"][i]))
        if e["proprio_state"][i] is not None:
            np.testing.assert_array_equal(np.asarray(e["proprio_state"][i]),
                                          np.asarray(l["proprio_state"][i]))
        np.testing.assert_array_equal(np.asarray(e["timestamps"][i]),
                                      np.asarray(l["timestamps"][i]))
    for col in ("video", "fps", "instruction", "control_mode", "stuck_strategy",
                "euler_triplet", "unit", "semantics_source", "action_space",
                "proprio_space", "embodiment_id"):
        assert e[col] == l[col], f"列 {col} 不一致"


@需要(PUSHT)
def test_golden_v3_pusht():
    _assert_identical(PUSHT, 8)


@需要(BRIDGE)
def test_golden_v2_bridge():
    _assert_identical(BRIDGE, 30)


@需要(DROID)
def test_golden_v2_droid_semantics():
    """droid:velocity 语义 profile 必须原样进懒扫描列。"""
    out = _lazy_df(DROID, 3).select("control_mode", "stuck_strategy",
                                    "semantics_source").to_pydict()
    assert out["control_mode"] == ["velocity"] * 3
    assert out["stuck_strategy"] == ["velocity_dual_scale"] * 3
    assert out["semantics_source"] == ["profile"] * 3


@需要(PUSHT)
def test_schema_identical_v3():
    assert repr(_eager_df(PUSHT, 2).schema()) == repr(_lazy_df(PUSHT, 2).schema())


@需要(BRIDGE)
def test_schema_identical_v2():
    assert repr(_eager_df(BRIDGE, 2).schema()) == repr(_lazy_df(BRIDGE, 2).schema())


@需要(PUSHT)
def test_limit_pushdown_correct():
    """limit < max_episodes:行数正确且值与急切路前缀一致。"""
    l = _lazy_df(PUSHT, 8).limit(3).to_pydict()
    e = _eager_df(PUSHT, 3).to_pydict()
    assert l["episode_id"] == e["episode_id"]


@需要(PUSHT)
def test_lazy_construction_reads_no_data(monkeypatch):
    """懒的本义:构造 DataFrame(profile 数据集)不触碰任何 data parquet。"""
    import pandas as pd

    from curation.ingest import daft_source, lerobot_reader

    calls = []
    orig = pd.read_parquet

    def spy(path, *a, **k):
        p = str(path)
        if os.sep + "data" + os.sep in p:      # data parquet(meta/episodes 不算)
            calls.append(p)
        return orig(path, *a, **k)

    monkeypatch.setattr(lerobot_reader.pd, "read_parquet", spy)
    df = daft_source.read_lerobot_lazy(PUSHT, max_episodes=5)
    assert calls == [], f"构造期不应读 data parquet,却读了 {calls[:3]}"
    df.count_rows()                             # 执行才读
    assert calls, "执行期应读 data parquet"


@需要(DROID)
def test_lazy_skip_missing_gap():
    """droid 下载缺口(ep2772-2774):懒扫描跳过继续,不崩。"""
    from curation.ingest.daft_source import read_lerobot_lazy
    out = read_lerobot_lazy(DROID, max_episodes=2780).select("episode_id").to_pydict()
    ids = set(out["episode_id"])
    assert "ep002771" in ids and "ep002775" in ids
    assert "ep002773" not in ids
