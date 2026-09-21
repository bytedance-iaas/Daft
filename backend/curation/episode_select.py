"""episode 选择的解析与对账 —— run / review-page / UI 三方共用的唯一判据。

issue #110(2026-08-30 定案):此前表达式只做语法解析,与数据集实际范围
从不对账 —— 全超界(100 条的集上填 200-300)一路绿灯跑完,交付出一份标着
成功的空交付;部分超界静默跑交集,日志还印请求数不印实跑数。三层守门自此
同源:手滑类(负数/巨区间/前 N 条=0)死在解析层,对账类(超不超界)由
reconcile_episodes 统一裁,UI 只是把同样的判决提前到点按钮那一刻。
"""
from __future__ import annotations

#: 单个区间允许的最大跨度。没有数据集有百万条 episode;超过它的表达式
#: 几乎必是手滑(如 1-999999999),而解析层会当场把区间展开成 set ——
#: 不设上限就是十亿元素的内存炸弹(2026-08-30 审计发现)。
MAX_SPAN = 1_000_000


class EpisodesOutOfRange(ValueError):
    """指定的 episode 与数据集实有范围交集为空(绝不产出空交付)。"""


def parse_episodes(expr: str | None) -> set[int] | None:
    """"34" / "34,56" / "10-20" / "3,10-12" → {int};非法抛 ValueError。

    负数一律拒(episode 编号从 0 起,负数只会静默筛出空集);区间展开前先算
    跨度,超 MAX_SPAN 直接拒 —— 见模块 docstring。
    """
    if not expr:
        return None
    out: set[int] = set()
    for part in str(expr).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):          # 区间(不把负号当分隔)
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if lo_i < 0 or hi_i < 0:
                raise ValueError(f"episode 编号不能为负: {part}")
            if hi_i < lo_i:
                raise ValueError(f"区间起止颠倒: {part}")
            if hi_i - lo_i + 1 > MAX_SPAN:
                raise ValueError(
                    f"区间 {part} 跨度 {hi_i - lo_i + 1} 条:没有数据集有这么多"
                    f" episode,应该是手滑(上限 {MAX_SPAN})")
            out.update(range(lo_i, hi_i + 1))
        else:
            v = int(part)
            if v < 0:
                raise ValueError(f"episode 编号不能为负: {part}")
            out.add(v)
    if not out:
        raise ValueError("未解析出任何 episode 编号")
    return out


def _preview(s: set[int], k: int = 8) -> str:
    xs = sorted(s)
    body = ", ".join(str(x) for x in xs[:k])
    return body + ("…" if len(xs) > k else "")


def reconcile_episodes(requested: set[int] | None, available,
                       what: str = "数据集") -> tuple[set[int] | None, str]:
    """指定集合 × 实有集合 → (实跑集合, 警告串或空串)。

    - requested 为 None(没指定)→ 原样放行,无话可说;
    - 交集为空 → 抛 EpisodesOutOfRange(调用方按输入错误收场,绝不空跑);
    - 部分超界 → 返回交集 + 一句说清"要的多少、缺的哪些、实跑多少"的警告
      (2026-08-30 用户拍板:跑交集+警告,不一律报错)。
    """
    if requested is None:
        return None, ""
    avail = {int(x) for x in available}
    kept = requested & avail
    if not kept:
        if avail:
            span = f"共 {len(avail)} 条(编号 {min(avail)}-{max(avail)})"
        else:
            span = "一条 episode 都没有"
        raise EpisodesOutOfRange(
            f"指定的 episode 全部不存在:{what}{span},"
            f"你指定的 {_preview(requested)} 一条也没有")
    missing = requested - avail
    if not missing:
        return kept, ""
    return kept, (f"指定的 {len(requested)} 条里有 {len(missing)} 条不存在"
                  f"({_preview(missing)}),只跑存在的 {len(kept)} 条")
