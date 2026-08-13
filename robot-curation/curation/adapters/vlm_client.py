"""VLM 客户端适配器:openai 兼容端点(vLLM serve)→ vlm_completion 注入函数。

★ 换模型硬需求(用户 2026-07-02):模型名/端点只出现在 pipeline YAML 的 vlm: 一处,
本模块从配置构造客户端,core/checks/task_success.py 对模型零感知。
prompt 设计借 OpenGVL:单帧提问完成度(VOC 的打乱在 core 层做,这里只管"一帧一问")。
"""
from __future__ import annotations

import base64
import os
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

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
_LAT_ROWS: list = []                   # [(tag, seconds, ok, started_at)],latency_reset 清零


def latency_record(tag: str, dt: float, ok: bool = True,
                   started_at: float | None = None) -> None:
    """记一次调用。started_at = 请求发出时刻(time.time() epoch 秒)。

    传 None 表示"没记时刻"(老代码路径/合成数据)——该桶就没有墙钟,
    上层如实降级,**绝不**拿次数×均值顶替。
    """
    with _HTTP_LOCK:
        _LAT_ROWS.append((tag, dt, ok, started_at))
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

    wall_s(墙钟)= 该类调用**忙碌区间的并集长度**:把每次调用的 [发出, 返回]
    区间合并去重后求总长。⚠️ 不能用"首发→末返"的跨度——同类调用分多个阶段跑时
    (caption 在开头补标、中段画像各一波),中间隔着别的阶段,空档会把墙钟灌水
    到离谱(2026-08-06 droid-30 实锤:caption 实跑 90s 被算成 988s,条形图把它
    画成头号瓶颈)。并集口径下连续阶段与旧口径一致,分段阶段只算真在飞的时长。
    失败的调用**也算**(它同样占了那段时间)。
    该桶一条 started_at 都没有(三列老 CSV / 老代码)→ 不给 wall_s,上层降级。
    """
    rows = latency_rows() if rows is None else list(rows)
    out = {}
    for tag in sorted({r[0] for r in rows}):
        mine = [r for r in rows if r[0] == tag]
        oks = sorted(r[1] for r in mine if r[2])
        errs = sum(1 for r in mine if not r[2])
        entry = {"n": len(oks), "errors": errs}
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

    兼容 2026-07-30 之前的**三列**老 CSV(call_type,seconds,ok):没有
    started_at 列 → 该行时刻为 None → 复算出来的档案没有墙钟。
    """
    import csv as _csv
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            st = (r.get("started_at") or "").strip()
            rows.append((r["call_type"], float(r["seconds"]),
                         bool(int(r["ok"])), float(st) if st else None))
    return rows


def http_stats() -> tuple[int, float]:
    """返回 (请求数, 请求耗时总和秒)。"""
    with _HTTP_LOCK:
        return _HTTP_STATS["n"], _HTTP_STATS["secs"]

# 并发请求默认值(2026-07-20 实测方舟:并发1→0.10 req/s、8→0.49、16→0.96,零 429、延迟不涨
# ≈7.8s/请求恒定 ⇒ 瓶颈是串行等待而非服务端限流)。默认 8 = n_probe 常用值,一条 episode
# 的 VOC 问询一轮打完;**保持可配置**:上 Ray 后每 worker 调小,总并发=worker数×本值,
# 一个旋钮压在服务端配额下(见 progress backlog)。
# ⚠️ 上面那个 7.8s 是**小 payload** 下的数;真实流水线每请求带 8-24 张 base64 图,
#    2026-07-22 端到端实测均延迟 ~21s(且到总并发 64 仍平坦)。凡拿裸 HTTP 小请求外推
#    真实吞吐,都会高估拐点——定并发必须用真实 payload 量(教训见 progress 2026-07-22)。
DEFAULT_MAX_CONCURRENCY = 8


