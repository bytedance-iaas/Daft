"""The model transport: OpenAI-compatible endpoints (vLLM serve, Volcengine Ark).

The concurrency gate, hedged requests, latency and usage rows, endpoint probes and model listing; the production
task-success assessor and the picture-defect request are built here from the pipeline YAML (the endpoint and model
appear only there) and live in ``video_vlm.py`` (design doc 13: continuous multi-camera video).
"""
from __future__ import annotations

import os
import queue as _queue
import threading
import time as _time
import uuid


# HTTP 计时探针(诊断用,始终累计,开销可忽略)。判读:
#   Σ请求耗时 / VLM段墙钟 ≈ 有效并发度;≈1 说明请求实际被串行化了。
#   Σ请求耗时 / 请求数     = 平均单请求延迟;并发下明显变大 = 服务端在排队。
# 2026-07-28 升级为分桶收集器(同事要定量延时档案):四个调用点各挂静态标签
# (probe 渐变问询 / endstate 二值复核 / caption 画像 / llm 归纳审计),逐请求
# 记 (标签, 秒, 成败, 发出时刻),run 结束汇总进报告 + details/vlm_latency.csv。
# 注意口径:这是**客户端视角**延时 = 网络 + 服务端排队 + 推理;并发大时单请求
# 变慢通常是排队挪到了服务端,不是模型变慢。
#
# 2026-07-30 增记 started_at(调用发出时刻),为了算**墙钟**(第一次发出 → 最后
# 一次返回的真实时长)。没有时刻就只能拿"次数 × 均值"当耗时,而我们是并发跑的,
# 那个数会把 8 分钟的实际耗时说成 8 小时 —— 严重误导,已废弃。
# 为什么用 time.time()(epoch)而不是 time.monotonic():
#   monotonic 的零点是**进程私有**的(不同进程、重启后互不可比),而延时行会跨
#   worker 进程汇总、CSV 还要留档给事后复算;epoch 在所有进程/机器上同一把尺子,
#   代价只是系统时钟被 NTP 拨动时可能有秒级抖动(相对分钟级的墙钟可以忽略)。
_HTTP_STATS = {"n": 0, "secs": 0.0}
_HTTP_LOCK = threading.Lock()          # 模块级,不进 UDF 闭包 → 不参与 cloudpickle
#: 每行 = (tag, seconds, ok, started_at, call_id, attempt, fail_kind),latency_reset 清零。
#: 2026-08-15 从 4 元组扩到 7 元组(对冲补发上线):call_id 把同一次**逻辑调用**的
#: 两发钩在一起(否则明细回答不了"补发有没有救回来");attempt 0=首发 1=补发;
#: fail_kind 区分"为什么没成"(timeout/http_error/connect_error,成功为空串)。
_LAT_ROWS: list = []


def _with_thinking(payload: dict, thinking: bool | None) -> dict:
    """Keep the legacy argument harmless; reasoning is selected by the Curation API.

    The Daemon freezes ``vlm.reasoning_effort`` on the task and passes it through the
    transport policy.  A separate boolean thinking switch could conflict with that
    value, so it is deliberately not emitted here.
    """
    del thinking
    return dict(payload)


def latency_record(tag: str, dt: float, ok: bool = True,
                   started_at: float | None = None, *,
                   call_id: str | None = None, attempt: int = 0,
                   fail_kind: str = "") -> None:
    """记一次真实发出的 HTTP 请求。started_at = 请求发出时刻(time.time() epoch 秒)。

    started_at 传 None 表示"没记时刻"(老代码路径/合成数据)——该桶就没有墙钟,
    上层如实降级,**绝不**拿次数×均值顶替。
    call_id 传 None 表示"没有逻辑调用分组"(老代码路径)——汇总时该行自成一组。
    """
    with _HTTP_LOCK:
        _LAT_ROWS.append((tag, dt, ok, started_at, call_id, int(attempt), fail_kind))
        _HTTP_STATS["n"] += 1
        _HTTP_STATS["secs"] += dt


