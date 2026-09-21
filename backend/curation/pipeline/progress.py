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

#: 阶段式进度的"最近一步"登记(group → 该步信息):心跳线程据此在长 LLM 调用
#: 期间重报"仍在这一步"。phase_done(group) 收账;不收账就换步也会自动顶掉。
_PHASE: dict = {}

#: 心跳(issue 讨论 2026-08-30 用户拍板:静默期看着像卡死):条目式阶段在
#: 首条完成前、阶段式在单次大调用期间,只要超过这个间隔没有新行,就把当前
#: 行原样重报一遍(带最新"已用")——任何时刻都有一根在呼吸的条。CLI 侧同样
#: 受益:长静默每 15s 一行,不算刷屏。
_HB_INTERVAL_S = 15.0
_HB_TICK_S = 5.0
_HB_THREAD = {"started": False}


def _hb_scan(now: float) -> list[str]:
    """一次心跳扫描 → 该补打的行(测试直接调它,不用等真线程)。"""
    out: list[str] = []
    for st in list(_PROGRESS.values()):
        total = st.get("total") or 0
        if not total or st.get("n", 0) >= total:
            continue                          # 没开张的未知量/已完成:不吭声
        if now - st.get("t0", now) < st.get("quiet_before_s", 0.0):
            continue                          # 快阶段的静默期照旧
        if now - max(st.get("last", 0.0), st.get("t0", 0.0)) < _HB_INTERVAL_S:
            continue
        st["last"] = now
        n = st.get("n", 0)
        pct = f" ({n / total * 100:.0f}%)"
        tail = " | 首批在飞…" if n == 0 else " | 仍在跑…"
        out.append(f"[curation] {st['label']} {n}/{total}{pct}"
                   f" | 已用 {_fmt_dur(now - st['t0'])}{tail}")
    for g, ph in list(_PHASE.items()):
        if now - ph["ts"] < _HB_INTERVAL_S:
            continue
        ph["ts"] = now
        used = f" | 已用 {_fmt_dur(now - ph['t0'])}" if ph.get("t0") else ""
        out.append(f"[curation] {g} 第 {ph['step']}/{ph['total']} 阶段 "
                   f"{ph['what']}{used} | 仍在这一步…")
    return out


def _hb_ensure_thread() -> None:
    if _HB_THREAD["started"]:
        return
    import os
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # 测试环境不起真线程:模块级 _PROGRESS 会被各用例的假状态污染,常驻
        # 线程把别家的僵尸条打进无关用例的 capsys(pod 实测:一条 t0 接近
        # epoch 的遗留项报出「已用 496684.9h」)。心跳逻辑用 _hb_scan 直测。
        return
    _HB_THREAD["started"] = True
    import threading
    import time

    def _loop():
        while True:
            time.sleep(_HB_TICK_S)
            try:
                for line in _hb_scan(time.time()):
                    print(line, flush=True)
            except Exception:  # noqa: BLE001 心跳绝不弄死跑批
                pass

    threading.Thread(target=_loop, daemon=True, name="progress-hb").start()


def phase_done(group: str) -> None:
    """阶段式流程收账:这个 group 不再有"仍在这一步"的心跳。"""
    _PHASE.pop(group, None)

#: 近段速率的窗口(秒)与历史条数上限。
#: ⚠️ 为什么不用全程平均(2026-08-13 用户实见「已用 41s | 剩余 ~6s」之后又跑了 40 秒):
#: VLM 段是并发的,开头一批同时冲进去、完成得又快又密,把全程平均速率抬得很高;
#: 尾巴上只剩零星几条排队,真实速率掉下来,于是估计一路偏乐观。近段速率跟着当下的
#: 节奏走,先快后慢时给出的剩余明显更接近实际。
#: 历史只在打印时追加(约每 5% 一条),上限是个兜底 —— 跑十万条也不该无限长。
_RATE_WINDOW_S = 60.0
_RATE_HISTORY_MAX = 64