def _map_concurrent(fn, items: list, max_concurrency: int) -> list:
    """并发映射,**保序返回**。

    ⚠️ 顺序是正确性要求而非性能细节:VOC 抗幻觉靠"预测完成度 vs 真实时序"的相关性,
    结果错位整个判定就废了。ThreadPoolExecutor.map 保证按输入顺序返回。
    max_concurrency<=1 时退化为串行(便于排障/对照)。
    """
    items = list(items)
    if max_concurrency <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(items))) as ex:
        return list(ex.map(fn, items))


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

# ⚠️ 协议演化(2026-07-02 实测,详见 spikes/vlm_ab_report.md):
# v1 单帧无参考:模型一律回"50"(无参照物,敷衍是理性答案)→ 废弃;
# v2 多图一次问(无上下文):Qwen3-VL 位置偏置幻觉——无视画面按图片顺序输出递增数列
#    (VOC 正确拦截,但可判率掉到 5%)→ 废弃;
# v3 锚定单查询:起始参考帧+单查询帧,免疫位置偏置(Qwen3 rho ~0→0.77-0.96),
#    但没解决绝对定标(干净/截断终值分布重叠);
# v4 few-shot 多图(忠实 openGVL):起始帧+标注上下文+8 张打乱评测帧一次问 →
#    实测(3 模型)仍崩:模型跟踪不了"哪张是哪张",Qwen2.5 18/20 全同预测,VOC 中位≈0;
# v5 few-shot + 锚定单查询:每请求 = 标注上下文(教标尺)+ 起始帧 + 1 张待评帧
#    (无跟踪/位置问题)。few-shot 解决绝对定标,单查询解决多图跟踪,各治各的病。
#    上下文在客户端构造期绑定 → 注入接口不变,core/funnel 零改动。
# v6 物证打分(2026-08-04,105 条人工真值消融定稿):v5 问"How complete"是**瞬时
#    状态**问题——机械臂做完撤手,末帧分数就掉,真值曲线天然不单调,VOC 的单调性
#    前提被打破(撤手型成功 VOC 中位 0.587 vs 单调型 0.932,29% 的好数据被冤枉)。
#    改问**物证**:只按物体/场景状态打分,不看机械臂在不在动作——做了就留痕的任务
#    真值由此恢复单调,"手臂移开"不再掉分。两面都写死:撤手不扣分/空挥不加分。
ANCHORED_PROMPT = (
    "Task: {instruction}\n"
    "Image 1 shows the START of a robot episode (reference: nothing accomplished yet).\n"
    "Image 2 is ONE frame from the SAME episode at an unknown time.\n"
    "Score how far the task has physically progressed in Image 2, from 0 to 100, "
    "judging ONLY by the state of the objects and the scene - not by what the robot "
    "arm is doing.\n"
    "- 0 = the objects look as they do in Image 1 (starting configuration).\n"
    "- 100 = the objects are in the goal configuration described by the task.\n"
    "If the objects are already in the goal configuration, answer 100 even if the arm "
    "has moved away, is idle, or is out of view. If the objects still look like "
    "Image 1, answer 0 even if the arm is moving.\n"
    "Answer ONLY an integer from 0 to 100."
)

FEWSHOT_SYSTEM = (
    "You are an expert roboticist tasked to predict task completion percentages for frames "
    "of a robot performing the task: {instruction}. Completion is between 0 and 100, where "
    "100 is full task completion. The frames may be in random order; reason about each frame "
    "independently when estimating completion."          # openGVL get_prompt 同款措辞
)
FEWSHOT_ASK = (
    "Now estimate the task completion percentage of EACH of the {k} frames above "
    "(after the labeled examples). Answer with exactly {k} comma-separated integers "
    "in the same order as given, nothing else."
)


def parse_completion(text: str) -> float:
    """模型回答 → 完成度 0-1。找第一个数;>1 视为百分制;解析失败抛 ValueError。"""
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        raise ValueError(f"VLM 回答里没有数字: {text!r}")
    v = float(m.group())
    if v > 1.0:
        v = v / 100.0
    return float(np.clip(v, 0.0, 1.0))


def strip_reasoning(text: str) -> str:
    """去掉推理模型的思维链(Cosmos-Reason 等输出 <think>…</think> 再给答案)。

    只取最后一个 </think> 之后的部分;没有标签则原样返回。
    """
    if "</think>" in text:
        return text.rsplit("</think>", 1)[1]
    return text


