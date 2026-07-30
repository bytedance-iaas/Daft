"""episode 三态时间线装配(D2,2026-07-28 用户设计:每条一根横向彩条)。

三态口径沿用 stuck 体系(2026-07-15 定):stuck=指令在推而不动(定罪)、
idle=无指令静止(头尾空闲 + 事件包络内的静止段)、normal=在干活。
数据来源全是管道已算出的现成值:时长(帧数/fps)、运动检查的头尾空闲秒数、
stuck 事件的逐段 timeline——本模块只做区间合并,纯函数零 IO。

已知近似(文档口径):事件包络外的"中段零星 idle"(idle_mid)只有总秒数没有
位置,不上色条(仍算 normal),总秒数在明细表可查。
"""
from __future__ import annotations


def build_episode_timeline(duration_s: float, idle_head_s: float = 0.0,
                           idle_tail_s: float = 0.0,
                           event_segments: list | None = None,
                           tail_gap_s: float = 0.0) -> list:
    """→ [{"start_s","end_s","state"}],首尾相接铺满 [0, duration]。

    合并规则:stuck 段优先于 idle 段(重叠时定罪压过停手);其余为 normal;
    相邻同态段自动缝合;所有边界保留 2 位小数。

    tail_gap_s(2026-07-29 用户定,建议传 1/fps):**尾帧无证据区延续前一段**。
    运动判定做在帧**间隔**上,T 帧只有 T-1 个间隔,而时长是 T/fps——末尾整整
    一帧没有任何间隔证据,原约定填 normal,于是 idle/stuck 一直顶到结尾的
    episode 会在末端冒出一小节青绿。bridge ep167(36 帧@5fps)最明显:
    idle 4.8-7.0s 而时长 7.2s,尾巴 0.2s 画成 normal,边界数字 7 和 7.2 还挤在
    一起;droid 15fps 只有 0.067s,一直没暴露而已。此处不是"判它在动",是
    **没有证据**,延续前一段比默认 normal 诚实。
    只吃"贴着结尾且不超过一帧"的零头:真实的长 normal 尾巴(有帧间隔证据
    支撑,长度必 >1 帧)原样保留;头部不用处理(首帧有第一个间隔的证据)。
    """
    dur = round(max(0.0, float(duration_s or 0)), 2)
    if dur <= 0:
        return []
    marks: list = []                          # (start, end, state) 候选区间
    if idle_head_s and idle_head_s > 0:
        marks.append((0.0, min(float(idle_head_s), dur), "idle"))
    if idle_tail_s and idle_tail_s > 0:
        marks.append((max(0.0, dur - float(idle_tail_s)), dur, "idle"))
    for seg in event_segments or []:
        s, t = float(seg.get("start_s", 0)), float(seg.get("end_s", 0))
        st = seg.get("state")
        if st in ("stuck", "idle") and t > s:
            marks.append((max(0.0, s), min(t, dur), st))

    # 逐点扫描:0.01s 栅格太贵,改事件边界扫描——收集全部边界点,区间内状态取
    # "stuck > idle > normal" 的最高优先级
    bounds = sorted({0.0, dur, *(round(x, 2) for m in marks for x in m[:2])})
    out: list = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a < 0.005:
            continue
        mid = (a + b) / 2
        state = "normal"
        for s, t, st in marks:
            if s <= mid < t:
                if st == "stuck":
                    state = "stuck"
                    break
                state = "idle"
        if out and out[-1]["state"] == state:
            out[-1]["end_s"] = round(b, 2)     # 缝合同态相邻段
        else:
            out.append({"start_s": round(a, 2), "end_s": round(b, 2), "state": state})
    # 尾帧无证据区:并入前一段(0.005=2 位小数的舍入容差,别让 7.2-7.0 的浮点
    # 尾数 0.19999… 把恰好一帧的零头判成"超过一帧")
    if (tail_gap_s and tail_gap_s > 0 and len(out) >= 2
            and out[-1]["state"] == "normal"
            and (out[-1]["end_s"] - out[-1]["start_s"]) <= float(tail_gap_s) + 0.005):
        out[-2]["end_s"] = out[-1]["end_s"]
        out.pop()
    return out


def timeline_totals(segments: list) -> dict:
    """各态总秒数(排序/汇总用)。"""
    tot = {"stuck": 0.0, "idle": 0.0, "normal": 0.0}
    for s in segments:
        tot[s["state"]] = round(tot.get(s["state"], 0.0) + s["end_s"] - s["start_s"], 2)
    return tot