def latency_reset() -> None:
    """跑批开始时清零(每个 run 一份独立档案)。"""
    with _HTTP_LOCK:
        _LAT_ROWS.clear()
        _HTTP_STATS["n"] = 0
        _HTTP_STATS["secs"] = 0.0


def latency_rows() -> list:
    """逐请求明细快照(供 CSV 落盘)。"""
    with _HTTP_LOCK:
        return list(_LAT_ROWS)


def _pctl(sorted_vals: list, q: float) -> float:
    """nearest-rank 分位数:ceil(q·n) 名(样本量几十到几千,精细插值无意义)。"""
    import math
    i = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[i]


def latency_summary(rows: list | None = None) -> dict:
    """按标签汇总:次数/错误/均值/P50/P90/P99/最大 + 墙钟(秒,保留2位)。

    rows=None 取当前进程累计的明细;传 rows 可对 CSV 读回的历史明细复算。

    2026-08-15 增补对冲口径(界面/报告说人话的原料):
      attempts    = 发起次数(含失败与补发的每一次真实 HTTP 请求);
      hedged      = 超时线后补发的逻辑调用数;retried = 快速报错后串行重发的;
      unanswered  = 两发都没拿到结果的逻辑调用数(老数据没有分组 → 即 errors);
      unanswered_timeout = 其中失败原因全是超时的(界面据此选「没等到回应」措辞)。
    n/errors/分位数的口径**不变**(n 只数成功请求,分位数只算成功请求)。

    wall_s(墙钟)= 该类调用**忙碌区间的并集长度**:把每次调用的 [发出, 返回]
    区间合并去重后求总长。⚠️ 不能用"首发→末返"的跨度——同类调用分多个阶段跑时
    (caption 在开头补标、中段画像各一波),中间隔着别的阶段,空档会把墙钟灌水
    到离谱(2026-08-06 droid-30 实锤:caption 实跑 90s 被算成 988s,条形图把它
    画成头号瓶颈)。并集口径下连续阶段与旧口径一致,分段阶段只算真在飞的时长。
    失败的调用**也算**(它同样占了那段时间)。
    该桶一条 started_at 都没有(三列老 CSV / 老代码)→ 不给 wall_s,上层降级。
    """
    rows = latency_rows() if rows is None else list(rows)
    # 兼容 4 元组老行(外部代码/历史 CSV 读出):补齐 call_id/attempt/fail_kind 默认值
    rows = [tuple(r) + (None, 0, "")[len(r) - 4:] if len(r) < 7 else tuple(r)
            for r in rows]
    out = {}
    for tag in sorted({r[0] for r in rows}):
        mine = [r for r in rows if r[0] == tag]
        oks = sorted(r[1] for r in mine if r[2])
        errs = sum(1 for r in mine if not r[2])
        entry = {"n": len(oks), "errors": errs, "attempts": len(mine)}
        # ── 逻辑调用分组(2026-08-15 对冲补发):同 call_id 的两发算一次逻辑调用;
        # 没有 call_id 的行(老 CSV/散记)每行自成一组 —— 此时 unanswered==errors,
        # 老交付的汇总数字因此一个不变。
        groups: dict = {}
        for i, r in enumerate(mine):
            groups.setdefault(r[4] if r[4] is not None else f"_row{i}", []).append(r)
        hedged = retried = unanswered = unanswered_timeout = 0
        for g in groups.values():
            first = min(g, key=lambda r: r[5])
            if any(r[5] > 0 for r in g):
                # 首发超时线上还挂着(或最终成功)= 对冲补发;首发快速报错 = 串行重发
                if first[2] or first[6] == "timeout":
                    hedged += 1
                else:
                    retried += 1
            if not any(r[2] for r in g):
                unanswered += 1
                if g and all(r[6] == "timeout" for r in g):
                    unanswered_timeout += 1
        entry.update({"hedged": hedged, "retried": retried,
                      "unanswered": unanswered,
                      "unanswered_timeout": unanswered_timeout})
        if oks:
            entry.update({
                "mean_s": round(sum(oks) / len(oks), 2),
                "p50_s": round(_pctl(oks, 0.50), 2),
                "p90_s": round(_pctl(oks, 0.90), 2),
                "p99_s": round(_pctl(oks, 0.99), 2),
                "max_s": round(oks[-1], 2)})
        stamped = [r for r in mine if r[3] is not None]
        if stamped:
            ivs = sorted((r[3], r[3] + r[1]) for r in stamped)
            busy, cs, ce = 0.0, ivs[0][0], ivs[0][1]
            for s, e in ivs[1:]:
                if s > ce:
                    busy += ce - cs
                    cs, ce = s, e
                else:
                    ce = max(ce, e)
            busy += ce - cs
            entry["wall_s"] = round(busy, 2)
        out[tag] = entry
    return out