def parse_completion_list(text: str, k: int) -> list[float]:
    """模型回答 → K 个完成度(0-1)。数量不符抛 ValueError(调用方转不可判)。

    先剥思维链(CoT 正文里满是数字,不剥会解析到推理过程);答案段若数字多于 K,
    取最后 K 个(模型爱复述题干"the 8 frames"之类)。
    """
    answer = strip_reasoning(text)
    nums = re.findall(r"-?\d+(?:\.\d+)?", answer)
    if len(nums) < k:
        raise ValueError(f"期望 {k} 个数,got {len(nums)}: {answer[-200:]!r}")
    vals = [float(x) for x in nums[-k:]]
    if any(v > 1.0 for v in vals):
        vals = [v / 100.0 for v in vals]
    return [float(np.clip(v, 0.0, 1.0)) for v in vals]


def _frame_to_data_uri(frame: np.ndarray, quality: int = 85) -> str:
    import cv2

    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("帧 JPEG 编码失败")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


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


def make_vlm_completion(
    endpoint: str,
    model: str,
    *,
    context: tuple[list[np.ndarray], list[float]] | None = None,  # v4:上下文帧+完成度标注(0-1)
    timeout_s: float = 600.0,
    temperature: float = 0.0,
    max_tokens: int = 2048,       # 推理模型(Cosmos-Reason)的 CoT 需要余量
    api_key_env: str | None = None,   # 托管端点(方舟 MaaS)鉴权;自托管 vLLM 留空
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,   # 逐帧问询的并发度;1=串行
):
    """构造批式 vlm(reference, shuffled_frames, instruction) -> list[float](注入给 task_success)。

    context 给出 → v4 few-shot 协议(单会话多图,忠实 openGVL);否则 → v3 锚定单查询。
    上下文在构造期绑定 → 注入接口不变,core/funnel 零改动。
    endpoint: openai 兼容根路径(如 http://localhost:8000/v1)。
    """
    import requests

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def _post(content: list) -> str:
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": content}]}
        _t = _time.time()
        _ok = False
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
            _ok = resp.ok
        finally:
            latency_record("probe", _time.time() - _t, _ok, started_at=_t)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _img(frame: np.ndarray) -> dict:
        return {"type": "image_url", "image_url": {"url": _frame_to_data_uri(frame)}}

    def vlm_v3(reference, shuffled_frames, instruction) -> list[float]:
        text = ANCHORED_PROMPT.format(
            instruction=instruction or "the robot manipulation task")
        ref_img = _img(reference)                     # 参考帧编码一次,各请求复用

        def ask(f):                                   # 锚定单查询:每帧一次请求
            ans = _post([{"type": "text", "text": text}, ref_img, _img(f)])
            return parse_completion(strip_reasoning(ans))

        return _map_concurrent(ask, shuffled_frames, max_concurrency)   # 保序

    def vlm_v5(reference, shuffled_frames, instruction) -> list[float]:
        ctx_frames, ctx_rates = context
        head = [{"type": "text", "text": FEWSHOT_SYSTEM.format(
            instruction=instruction or "the robot manipulation task")},
            {"type": "text", "text": "Labeled example frames (from a similar episode):"}]
        for f, r in zip(ctx_frames, ctx_rates):       # few-shot:教绝对标尺
            head.append(_img(f))
            head.append({"type": "text", "text": f"Task completion: {int(round(r * 100))}%"})
        head.append({"type": "text", "text": "Initial frame of the CURRENT episode (0% complete):"})
        head.append(_img(reference))

        def ask(f):                                   # 单查询:每请求只评 1 帧,无跟踪问题
            content = list(head)                      # 每线程独立副本,勿共享可变列表
            content.append({"type": "text",
                            "text": "Now estimate the task completion percentage of THIS frame "
                                    "of the current episode. Answer ONLY an integer 0-100:"})
            content.append(_img(f))
            return parse_completion(strip_reasoning(_post(content)))

        return _map_concurrent(ask, shuffled_frames, max_concurrency)   # 保序

    return vlm_v5 if context is not None else vlm_v3


