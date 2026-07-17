"""隔离清算逻辑(重试+对折)单元测试:stub runner 模拟原生崩溃。"""
from __future__ import annotations

from curation.pipeline.isolation import run_isolated


def _ok(start, n):
    return {"start": start, "n": n}


def test_all_success_no_retry():
    calls = []

    def runner(s, n):
        calls.append((s, n))
        return _ok(s, n)

    results, unrec = run_isolated([(0, 250), (250, 250)], runner)
    assert len(results) == 2 and unrec == []
    assert calls == [(0, 250), (250, 250)]            # 成功批不重跑


def test_transient_crash_recovered_by_retry():
    """并发窗口类崩溃:第一次崩,重试即过——不触发对折。"""
    state = {"fails_left": 1}

    def runner(s, n):
        if (s, n) == (250, 250) and state["fails_left"] > 0:
            state["fails_left"] -= 1
            return None
        return _ok(s, n)

    results, unrec = run_isolated([(0, 250), (250, 250)], runner)
    assert unrec == []
    assert sum(r["n"] for r in results) == 500        # 一条不丢


def test_poison_episode_pinpointed():
    """确定性毒条 ep137:对折应抢救回其余 249 条,点名 (137,1)。"""
    POISON = 137

    def runner(s, n):
        return None if s <= POISON < s + n else _ok(s, n)

    results, unrec = run_isolated([(0, 250)], runner, min_split=1)
    assert unrec == [{"start": POISON, "n": 1}]
    covered = sorted((r["start"], r["n"]) for r in results)
    total = sum(n for _, n in covered)
    assert total == 249                               # 好数据全救回
    # 覆盖不重不漏:249 条 + 毒条 = 完整区间
    eps = set()
    for s, n in covered:
        eps.update(range(s, s + n))
    assert eps == set(range(250)) - {POISON}


def test_two_poisons_same_batch():
    POISON = {60, 200}

    def runner(s, n):
        return None if any(s <= p < s + n for p in POISON) else _ok(s, n)

    results, unrec = run_isolated([(0, 250)], runner, min_split=1)
    assert sorted(u["start"] for u in unrec) == [60, 200]
    assert sum(r["n"] for r in results) == 248


def test_min_split_floor():
    """min_split=50:不细分到单条,失败段以 ≤50 条粒度点名(省时间模式)。"""
    def runner(s, n):
        return None if s <= 137 < s + n else _ok(s, n)

    results, unrec = run_isolated([(0, 250)], runner, min_split=50)
    assert len(unrec) == 1 and unrec[0]["n"] <= 63    # 对折到 50-63 条档位
    assert sum(r["n"] for r in results) + unrec[0]["n"] == 250