def read_latency_csv(path: str) -> list:
    """details/vlm_latency.csv → rows(供事后复算墙钟/分位数)。

    兼容两代老 CSV,缺列按默认值降级(老交付的剖析页照常算、数字不变):
    - 2026-07-30 之前的**三列**(call_type,seconds,ok):无 started_at → 无墙钟;
    - 2026-08-15 之前的**四列**:无 call_id/attempt/fail_kind → 每行自成一次
      逻辑调用、全按首发、失败原因未知(界面措辞据此降级,不硬编原因)。
    """
    import csv as _csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            st = (r.get("started_at") or "").strip()
            cid = (r.get("call_id") or "").strip()
            att = (r.get("attempt") or "").strip()
            rows.append((r["call_type"], float(r["seconds"]),
                         bool(int(r["ok"])), float(st) if st else None,
                         cid or None, int(att) if att else 0,
                         (r.get("fail_kind") or "").strip()))
    return rows


#: vlm_latency.csv 的列头(cli/runctx.py 落盘;前四列是老契约不动)
LATENCY_CSV_HEADER = ["call_type", "seconds", "ok", "started_at",
                      "call_id", "attempt", "fail_kind"]


#: Token usage side channel (v2, design doc 04 §5). None = off, exactly as v1. The v2 CLI
#: installs a callable(tag, response_or_None) that books every HTTP request this module
#: sends: the usage block of a response it can read, or one request of unknown usage (a
#: timeout, a connection error, an error status). It is called after the fact, next to
#: latency_record, and never changes what is sent, returned or recorded above.
_USAGE_SINK: dict = {"fn": None}


def set_usage_sink(fn) -> None:
    """Install (or with None remove) the usage sink; returns nothing."""
    _USAGE_SINK["fn"] = fn


def usage_note(tag: str, response) -> None:
    """Hand one sent request to the usage sink; a failing sink is ignored."""
    fn = _USAGE_SINK["fn"]
    if fn is None:
        return
    try:
        fn(tag, response)
    except Exception:  # noqa: BLE001  bookkeeping must never break a model call
        pass


# 并发请求默认值(2026-07-20 实测方舟:并发1→0.10 req/s、8→0.49、16→0.96,零 429、延迟不涨
# ≈7.8s/请求恒定 ⇒ 瓶颈是串行等待而非服务端限流)。默认 8 = n_probe 常用值,一条 episode
# 的 VOC 问询一轮打完;**保持可配置**:上 Ray 后每 worker 调小,总并发=worker数×本值,
# 一个旋钮压在服务端配额下(见 progress backlog)。
# ⚠️ 上面那个 7.8s 是**小 payload** 下的数;真实流水线每请求带 8-24 张 base64 图,
#    2026-07-22 端到端实测均延迟 ~21s(且到总并发 64 仍平坦)。凡拿裸 HTTP 小请求外推
#    真实吞吐,都会高估拐点——定并发必须用真实 payload 量(教训见 progress 2026-07-22)。
DEFAULT_MAX_CONCURRENCY = 8