def build_linear_context(frames: list[np.ndarray], n: int = 8,
                         shuffle_seed: int = 1) -> tuple[list[np.ndarray], list[float]]:
    """从一条已知成功的干净 episode 造 few-shot 上下文:均匀抽 n 帧,
    完成度=线性近似(openGVL 的 approx_completion_rates 同款假设),打乱后连标注一起给
    (示范"乱序也要按内容判")。"""
    idx = np.unique(np.linspace(0, len(frames) - 1, n, dtype=int))
    rates = idx / max(len(frames) - 1, 1)
    order = np.random.default_rng(shuffle_seed).permutation(len(idx))
    return [frames[idx[j]] for j in order], [float(rates[j]) for j in order]


# ── v7.2 多视角协议(2026-08-04,105 条人工真值 × 三模型小考定稿)──────────
# 打分层=带标签联合:同一时刻各相机帧一次给齐(带相机身份标签),模型逐时刻融合
#   感知——盲区是逐时刻变化的,"信看得清的那路"只能由模型在当下决定(droid ep19:
#   单路外1 VOC -0.11 逐帧掷硬币,外2 +0.76,联合 final 1.0 直接判对)。
# v5 铁律不破:每请求仍只出一个数(k 张**同时刻**查询帧 ≠ v4 的 8 张异时刻帧,
#   无跟踪问题)。
# v7.4 注意力引导(2026-08-06,ep148 消融 + A/B + 105 全量验证):细小物体在自由
#   观察下被夹爪连杆伪装+先验抢注意力("看得见但没看");引导句后微小物证族 4/6
#   走出人工队列(ep148 voc 0.52→0.98),头条 87/15/0 与 3/3 拦截不变,冤杀 0。
JOINT_SCORE_PROMPT = (
    "Task: {instruction}\n"
    "Images 1-{k} show the START of a robot episode, one image per camera, in this "
    "order: {camlist}. Nothing is accomplished yet at the start.\n"
    "Images {k1}-{k2} show ONE SAME later moment of the SAME episode, from the same "
    "cameras in the same order.\n"
    "Some views may be occluded or unhelpful; rely on the views where the objects "
    "are clearly visible.\n"
    "If the task involves a small or thin object (a band, string, straw, paper, "
    "small part), look for it between the gripper fingers and at the target "
    "location before scoring - thin objects are easy to mistake for the "
    "gripper's own rods.\n"
    "Score how far the task has physically progressed at that moment, from 0 to 100, "
    "judging ONLY by the state of the objects and the scene - not by what the robot "
    "arm is doing.\n"
    "- 0 = the objects are still in their starting configuration.\n"
    "- 100 = the objects are in the goal configuration described by the task.\n"
    "If the objects are already in the goal configuration, answer 100 even if the "
    "arm has moved away, is idle, or is out of view.\n"
    "Answer ONLY an integer from 0 to 100."
)


def make_multiview_completion(
    endpoint: str,
    model: str,
    *,
    timeout_s: float = 600.0,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    api_key_env: str | None = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
):
    """多视角联合打分工厂。**帧 = [(相机名, 图), ...]**(同一时刻各相机,标签随数据走)。

    注入接口不变:vlm(reference, shuffled_frames, instruction) -> list[float],
    只是 reference 与 shuffled_frames 的元素都是 [(name, img), ...]。
    单相机数据集 = 单元素列表,自动退化为"单视角带标签"提问,零分支。
    """
    import requests

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def _post(content):
        payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": content}]}
        _t = _time.time()
        _ok = False
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
            _ok = resp.ok
        finally:
            latency_record("probe", _time.time() - _t, _ok, started_at=_t)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _img(frame) -> dict:
        return {"type": "image_url", "image_url": {"url": _frame_to_data_uri(frame)}}

    def vlm_joint(reference, shuffled_frames, instruction) -> list[float]:
        names = [n for n, _ in reference]
        letters = [chr(ord("A") + i) for i in range(len(names))]
        camlist = ", ".join(f"image {i+1} = camera {letters[i]} ({n})"
                            for i, n in enumerate(names))
        k = len(names)
        text = JOINT_SCORE_PROMPT.format(
            instruction=instruction or "the robot manipulation task",
            k=k, camlist=camlist, k1=k + 1, k2=2 * k)
        refs = [_img(f) for _, f in reference]        # 参考帧编码一次,各请求复用

        def ask(mvframe):                             # 每请求 = 一个时刻,一个数
            content = [{"type": "text", "text": text}] + refs + \
                      [_img(f) for _, f in mvframe]
            return parse_completion(strip_reasoning(_post(content)))

        return _map_concurrent(ask, shuffled_frames, max_concurrency)   # 保序

    return vlm_joint


