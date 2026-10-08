"""v1 的数据集导出已退役（D69）。

平台只出质检报告，不再写交付数据集：三个格式的写出器（LeRobot / mcap / rrd）、增量重导出、
导出清单与边出边传的发布器都删掉了。v1 的流水线（`curation run`、`rejudge`，仍挂在
`cli/legacy.py` 上）在判定与报告这一半照旧能跑，走到它自己的导出那一段时调到这里、明确报错，
而不是悄悄地少交付一样东西。真要用 v1 的导出，去 `release_v1` 分支。

它顶替的名字按原样给出：写出器与发布器的构造函数一律 :func:`retired`（调用即报错），
``activate`` 是空上下文、``active`` 永远 None（v1 那几处「有没有在随产随传」的判断照常走），
``INDEX_NAME`` 是原格式交付清单的文件名。
"""
from __future__ import annotations

import contextlib

#: v1 的原格式交付目录里的清单文件名（rrd / mcap 写出器原来的常量）。
INDEX_NAME = "index.json"

MESSAGE = ("数据集导出已下线（D69）：平台只出质检报告，不再写交付数据集。"
           "v1 的导出代码在 release_v1 分支")


def retired(*_args, **_kwargs):
    """Every entry point of the retired writers and the publisher lands here."""
    raise RuntimeError(MESSAGE)


#: ``publish.Publisher``: constructing one is already the export, so it is refused.
Publisher = retired


def active(*_args, **_kwargs):
    """``publish.active()``: there is no publisher any more, so nothing is streaming out."""
    return None


@contextlib.contextmanager
def activate(_publisher=None):
    """``publish.activate()``: a no-op context, as it always was without a publisher."""
    yield