# ── 分类型超时 + 对冲补发(2026-08-15,droid-200 实测定案)──────────────────
# 旧默认 600s(llm 1800s)的代价:6 次失败全是超时、其中 5 次恰好耗时 600.1s
# ——卡在超时线上白等;打分那步本可 18.1 分钟干完,被拖到 23.3 分钟。而各类正常
# 调用 p95 都在 60s 内(probe 51.7 / endstate 47.0 / arbitration 57.5),llm 长
# 输入体质特殊(p99 139.9s)单独给 120s。慢调用不集中在某一批(按分钟稳定在
# 1.6%-7%)⇒ 是服务端排队的随机抖动,补发大概率落进快的那堆,这是对冲的立论。
DEFAULT_TIMEOUTS_S = {"probe": 60.0, "endstate": 60.0, "arbitration": 60.0,
                      "caption": 60.0, "llm": 120.0}

#: 首发排队等闸门许可的保险丝(秒)。排队本身**不烧 2T 预算**(方案 A,2026-08-31
#: 用户拍板):正常拥堵该等就等——队深被上游 episode 信号量天然封顶,等待一定
#: 有进展。这根丝只防"闸门泄漏"这类真 bug 造成的无声吊死,不参与正常节流;
#: 按最坏合法队深(episode 并发 × 帧扇出 ÷ 容量 × 单发 2T)再放大数倍取整。
GATE_WAIT_FUSE_S = 1800.0


def timeout_for(kind: str, vlm_cfg: dict | None) -> float:
    """分类型超时:站点配置 checks.task_success.vlm.timeouts_s.<kind> 可覆盖
    (走既有 default.yaml 深合并那条路,不新造读配置机制),没配用出厂默认。"""
    t = ((vlm_cfg or {}).get("timeouts_s") or {}).get(kind)
    return float(t) if t is not None else DEFAULT_TIMEOUTS_S[kind]


# 惰性建真信号量的原因:threading.Semaphore 内含 _thread.lock,cloudpickle 序列化
# 不了 —— 2026-08-18 生产回归实锤:各工厂把裸 Semaphore 关进闭包,闭包一路进了
# task_check 这个 daft UDF,daft 的 check_serializable 直接把完整质检炸死在起跑线
# (只有 --lite 能跑)。此类共享状态要么放模块级(_HTTP_LOCK 那样),要么像这里:
# 实例只携带容量,真锁头等第一次 acquire 才建,__getstate__ 时丢弃。
_GATE_BUILD_LOCK = threading.Lock()    # 模块级,只用于惰性建锁的双检,不进实例状态


class SharedGate:
    """对冲闸门:接口对齐 threading.Semaphore(acquire(timeout=)/release),但可进
    cloudpickle —— 序列化只带容量,真正的 Semaphore 在目标进程首次 acquire 时重建。

    共享语义:同一实例传给多个工厂 = 这些工厂共用一副在飞许可(pickle 的 memo 机制
    保证一次 dumps 里同一实例反序列化后仍是同一实例,共享不因序列化而裂开)。
    ⚠️ 跨进程各自重建各自的信号量,闸门只在进程内生效 —— 与裸 Semaphore 的语义一致
    (它本来就锁不住别的进程),不是退化。容量在构造期定死:每次 run_funnel 按当时
    配置新建实例,同进程内改配置重跑并发度即生效;跑到一半改配置不生效是有意的
    (在飞许可换容量没有安全语义)。
    """

    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))
        self._sem: threading.Semaphore | None = None

    def _semaphore(self) -> threading.Semaphore:
        # 双检惰性建:acquire 可能同时从多条对冲线程进来,不能建出两副许可
        if self._sem is None:
            with _GATE_BUILD_LOCK:
                if self._sem is None:
                    self._sem = threading.Semaphore(self.capacity)
        return self._sem

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        return self._semaphore().acquire(blocking, timeout)

    def release(self) -> None:
        self._semaphore().release()

    def __getstate__(self) -> dict:
        return {"capacity": self.capacity}          # 锁头不过网:目标侧惰性重建

    def __setstate__(self, state: dict) -> None:
        self.capacity = state["capacity"]
        self._sem = None