# 复核层=逐机位独立双问 + "unclear"弃权票:被挡的机位可以诚实退场("看不见"≠
# "没做成"),票数只来自自称看得清的证人;双问矛盾 = 该机位证词不采信。
Q_DONE_MV = (
    "Task: {task}\n"
    "All images are from ONE camera: {cam_label}.\n"
    "Images 1-{n0}: the EARLIER part of the episode, in chronological order. "
    "Images {n1}-{n}: the LATER part, in chronological order.\n"
    "Did the robot COMPLETE the task at any point? Judge by the OBJECTS, not the "
    "arm's motions: the task counts as completed only if the target object actually "
    "ended up as the task describes. A grasping or placing motion does not count if "
    "the object was never actually moved.\n"
    "If this camera's view does not show the target object clearly enough to tell, "
    "answer \"unclear\" - do not guess.\n"
    "Answer ONLY one word: yes, no, or unclear."
)
Q_FAILED_MV = (
    "Task: {task}\n"
    "All images are from ONE camera: {cam_label}.\n"
    "Images 1-{n0} are from the earlier part, images {n1}-{n} from the later part, "
    "both in chronological order.\n"
    "Did the robot FAIL to complete the task (i.e. the target object never actually "
    "ended up as the task describes)? Look closely at the target object across the "
    "frames; ignore whether the arm's motions looked correct.\n"
    "If this camera's view does not show the target object clearly enough to tell, "
    "answer \"unclear\" - do not guess.\n"
    "Answer ONLY one word: yes, no, or unclear."
)


def parse_vote_word(text: str) -> str:
    """模型回答 → yes/no/unclear;识别不出返回 '?'(调用方按矛盾处理)。"""
    t = strip_reasoning(text).strip().lower()
    for w in ("unclear", "yes", "no"):     # unclear 先匹配,避免与 no 的子串歧义
        if t.startswith(w):
            return w
    if "unclear" in t[:40]:
        return "unclear"
    if "yes" in t[:8]:
        return "yes"
    if "no" in t[:8]:
        return "no"
    return "?"


def make_endstate_voter(endpoint: str, model: str, timeout_s: float = 600.0,
                        api_key_env: str | None = None):
    """逐机位复核投票器(v7.2,取代旧 make_endstate_judge 的多机位混问——旧法
    24 张图一锅烩一个答案,好机位的清晰证据被烂机位稀释,droid ep19 实锤)。

    voter(start_frames, end_frames, cam_label, instruction) ->
        'yes' / 'no' / 'unclear' / 'contradictory' / 'unavail'
    单机位一票;票的合成(cam_vote)与汇总(tally)在 core,本函数只管问与解析。
    """
    import requests

    from ..core.checks.task_success import cam_vote

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def _ask(text, imgs):
        content = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": _frame_to_data_uri(f)}}
            for f in imgs]
        _t = _time.time()
        _ok = False
        try:
            r = requests.post(url, json={"model": model, "temperature": 0.0,
                                         "max_tokens": 2048,
                                         "messages": [{"role": "user", "content": content}]},
                              headers=headers, timeout=timeout_s)
            _ok = r.ok
        finally:
            latency_record("endstate", _time.time() - _t, _ok, started_at=_t)
        r.raise_for_status()
        return parse_vote_word(r.json()["choices"][0]["message"]["content"])

    def voter(start_frames, end_frames, cam_label, instruction):
        task = instruction or "the robot manipulation task"
        imgs = list(start_frames) + list(end_frames)
        n0 = len(start_frames)
        fmt = dict(task=task, cam_label=cam_label, n0=n0, n1=n0 + 1, n=len(imgs))
        try:                                  # 双问并发(互不依赖)
            a, b = _map_concurrent(lambda q: _ask(q.format(**fmt), imgs),
                                   [Q_DONE_MV, Q_FAILED_MV], 2)
        except Exception:  # noqa: BLE001
            return "unavail"
        return cam_vote(a, b)

    return voter


