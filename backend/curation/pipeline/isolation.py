"""按批子进程隔离的清算逻辑:重试 + 对折缩围(P5.2 压测/生产批处理共用)。

背景:原生崩溃(segfault)杀死整个子进程,try/except 接不住 → 指挥官只能事后清算。
策略:失败批先原样重试一次(并发窗口类崩溃换个时机大概率过);仍失败则对折递归,
把好数据全部抢救回来,坏数据收敛点名到单条(episode 级"无法处理"清单)。

runner 注入式:runner(start, n) -> dict(成功,内容由调用方定义) | None(子进程崩溃)。
本模块零 daft/子进程依赖 → stub runner 即可单元测试。
"""
from __future__ import annotations

from typing import Callable

Runner = Callable[[int, int], dict | None]


def _salvage(start: int, n: int, runner: Runner, min_split: int,
             results: list[dict], unrecoverable: list[dict]) -> None:
    """对折递归:能跑通的段收进 results,缩到 min_split 仍崩的段进 unrecoverable。"""
    out = runner(start, n)
    if out is not None:
        results.append(out)
        return
    if n <= min_split:
        unrecoverable.append({"start": start, "n": n})
        return
    half = n // 2
    _salvage(start, half, runner, min_split, results, unrecoverable)
    _salvage(start + half, n - half, runner, min_split, results, unrecoverable)


def run_isolated(
    spans: list[tuple[int, int]],
    runner: Runner,
    min_split: int = 1,
) -> tuple[list[dict], list[dict]]:
    """跑全部 (start, n) 批;失败批重试一次,仍败则对折定位。

    返回 (results, unrecoverable):
    - results:所有成功段的 runner 返回值(顺序=完成顺序,含抢救回的子段)
    - unrecoverable:缩到 min_split 仍崩的段(生产上=点名的问题 episode)
    """
    results: list[dict] = []
    failed: list[tuple[int, int]] = []
    for start, n in spans:
        out = runner(start, n)
        if out is not None:
            results.append(out)
        else:
            failed.append((start, n))

    unrecoverable: list[dict] = []
    for start, n in failed:
        retry = runner(start, n)                     # 清算第一步:原样重试一次
        if retry is not None:
            results.append(retry)
            continue
        if n <= min_split:
            unrecoverable.append({"start": start, "n": n})
            continue
        half = n // 2                                 # 清算第二步:对折缩围
        _salvage(start, half, runner, min_split, results, unrecoverable)
        _salvage(start + half, n - half, runner, min_split, results, unrecoverable)
    return results, unrecoverable