#: ETA 预热:完成量不足总数 10%(下限 3 条、上限 50 条)时不报剩余。
#: ⚠️ 2026-08-25 droid-50 实测:并发段头几条的完成时刻近乎随机,全程平均速率
#: 严重失真 —— 1/49 时报「剩余 ~57min」,4/49 掉到 ~14min,实际尾巴 4 分钟,
#: 数字一路跳水比不给更伤信任。预热期只报 n/total 与已用时长,都是实话。
_ETA_WARMUP_FRAC_DEN = 10          # 总数的 1/10
_ETA_WARMUP_MIN_N = 3
_ETA_WARMUP_MAX_N = 50             # 十万条也不该等到一万条才开口

#: 剩余小于这个数就不报数字,改说「收尾中」。
#: ⚠️ 没结束就不许显示 `~0s`:那是"马上就好"的承诺,而并发段的最后几条常常还要
#: 磨很久,承诺完再跑 40 秒比不给数字更伤信任。
_ETA_FLOOR_S = 5.0


def _progress_init(key: str, total: int, label: str, min_interval_s: float = 20.0,
                   quiet_before_s: float = 0.0, unit: str = "条") -> str:
    """登记一个条目式阶段,返回给 UDF 捕获的 key(纯字符串,可序列化)。

    节流是两条规则的或:每 total/20 条打一次(**与数据量无关,总共约 20 行**),
    或距上次超过 min_interval_s。完成行(n==total)永远打。

    quiet_before_s:头几秒一律不打中间行(完成行不受影响)。用于**又快又多**的阶段
    ——数值检查在 10 条上是毫秒级,不设静默就会瞬间刷 10 行噪音;而同一段跑 10 万条
    要几分钟,那时又确实需要进度。一个参数同时伺候两种规模,不必按数据量分支。
    """
    import time

    # unit:计数单位,跟在 N/M 后面打进日志、任务卡照抄(2026-09-04 用户定:同一张卡上
    # "5/5 阶段""39/39 个文件""2/2 条"混在一起,不带单位会被当成条数)。
    _PROGRESS[key] = {"total": max(int(total), 0), "label": label, "n": 0, "unit": unit,
                      "t0": time.time(), "last": 0.0, "min_interval_s": min_interval_s,
                      "quiet_before_s": quiet_before_s,
                      "step": max(1, max(int(total), 0) // 20), "lock": None,
                      "hist": []}
    # 开跑先亮条(2026-08-30 用户:首条完成前进度条什么都不显示,像卡死):
    # 0/N 立即可见;快阶段(设了静默期的)照旧不吭声,免得毫秒级阶段刷噪音
    if total and quiet_before_s <= 0:
        print(f"[curation] {label} 0/{total} {unit} (0%) | 已用 0s", flush=True)
    _hb_ensure_thread()
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
    if n < min(max(_ETA_WARMUP_MIN_N,
                   (total + _ETA_WARMUP_FRAC_DEN - 1) // _ETA_WARMUP_FRAC_DEN),
               _ETA_WARMUP_MAX_N):
        return None                        # 预热期:样本太少,估了也是瞎估
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
    unit = st.get("unit", "条")
    msg = f"[curation] {st['label']} {n}/{total or '?'} {unit}{pct} | 已用 {_fmt_dur(elapsed)}"
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
    # 打"第 N/M 阶段":与条目式的"N/M 条"分得开(2026-09-04 用户定)
    print(f"[curation] {group} 第 {step}/{total} 阶段 {what}{used}", flush=True)
    # 登记给心跳:这一步(可能是一次几分钟的 LLM 大调用)期间每 15s 重报一次
    # "仍在这一步";换步自动顶掉,整段结束调 phase_done(group) 收账
    _PHASE[group] = {"step": step, "total": total, "what": what,
                     "t0": t0, "ts": time.time()}
    _hb_ensure_thread()


def _fmt_dur(sec: float) -> str:
    sec = max(float(sec), 0.0)
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}min"
    return f"{sec / 3600:.1f}h"