def vlm_completion_from_config(cfg: dict):
    """pipeline YAML 的 checks.task_success.vlm 段 → 注入函数(模型只在配置一处)。

    v7.2 起返回**多视角联合**打分器(帧 = [(相机名, 图), ...],funnel 按此组装;
    单相机数据集自动退化,零分支)。单视角 make_vlm_completion 保留给探针/考卷。"""
    vlm = cfg["checks"]["task_success"].get("vlm") or {}
    if not vlm.get("endpoint"):
        raise ValueError("配置 checks.task_success.vlm.endpoint 缺失")
    return make_multiview_completion(vlm["endpoint"], vlm["model"],
                                     api_key_env=vlm.get("api_key_env"),
                                     max_concurrency=int(vlm.get("max_concurrency",
                                                                 DEFAULT_MAX_CONCURRENCY)))


# ══ 取证仲裁链的提示词与工厂(2026-08-13 定稿)═══════════════════════════════
# 提示词全部原样承接实验版 arb_bench_b_v2(droid-200 真值上校准过的措辞):
# - 问题生成三军规(禁加强词/严格度不超指令字面/gripper 不当 target):删一条冤杀就涨;
# - 判官讲理标准三句(达成即YES/看不见答UNCLEAR绝不猜NO/空间短语按说话人本意):
#   实验里把冤杀 52→17。改措辞前先回 droid-200 重测,不许手感调优。

ARB_QUESTION_PROMPT = """A robot was instructed to perform this manipulation task:
"{instruction}"

You are preparing a verification checklist for whether the task succeeded.
Output strict JSON with exactly these keys:
- "task_type": "transient" if the task's goal is only to grasp / pick up / lift
  / hold an object (no placement and no lasting change to the scene);
  otherwise "persistent".
- "target_location": short English name of the physical place that decides
  success (a fixture or container someone could point at in a photo). For a
  transient task use the object's surroundings.
- "target_visual": one short phrase describing what target_location looks like
  in a photo (color / shape / distinctive parts), to help locate it.
- "object": short English name of the object being manipulated.
- "verify_question": ONE yes/no question that confirms the task succeeded;
  mention both the object and the target location.

Rules for verify_question:
- Its strictness must NOT exceed the instruction's literal wording. Never add
  intensifiers such as "all", "fully", "completely", "securely", "properly",
  "firmly", "neatly", "exactly" unless that word is in the instruction itself.
- For a persistent task, the question is about the FINAL scene and must remain
  true after the robot releases the object and retreats. Never ask whether the
  object is in the gripper.
- For a transient task, ask whether the object is held in the gripper / lifted
  off its surface (it will be judged at the grasp moment, not the final frame).
Output only the JSON, nothing else."""

ARB_GROUND_PROMPT = """In this image, locate two things: (1) the ENTIRE {target} unit
(including its rim and immediate surroundings; looks like: {visual}),
(2) the {obj}. For each, answer 'NOT VISIBLE' or give a bounding box.
Reply in this exact format (thousandths of image width/height, image is {w}x{h}):
TARGET: <bbox>x0 y0 x1 y1</bbox> or NOT VISIBLE
OBJECT: <bbox>x0 y0 x1 y1</bbox> or NOT VISIBLE"""

