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
                           event_segments: list | None = None) -> list:
    """→ [{"start_s","end_s","state"}],首尾相接铺满 [0, duration]。

    合并规则:stuck 段优先于 idle 段(重叠时定罪压过停手);其余为 normal;
    相邻同态段自动缝合;所有边界保留 2 位小数。
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
    return out


def timeline_totals(segments: list) -> dict:
    """各态总秒数(排序/汇总用)。"""
    tot = {"stuck": 0.0, "idle": 0.0, "normal": 0.0}
    for s in segments:
        tot[s["state"]] = round(tot.get(s["state"], 0.0) + s["end_s"] - s["start_s"], 2)
    return tot
