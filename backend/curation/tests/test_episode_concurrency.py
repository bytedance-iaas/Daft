"""VLM 段 episode 级并发的闸门测试(2026-07-22)。

背景:VLM 段占端到端 97% 的时间(实测 10 条 DROID:关掉 VLM 12s / 开 VLM 365s),
且几乎全是等服务端响应的网络阻塞 → episode 级并发是这里唯一有意义的提速手段。

⚠️ 本文件存在的根本原因:**daft 0.7.16 的 `max_concurrency=` 对 async 行级 UDF
不限在飞协程数**(实测设 1/2/4/12,峰值一律等于 morsel 全部行数)。这是**静默**失效
——不报错、不告警,只是闸门不存在。它当时造成了两个真实损失:
  ① 以为在跑"串行基线"其实是全并发,得出"改造无效"的错误结论,白测一轮;
  ② 上规模时在飞数 = morsel 行数,乘帧级并发后会砸穿服务端配额。
故闸门自己实现(_episode_gate),并由本测试钉死。若某天升级 daft 后本文件的
test_daft_max_concurrency_still_does_not_gate 变绿(即 daft 自己会限流了),
可以考虑删掉自建闸门——**但在那之前不许信它**。
"""
from __future__ import annotations

import asyncio
import time

import daft
import pytest

from curation.pipeline.funnel import _episode_gate


def _run_with_gate(limit: int, rows: int = 8, sleep_s: float = 0.2):
    """跑一条 daft async UDF,返回 (峰值在飞数, 墙钟耗时)。"""
    state = {"n": 0, "max": 0}

    @daft.func(return_dtype=daft.DataType.int64())
    async def f(x):
        async with _episode_gate(limit):
            state["n"] += 1
            state["max"] = max(state["max"], state["n"])
            await asyncio.sleep(sleep_s)
            state["n"] -= 1
        return int(x)

    t0 = time.time()
    daft.from_pydict({"x": list(range(rows))}).with_column("y", f(daft.col("x"))).collect()
    return state["max"], time.time() - t0


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_gate_caps_inflight(limit):
    """闸门必须把在飞条数压在 limit 以内——这是防砸穿服务端配额的唯一保证。"""
    peak, _ = _run_with_gate(limit, rows=8)
    assert peak <= limit, f"闸门={limit} 却有 {peak} 条同时在飞"


def test_gate_actually_parallelizes():
    """闸门不能只会限流不会放行:并发 4 必须显著快于串行。

    8 行 × 0.2s:串行 ≥1.6s,并发4 理论 0.4s。取 2 倍加速为及格线(留 CI 抖动余量),
    这样既能抓住"退化成串行"的回归,又不会因机器慢而假红。
    """
    t_serial = _run_with_gate(1, rows=8)[1]
    t_par = _run_with_gate(4, rows=8)[1]
    assert t_par < t_serial / 2, f"并发4 未提速:串行 {t_serial:.2f}s vs 并发 {t_par:.2f}s"


def test_gate_rebuilds_per_event_loop():
    """信号量按事件循环缓存:换 loop 必须换新的,否则会 'bound to a different loop'。

    daft 每次 collect() 可能起新 loop,复用旧信号量会在运行期炸。
    """
    peak1, _ = _run_with_gate(2, rows=4)
    peak2, _ = _run_with_gate(2, rows=4)      # 第二次 collect,大概率是新 loop
    assert peak1 <= 2 and peak2 <= 2


def test_gate_limit_change_takes_effect():
    """同一进程内改并发数必须生效(配置 --set 覆盖后不能还用旧信号量)。"""
    assert _run_with_gate(1, rows=4)[0] == 1
    assert _run_with_gate(4, rows=4)[0] > 1


def test_daft_max_concurrency_still_does_not_gate():
    """⚠️ 钉住 daft 的实际行为:`max_concurrency=` 对 async 行级 UDF **不限流**。

    这不是在测 daft 的 bug,是在测"我们赖以做决定的前提还成不成立"。
    本测试变红 = daft 行为变了(它开始限流了)→ 去复核自建闸门是否还必要、
    以及两层闸门叠加会不会把并发压得过低。**不要直接删掉本测试了事。**
    """
    state = {"n": 0, "max": 0}

    @daft.func(return_dtype=daft.DataType.int64(), max_concurrency=1)
    async def f(x):
        state["n"] += 1
        state["max"] = max(state["max"], state["n"])
        await asyncio.sleep(0.2)
        state["n"] -= 1
        return int(x)

    daft.from_pydict({"x": list(range(8))}).with_column("y", f(daft.col("x"))).collect()
    assert state["max"] > 1, (
        f"daft 的 max_concurrency=1 现在真的限流了(峰值 {state['max']})——"
        "前提变了,去复核 funnel.py 的自建闸门注释与必要性"
    )