ARB_JUDGE_PROMPT = """You are verifying whether a robot manipulation task succeeded.
Step 1: briefly describe what you see in and around the {target} area — list the
objects, their spatial relations, and anything covering, lining or partially
hiding the {target}.
Step 2: answer this question: {question}

Judging standards:
- Judge like a reasonable human: if the instruction's evident goal is achieved,
  answer YES even if the execution is imperfect (slightly misplaced, minor
  leftovers, untidy result).
- Interpret spatial phrases the way the instruction's author would. Example: a
  tap "at the center of the sink" means its spout ends up over the middle of
  the sink, not inside the basin.
- If the region that decides the answer is not visible (inside a container,
  occluded, out of frame), answer UNCLEAR — never guess NO.
Finish with a single line: VERDICT: YES or VERDICT: NO or VERDICT: UNCLEAR."""

ARB_SAME_INTENT_PROMPT = """Two descriptions of what a robot was asked to do:
A: "{a}"
B: "{b}"
Do they describe the SAME task (same action on the same object, wording
differences aside)? Answer with exactly one word: SAME or DIFFERENT."""

#: 取证场景 → 判官开场白(core 只传语义化 scene 名,措辞集中在本处)
_ARB_SCENE_INTRO = {
    "exterior_final": ("Image 1 is the full scene at the FINAL state of the "
                       "episode; image 2 is a zoomed view of the {target} area "
                       "from the same frame. "),
    "exterior_post_grasp": ("Image 1 is the full scene shortly after the robot "
                            "grasped the object; image 2 is a zoomed view of the "
                            "{target} area from the same frame. "),
    "wrist_grasp": ("These are wrist-camera frames around the moment the robot "
                    "gripper GRASPED an object, in chronological order. "),
    "wrist_release": ("These are wrist-camera frames around the moment the robot "
                      "gripper RELEASED an object, in chronological order. "),
}


def parse_arbitration_boxes(text: str, w: int, h: int) -> list:
    """TARGET/OBJECT 两行 → 像素框列表。坐标制自适应(0-1 归一化 / 千分制):
    模型并不总按提示词的千分制回答,数值 ≤1.5 视为归一化坐标。太小的框(<3px)
    是解析噪声,丢弃。"""
    boxes = []
    for line in str(text).splitlines():
        if "NOT" in line.upper():
            continue
        nums = re.findall(r"-?\d+\.?\d*", line)
        if len(nums) < 4:
            continue
        bx = [float(x) for x in nums[-4:]]
        scale = 1.0 if max(bx) <= 1.5 else 1000.0
        x0, y0 = bx[0] / scale * w, bx[1] / scale * h
        x1, y1 = bx[2] / scale * w, bx[3] / scale * h
        if x1 - x0 >= 3 and y1 - y0 >= 3:
            boxes.append((x0, y0, x1, y1))
    return boxes


def parse_evidence_verdict(text: str) -> str:
    """判官回答 → yes/no/unclear。找不到 VERDICT 行=没按协议答,按 unclear 处理
    (看不懂的证词不能当实票)。"""
    m = re.search(r"VERDICT:\s*(YES|NO|UNCLEAR)", strip_reasoning(str(text)), re.I)
    return m.group(1).lower() if m else "unclear"


def _make_arb_post(endpoint: str, model: str, timeout_s: float,
                   api_key_env: str | None, max_tokens: int):
    """仲裁链共用的一次调用闭包(文本+图,延时记入 arbitration 桶)。"""
    import requests

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def _post(text: str, frames: list = ()) -> str:
        content = [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": _frame_to_data_uri(f)}}
            for f in frames]
        _t = _time.time()
        _ok = False
        try:
            r = requests.post(url, json={"model": model, "temperature": 0.0,
                                         "max_tokens": max_tokens,
                                         "messages": [{"role": "user",
                                                       "content": content}]},
                              headers=headers, timeout=timeout_s)
            _ok = r.ok
        finally:
            latency_record("arbitration", _time.time() - _t, _ok, started_at=_t)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    return _post


