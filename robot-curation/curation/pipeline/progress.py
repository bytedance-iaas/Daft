"""CLI 进度显示(阶段级)。

背景:慢阶段静默 = 用不了。DROID 200 条带 VLM 跑一小时零输出,分不清"在跑"还是
"卡死"。2026-07-22 补齐到全流程:数值段/抽帧段/VLM 段/技能画像都有输出。

两种显示,按**能不能数**来选,不混用:
- 条目式 `_progress_init/_progress_tick`:有明确总数的逐条阶段(N 条 episode)→
  报 n/total + 百分比 + ETA。
- 阶段式 `phase_step`:一步就是一次 LLM 大调用,既没有可数单位、耗时也不可预测 →
  **只报"第几步 / 在做什么",不编百分比**。给不可预测的步骤配假进度条是骗人:
  卡住时用户还以为在动。

⚠️ 状态存模块级全局按 key 索引,而不是放对象里让 UDF 闭包捕获:**daft 要求 UDF 可
   序列化**(cloudpickle),threading.Lock 不可 pickle,实测直接报
   "cannot pickle '_thread.lock' object"。UDF 里只捕获一个字符串 key,锁与计数在
   执行侧按 key 惰性创建。这同时预演了 RayRunner 的 UDF 序列化约束。
"""
from __future__ import annotations

_PROGRESS: dict = {}

#: 近段速率的窗口(秒)与历史条数上限。
#: ⚠️ 为什么不用全程平均(2026-08-13 用户实见「已用 41s | 剩余 ~6s」之后又跑了 40 秒):
#: VLM 段是并发的,开头一批同时冲进去、完成得又快又密,把全程平均速率抬得很高;
#: 尾巴上只剩零星几条排队,真实速率掉下来,于是估计一路偏乐观。近段速率跟着当下的
#: 节奏走,先快后慢时给出的剩余明显更接近实际。
#: 历史只在打印时追加(约每 5% 一条),上限是个兜底 —— 跑十万条也不该无限长。
_RATE_WINDOW_S = 60.0
_RATE_HISTORY_MAX = 64

#: 剩余小于这个数就不报数字,改说「收尾中」。
#: ⚠️ 没结束就不许显示 `~0s`:那是"马上就好"的承诺,而并发段的最后几条常常还要
#: 磨很久,承诺完再跑 40 秒比不给数字更伤信任。
_ETA_FLOOR_S = 5.0


def _progress_init(key: str, total: int, label: str, min_interval_s: float = 20.0,
                   quiet_before_s: float = 0.0) -> str:
    """登记一个条目式阶段,返回给 UDF 捕获的 key(纯字符串,可序列化)。

    节流是两条规则的或:每 total/20 条打一次(**与数据量无关,总共约 20 行**),
    或距上次超过 min_interval_s。完成行(n==total)永远打。

    quiet_before_s:头几秒一律不打中间行(完成行不受影响)。用于**又快又多**的阶段
    ——数值检查在 10 条上是毫秒级,不设静默就会瞬间刷 10 行噪音;而同一段跑 10 万条
    要几分钟,那时又确实需要进度。一个参数同时伺候两种规模,不必按数据量分支。
    """
    import time

    _PROGRESS[key] = {"total": max(int(total), 0), "label": label, "n": 0,
                      "t0": time.time(), "last": 0.0, "min_interval_s": min_interval_s,
                      "quiet_before_s": quiet_before_s,
                      "step": max(1, max(int(total), 0) // 20), "lock": None,
                      "hist": []}
    return key


def _eta_seconds(st: dict, now: float, n: int, total: int):
    """剩余秒数,用**最近一个窗口**的速率外推;窗口内样本不够就退回全程平均。

    必须在持锁时调用:它要读写 st["hist"](进度是挂在 daft UDF 上逐条 tick 的,
    多线程同时进来)。返回 None = 说不出(没有总数,或一条都还没完成)。
    """
    hist = st.setdefault("hist", [])       # setdefault:老状态字典里可能还没这个键
    prev = hist[:]                         # 本次这个点不能拿来跟自己比
    hist.append((now, n))
    if len(hist) > _RATE_HISTORY_MAX:
        del hist[:-_RATE_HISTORY_MAX]
    if not total or n <= 0 or n >= total:
        return None
    elapsed = now - st["t0"]
    rate = n / elapsed if elapsed > 0 else 0.0        # 兜底:全程平均速率
    # 窗口内最早的那个采样点;整个窗口里一个点都没有(阶段慢到两次打印隔了一分钟
    # 以上)就退而取最近的那个 —— 那仍是"近段",比全程平均贴近当下节奏。
    base = next(((t, k) for t, k in prev if now - t <= _RATE_WINDOW_S),
                prev[-1] if prev else None)
    if base and now > base[0] and n > base[1]:
        rate = (n - base[1]) / (now - base[0])
    return (total - n) / rate if rate > 0 else None


def _progress_tick(key: str) -> None:
    """逐条调用。锁在执行侧惰性创建 → 不进闭包、不参与序列化。

    显示的是"**已完成** N 条"而非"正在处理第 N 条":daft 可能并行执行行,顺序不保证。
    """
    import threading
    import time

    st = _PROGRESS.get(key)
    if st is None:                       # 反序列化到别的进程 → 静默跳过,绝不因进度条中断质检
        return
    if st["lock"] is None:
        st["lock"] = threading.Lock()
    with st["lock"]:
        st["n"] += 1
        n, total = st["n"], st["total"]
        now = time.time()
        done = n == total
        # 静默期:快阶段在头几秒不打中间行,但完成行照打 → 10 条时只剩一行汇总
        if not done and now - st["t0"] < st.get("quiet_before_s", 0.0):
            return
        due = (n % st["step"] == 0) or done or \
              (now - st["last"] >= st["min_interval_s"])
        if not due:
            return
        st["last"] = now
        elapsed = now - st["t0"]
        eta = _eta_seconds(st, now, n, total)      # 历史也在锁里维护
    pct = f" ({n / total * 100:.0f}%)" if total else ""
    msg = f"[curation] {st['label']} {n}/{total or '?'}{pct} | 已用 {_fmt_dur(elapsed)}"
    if eta is not None:
        msg += (" | 收尾中" if eta < _ETA_FLOOR_S else f" | 剩余 ~{_fmt_dur(eta)}")
    print(msg, flush=True)


def phase_step(group: str, step: int, total: int, what: str,
               t0: float | None = None) -> None:
    """阶段式进度:报"第几步 / 在做什么",**不报百分比**。

    用于不可数、耗时不可预测的步骤(一步=一次 LLM 大调用)。t0 给了就附带累计用时,
    让用户能判断整体还要多久——这是我们能诚实提供的唯一时间信息。
    """
    import time

    used = f" | 已用 {_fmt_dur(time.time() - t0)}" if t0 else ""
    print(f"[curation] {group} {step}/{total} {what}{used}", flush=True)


def _fmt_dur(sec: float) -> str:
    sec = max(float(sec), 0.0)
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}min"
    return f"{sec / 3600:.1f}h"