def hedged_request(send, *, tag: str, timeout_s: float, gate=None):
    """带对冲补发的一次**逻辑调用**。send(hard_timeout_s) -> requests.Response。

    唯一不变量(2026-08-15 用户拍板,好解释也好测):**从首发发出算起,整次
    逻辑调用最多 2×timeout_s 内返回或作废**。时间线:
    - 首发硬超时 = 2T;到超时线 T 它还挂着 → 补发一次(硬超时 = 剩余预算 ≤ T),
      谁先拿到成功响应用谁,不等慢的那个;
    - 首发在超时线前**快速报错**:5xx / 连接类网络错 → 立即**串行**重发一次
      (这不是对冲,是抢救该拿的那票;仍受闸门与 2T 总预算约束);
      4xx 一律不重发(400/401/403/404/429 都是我们自己的问题,429 尤其
      不许重发 —— 服务端明说"你太快了",再发是火上浇油);
    - 常规共至多两发;**两发皆因超时死透 → 再给恰好一次串行重发,带全新
      1×timeout 预算**(2026-08-31 用户拍板;超时路径最坏 = 排队 + 3×timeout。
      立论与对冲相同:超时是服务端长尾抖动,隔一拍重发大概率落进快的那堆;
      只有超时触发,4xx/429 与 5xx/连接错的既有规则一个字不变);
    - 全部没成 → 原样抛异常/交回错误响应,调用方既有 except
      分支照旧走"少一票"(判定逻辑一个字不动:救人一签、杀人双签不变)。

    ⚠️ 如实说明:落败那一发**取消不了**(HTTP 没有取消语义,服务端照样在算),
    本函数只是不再等它;其线程最晚在 2T 预算线上退出,期间继续持有闸门许可
    ——补发多的那一小段时间里有效并发容量会下降,这是**故意的**(在飞请求
    总数绝不翻倍),不是 bug。

    gate = SharedGate(语义即共享信号量),**首发和补发都要拿许可**(许可持有 = 该 HTTP
    请求真在飞)。容量由工厂按自身结构并发传入;⚠️ 容量不许低于该类的结构并发,
    否则等于把原本并行的调用串行化 —— 最容易悄悄拖慢整批的坑。
    **首发许可在调用方线程同步等到手后才起 2T 预算表**(方案 A,2026-08-31):
    排队不烧预算,拥堵时该等就等(队深被上游并发结构封顶,等待必有进展);
    只有 GATE_WAIT_FUSE_S 保险丝防闸门泄漏。补发仍只用剩余预算排队。

    两发跑在**守护线程**上(2026-08-15 主会话实测教训):此前用 per-call
    ThreadPoolExecutor,其工作线程非守护,CPython 在解释器退出时会把它们全部
    join —— 跑批全部干完、报告落盘后,进程还要陪落败那发挂最多 2T(120 秒)才
    退出,任务台只认退出码文件,界面就多显示两分钟「运行中」(正是本项目明令
    禁止的"静默慢步骤")。守护线程在解释器退出时直接丢弃,对已经放弃的那一发
    这正是要的语义(结果本来就不要了);顺带省掉每次逻辑调用建一个线程池的
    开销(4000 次调用 = 4000 个池)。落败发的生命周期从此只由自身硬超时封顶,
    不再牵连进程生命周期。
    """
    import requests as _rq

    call_id = uuid.uuid4().hex[:12]     # 不用进程内自增:rejudge 增量并档会跨进程撞号
    settled = threading.Event()         # 赢家已定 → 还没发出去的补发不再发(纯浪费)
    results: _queue.Queue = _queue.Queue()   # 每发恰好投一条 (attempt_no, "resp"/"exc", 载荷)

    # 方案 A(2026-08-31 用户拍板;修 droid-50 批"排队即弃权"9 条):首发的闸门
    # 许可在**调用方线程同步等到手**,拿到许可才起 2T 预算表——排队不再烧预算,
    # docstring 里"从首发发出算起"的不变量从此与实现一致。旧写法在进门就起表,
    # 拥堵时整段预算耗在队列里,一枪未发即作废,重发机制根本轮不到上场。
    if gate is not None and not gate.acquire(timeout=GATE_WAIT_FUSE_S):
        raise _rq.exceptions.Timeout(
            f"{tag}: 等待并发闸门许可超过保险丝 {GATE_WAIT_FUSE_S:.0f}s(疑似闸门泄漏)")
    deadline = _time.time() + 2.0 * timeout_s

    def _attempt(attempt_no: int, held: bool = False):
        # held=True:首发,许可已在调用方线程拿好;补发仍自行排队,且只许用
        # 剩余预算等——补发是锦上添花,不值得为它等过 2T 死线。
        if not held:
            budget = deadline - _time.time()
            if budget <= 0:
                raise _rq.exceptions.Timeout(f"{tag}: 2×timeout 总预算已耗尽,不再发起")
            if gate is not None and not gate.acquire(timeout=budget):
                raise _rq.exceptions.Timeout(f"{tag}: 等待并发闸门许可超出 2×timeout 总预算")
        try:
            if settled.is_set():
                return None
            hard = deadline - _time.time()
            if hard <= 0:
                raise _rq.exceptions.Timeout(f"{tag}: 2×timeout 总预算已耗尽,不再发起")
            t1 = _time.time()
            ok, kind = False, "connect_error"
            resp = None
            try:
                resp = send(hard)
                ok = bool(resp.ok)
                kind = "" if ok else "http_error"
                return resp
            except Exception as e:
                if isinstance(e, _rq.exceptions.Timeout):
                    kind = "timeout"
                raise
            finally:
                latency_record(tag, _time.time() - t1, ok, started_at=t1,
                               call_id=call_id, attempt=attempt_no, fail_kind=kind)
                usage_note(tag, resp)
        finally:
            if gate is not None:
                gate.release()

    def _timeout_retry(orig_exc):
        """超时死透后的最后一搏(2026-08-31 用户拍板):读超时多半是服务端长尾
        抖动,换个时刻再发大概率落进快的那堆(与对冲同一立论)。给**恰好一次**
        串行重发,带全新 1×timeout 预算 ⇒ 超时路径的最坏时长 = 排队 + 3×timeout。
        只有超时触发:429/4xx 是"我们的问题"绝不重发,5xx/连接错已有自己的
        串行重发,都不走这里。排队照方案 A 不烧预算;拿不到许可或重发再败,
        原样抛回第一具尸体(调用方 except 分支照旧走"少一票")。"""
        nonlocal deadline
        if gate is not None and not gate.acquire(timeout=GATE_WAIT_FUSE_S):
            raise orig_exc
        deadline = _time.time() + timeout_s
        try:
            resp = _attempt(2, held=True)
            if resp is not None:
                return resp
            raise orig_exc
        except Exception:  # noqa: BLE001  重发的死法不重要,交回原始死因
            raise orig_exc

    def _spawn(attempt_no: int, held: bool = False) -> None:
        def _run():
            try:
                results.put((attempt_no, "resp", _attempt(attempt_no, held)))
            except Exception as e:  # noqa: BLE001
                results.put((attempt_no, "exc", e))
        try:
            threading.Thread(target=_run, daemon=True,
                             name=f"vlm-hedge-{tag}-{attempt_no}").start()
        except BaseException:
            if held and gate is not None:   # 线程都没起来,预取的许可要还回去
                gate.release()
            raise

    try:
        _spawn(0, held=True)
        try:
            _no, kind, payload = results.get(timeout=timeout_s)
        except _queue.Empty:
            payload = None              # 超时线上还挂着 → 对冲
        else:
            if kind == "exc":
                if isinstance(payload, _rq.exceptions.Timeout):
                    # 首发早夭于超时:补发已无意义(同一时刻大概率同样命运),
                    # 直接走"隔一拍再来一发"的超时重发
                    return _timeout_retry(payload)
                return _attempt(1)      # 快速网络错(连接拒绝/复位等)→ 立即串行重发
            if payload.ok or payload.status_code < 500:
                return payload          # 成功,或 4xx(429 在内):原样交回,不重发
            resp0 = payload
            try:
                return _attempt(1)      # 5xx → 立即串行重发一次
            except Exception:  # noqa: BLE001
                return resp0            # 重发也没成:保留服务端第一句原话给调用方
        _spawn(1)
        best_resp, last_exc = None, None
        for _ in range(2):              # 两发各投恰一条结果
            try:                        # 双保险超时:两发本就在 deadline 前自我了断
                _no, kind, payload = results.get(
                    timeout=max(0.1, deadline - _time.time() + 5.0))
            except _queue.Empty:
                break
            if kind == "exc":
                last_exc = payload
                continue
            if payload is None:         # 补发被跳过(赢家已定)
                continue
            if payload.ok:
                return payload
            best_resp = payload
        if best_resp is not None:
            return best_resp            # 两发都是错误响应:交回,调用方 raise_for_status 走原路
        if last_exc is not None:
            if isinstance(last_exc, _rq.exceptions.Timeout):
                return _timeout_retry(last_exc)   # 两发皆超时 → 最后一搏
            raise last_exc
        return _timeout_retry(
            _rq.exceptions.Timeout(f"{tag}: 两发均未在 2×timeout 预算内返回"))
    finally:
        settled.set()                   # 守护线程自会在 2T 预算线上退出,不 join 不等