def make_question_writer(endpoint: str, model: str, timeout_s: float = 600.0,
                         api_key_env: str | None = None):
    """问题生成器工厂:writer(intent) -> 校验过的 spec dict(缺关键键即抛,
    调用方把整条意图链转弃权——半张检查单没法取证,不硬凑)。"""

    _post = _make_arb_post(endpoint, model, timeout_s, api_key_env, max_tokens=400)

    def writer(intent: str) -> dict:
        import json as _json

        ans = strip_reasoning(_post(ARB_QUESTION_PROMPT.format(instruction=intent)))
        ans = re.sub(r"^```(json)?|```$", "", ans.strip(), flags=re.M).strip()
        spec = _json.loads(ans)
        for key in ("task_type", "target_location", "verify_question"):
            if not str(spec.get(key) or "").strip():
                raise ValueError(f"问题生成缺关键字段 {key!r}: {ans[:200]!r}")
        return spec

    return writer


def make_grounder(endpoint: str, model: str, timeout_s: float = 600.0,
                  api_key_env: str | None = None):
    """定位器工厂:grounder(img, target, visual, obj) -> 像素框列表(空=不可见)。"""

    _post = _make_arb_post(endpoint, model, timeout_s, api_key_env, max_tokens=400)

    def grounder(img, target: str, visual: str, obj: str) -> list:
        arr = np.asarray(img)
        h, w = arr.shape[:2]
        ans = _post(ARB_GROUND_PROMPT.format(target=target, visual=visual,
                                             obj=obj, w=w, h=h), [arr])
        return parse_arbitration_boxes(strip_reasoning(ans), w, h)

    return grounder


def make_evidence_judge(endpoint: str, model: str, timeout_s: float = 600.0,
                        api_key_env: str | None = None):
    """判官工厂:judge(imgs, *, target, question, scene) -> 'yes'/'no'/'unclear'。

    一次调用一票;三票多数在 core(_arb_line_verdict)。scene 是语义化场景名
    (exterior_final / exterior_post_grasp / wrist_grasp / wrist_release),
    对应的开场白措辞只在本模块。"""

    _post = _make_arb_post(endpoint, model, timeout_s, api_key_env, max_tokens=1024)

    def judge(imgs: list, *, target: str, question: str, scene: str) -> str:
        intro = _ARB_SCENE_INTRO.get(scene, "")
        text = (intro.format(target=target)
                + ARB_JUDGE_PROMPT.format(target=target, question=question))
        return parse_evidence_verdict(_post(text, list(imgs)))

    return judge


def make_intent_comparer(endpoint: str, model: str, timeout_s: float = 600.0,
                         api_key_env: str | None = None):
    """意图语义比对工厂:same(a, b) -> True=同一任务(意图打架护栏用)。

    既非 SAME 也非 DIFFERENT 的回答直接抛 —— 调用方按打架从严处理,
    比"当作相同"少一道护栏安全。"""

    _post = _make_arb_post(endpoint, model, timeout_s, api_key_env, max_tokens=8)

    def same(a: str, b: str) -> bool:
        ans = strip_reasoning(_post(ARB_SAME_INTENT_PROMPT.format(a=a, b=b))).upper()
        if "DIFFERENT" in ans:
            return False
        if "SAME" in ans:
            return True
        raise ValueError(f"语义比对回答无法解析: {ans[:80]!r}")

    return same


def make_llm_ask(endpoint: str, model: str, timeout_s: float = 1800.0,
                 max_tokens: int = 8192, api_key_env: str | None = None):
    """纯文本 LLM 调用工厂(M7 taxonomy/audit 用;同一端点同一模型,配置一处)。"""
    import requests

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def llm_ask(prompt_text: str) -> str:
        _t = _time.time()
        _ok = False
        try:
            r = requests.post(url, json={"model": model, "temperature": 0.0,
                                         "max_tokens": max_tokens,
                                         "messages": [{"role": "user", "content": prompt_text}]},
                              headers=headers, timeout=timeout_s)
            _ok = r.ok
        finally:
            latency_record("llm", _time.time() - _t, _ok, started_at=_t)
        r.raise_for_status()
        return strip_reasoning(r.json()["choices"][0]["message"]["content"])

    return llm_ask
