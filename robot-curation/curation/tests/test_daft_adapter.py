"""P2.1 验收:玩具 check 走"注册→适配→DataFrame 跑通"全链(pusht 真数据)。"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from curation.core.contract import CheckResult, Episode, clear_registry, register_check

PUSHT = "/data03/hao/data/pusht"

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(PUSHT, "meta", "info.json")),
    reason="pusht 数据未下载",
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(scope="module")
def pusht_df():
    from curation.ingest.lerobot_reader import read_lerobot

    return read_lerobot(PUSHT)


def _toy_smoothness(ep: Episode) -> CheckResult:
    jerk = float(np.abs(np.diff(ep.action, n=2, axis=0)).mean())
    return CheckResult(name="toy_smoothness", score=1.0 / (1.0 + jerk),
                       detail={"jerk": jerk, "T": len(ep.action)})


def _toy_min_length(ep: Episode) -> CheckResult:
    ok = len(ep.action) >= 60
    return CheckResult(name="toy_min_length", passed=ok, detail={"T": len(ep.action)})


def test_full_chain_soft_check(pusht_df):
    from curation.adapters.daft_adapter import wrap_check, with_checks

    spec = register_check(_toy_smoothness, name="toy_smoothness", needs=("action",))
    df = with_checks(pusht_df, {"toy_smoothness": spec})
    out = df.select("episode_id", "check_toy_smoothness").to_pydict()

    assert len(out["episode_id"]) == 206
    r0 = out["check_toy_smoothness"][0]
    assert 0.0 < r0["score"] <= 1.0
    assert r0["passed"] is None  # 软分不设 passed
    detail = json.loads(r0["detail"])
    assert detail["T"] == 161  # ep0 已知长度,证明 Episode 正确穿透到检查函数


def test_full_chain_hard_gate_filter(pusht_df):
    """硬门:注册→适配→filter 短路,行数变化与真实数据一致。"""
    from curation.adapters.daft_adapter import hard_gate_mask, with_checks

    spec = register_check(_toy_min_length, name="toy_min_length", gate="hard")
    df = with_checks(pusht_df, {"toy_min_length": spec})
    survivors = df.filter(hard_gate_mask(df, {"toy_min_length": spec}))

    n_total = pusht_df.count_rows()
    n_pass = survivors.count_rows()
    # 与纯 Python 对照(同一数据同一规则,两条路径必须一致)
    from curation.ingest.lerobot_reader import read_lerobot_rows

    expected = sum(1 for r in read_lerobot_rows(PUSHT) if len(r["action"]) >= 60)
    assert n_pass == expected, f"daft 硬门({n_pass}) 与纯 Python 对照({expected}) 不一致"
    assert 0 < n_pass < n_total  # 真数据下该规则应既杀一些又留一些


def test_multiple_checks_accumulate(pusht_df):
    from curation.adapters.daft_adapter import with_checks
    from curation.core.contract import all_checks

    register_check(_toy_smoothness, name="toy_smoothness")
    register_check(_toy_min_length, name="toy_min_length", gate="hard")
    df = with_checks(pusht_df.limit(5), all_checks())
    cols = df.column_names
    assert "check_toy_smoothness" in cols and "check_toy_min_length" in cols
    assert df.count_rows() == 5