def list_models(endpoint: str, api_key_env: str | None = None,
                timeout_s: float = 5.0) -> list[str]:
    """GET <endpoint>/models → 服务端模型 id 列表(探活+发现一体,失败抛异常)。

    供 `curation backends` 子命令与运维探查用;托管端点(方舟)自动带鉴权头。
    """
    import json
    import urllib.request

    req = urllib.request.Request(endpoint.rstrip("/") + "/models",
                                 headers=auth_headers(api_key_env))
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return [m["id"] for m in json.load(r).get("data", [])]


def resolve_single_model(endpoint: str, api_key_env: str | None = None,
                         timeout_s: float = 10.0) -> str:
    """端点 → 唯一模型名(2026-07-28 同事反馈:单模型服务报模型名是冗余)。

    自托管 vLLM 一服务一模型是常态 → GET /models 恰一个就直接用;
    托管 MaaS(方舟 130+ 模型)必须显式指定 → 报错并列出候选,给出修法。
    """
    try:
        models = list_models(endpoint, api_key_env, timeout_s=timeout_s)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"VLM 模型自动发现失败:{endpoint} 的 /models 不可达"
                         f"({type(e).__name__}: {e})。请确认端点,或显式指定 --vlm-model") from e
    if len(models) == 1:
        return models[0]
    if not models:
        raise ValueError(f"VLM 模型自动发现失败:{endpoint} 返回空模型列表。"
                         "请显式指定 --vlm-model")
    shown = ", ".join(models[:5]) + (f" …(共{len(models)}个)" if len(models) > 5 else "")
    raise ValueError(f"端点 {endpoint} 有多个模型({shown}),无法自动选择。"
                     "请显式指定 --vlm-model <模型名>")


