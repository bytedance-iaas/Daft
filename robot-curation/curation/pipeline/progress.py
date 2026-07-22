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
                      "step": max(1, max(int(total), 0) // 20), "lock": None}
    return key


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
    pct = f" ({n / total * 100:.0f}%)" if total else ""
    msg = f"[curation] {st['label']} {n}/{total or '?'}{pct} | 已用 {_fmt_dur(elapsed)}"
    if total and n:
        msg += f" | 剩余 ~{_fmt_dur(elapsed / n * (total - n))}"
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