def auth_headers(api_key_env: str | None) -> dict:
    """按环境变量名取 API Key → OpenAI 兼容的 Bearer 头。

    ★ 安全约定:YAML 里只写**环境变量名**(api_key_env),密钥本身只存环境变量/K8s Secret,
    绝不进配置文件与仓库。自托管 vLLM 无需鉴权 → 不配 api_key_env 即返回空头,行为不变。
    """
    if not api_key_env:
        return {}
    key = os.environ.get(api_key_env, "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def strip_reasoning(text: str) -> str:
    """去掉推理模型的思维链(Cosmos-Reason 等输出 <think>…</think> 再给答案)。

    只取最后一个 </think> 之后的部分;没有标签则原样返回。
    """
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1]
    return text


def probe_endpoint(endpoint: str, model: str, *, api_key_env: str | None = None,
                   timeout: float = 8.0) -> tuple[bool, str]:
    """两段式探活,兼容自托管 vLLM 与托管 MaaS(方舟)。

    ① GET /models:vLLM 支持,且能顺带确认模型确实已加载;
    ② 退化为一次最小 chat 请求:托管平台不一定实现 /models(或模型名是推理接入点 ID
       而非列表里的名字)。这一步更硬——它同时验证了"端点可达 + 鉴权有效 + 模型名可用",
       正是我们真正依赖的能力。
    返回 (可用, 说明)。
    """
    import requests

    base = endpoint.rstrip("/")
    headers = auth_headers(api_key_env)
    try:
        r = requests.get(base + "/models", headers=headers, timeout=timeout)
        if r.ok and model.split("/")[-1] in r.text:
            return True, "VLM 端点可用(/models 已列出该模型)"
    except Exception:  # noqa: BLE001
        pass                      # 落到 ② 兜底,不在此判死
    try:
        r = requests.post(base + "/chat/completions", headers=headers, timeout=timeout,
                          json={"model": model, "max_tokens": 1, "temperature": 0.0,
                                "messages": [{"role": "user", "content": "ping"}]})
        if r.ok:
            return True, "VLM 端点可用(chat 探活通过)"
        hint = " —— 检查 api_key_env 指向的环境变量是否已注入" if r.status_code in (401, 403) else ""
        return False, f"VLM 端点探活失败 HTTP {r.status_code}:{r.text[:120]}{hint}"
    except Exception as e:  # noqa: BLE001
        return False, f"VLM 端点不可达({type(e).__name__})"


def vlm_completion_from_config(cfg: dict):
    """Production scorer: continuous multi-camera video -> structured assessment.

    Frame factories above remain available for historical evaluations only.
    An unsupported video endpoint fails explicitly; it never falls back to images.
    """
    from .video_vlm import make_video_assessor

    vlm = cfg["checks"]["task_success"].get("vlm") or {}
    if not vlm.get("endpoint"):
        raise ValueError("配置 checks.task_success.vlm.endpoint 缺失")
    return make_video_assessor(vlm["endpoint"], vlm["model"],
                                     api_key_env=vlm.get("api_key_env"),
                                     thinking=cfg.get("pipeline", {}).get("thinking"),
                                     timeout_s=timeout_for("probe", vlm),
                                     fps=float((vlm.get("video") or {}).get("fps", 5)),
                                     max_in_flight=int(vlm.get("max_concurrency",
                                                               DEFAULT_MAX_CONCURRENCY)))


def camera_check_from_config(cfg: dict):
    """The picture-defect request an episode without a task text gets (D73): the production
    scorer's endpoint, timeout and gate, asking for the cameras' defects alone."""
    from .video_vlm import make_video_assessor

    vlm = cfg["checks"]["task_success"].get("vlm") or {}
    if not vlm.get("endpoint"):
        raise ValueError("配置 checks.task_success.vlm.endpoint 缺失")
    return make_video_assessor(vlm["endpoint"], vlm["model"], cameras_only=True,
                               api_key_env=vlm.get("api_key_env"),
                               thinking=cfg.get("pipeline", {}).get("thinking"),
                               timeout_s=timeout_for("probe", vlm),
                               fps=float((vlm.get("video") or {}).get("fps", 5)),
                               max_in_flight=int(vlm.get("max_concurrency",
                                                         DEFAULT_MAX_CONCURRENCY)))


# ══ 取证仲裁链的提示词与工厂(2026-08-13 定稿)═══════════════════════════════
# 提示词全部原样承接实验版 arb_bench_b_v2(droid-200 真值上校准过的措辞):
# - 问题生成三军规(禁加强词/严格度不超指令字面/gripper 不当 target):删一条冤杀就涨;
# - 判官讲理标准三句(达成即YES/看不见答UNCLEAR绝不猜NO/空间短语按说话人本意):
#   实验里把冤杀 52→17。改措辞前先回 droid-200 重测,不许手感调优。


