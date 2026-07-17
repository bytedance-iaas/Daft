"""stuck 事件聚合与时间线(2026-07-15 用户定):嵌套结构交给 JSON,CSV 只留摘要。

输入 = motion_quality 的 stuck_joints(每轴每段一条,含精确起止帧+包络);
输出 = 事件列表:包络重叠的段合并为一次物理事件(手腕卡死时 y/z 差一帧先后冻),
每事件带按时间顺序的 timeline(idle→stuck→idle...,秒+帧双标)。纯函数,零依赖。
"""
from __future__ import annotations


def build_stuck_events(stuck_joints: list[dict], fps: float) -> list[dict]:
    """stuck_joints → 事件列表(含 timeline)。

    事件 = 包络窗口重叠的一组(轴,段);timeline = 包络内证据区间(stuck)与
    其余静止(idle)的时间顺序剧本。stuck 区间取各轴证据窗的并集(两轴差一帧
    先后冻结时,并集才是"手臂失灵"的真实窗口)。
    """
    if not stuck_joints:
        return []
    items = sorted(stuck_joints, key=lambda s: s.get("envelope_start_frame", 0))
    groups: list[dict] = []
    for sj in items:
        e0 = int(sj.get("envelope_start_frame", sj.get("freeze_start_frame", 0)))
        e1 = e0 + int(sj.get("envelope_frames", sj.get("max_dead_run", 0)))
        ev0 = int(sj.get("freeze_start_frame", 0))
        ev1 = int(sj.get("freeze_end_frame", ev0 + int(sj.get("max_dead_run", 0))))
        if groups and e0 <= groups[-1]["env_end"]:
            g = groups[-1]
            g["env_end"] = max(g["env_end"], e1)
            g["axes"].append(str(sj.get("axis")))
            g["evid"].append((ev0, ev1))
        else:
            groups.append({"env_start": e0, "env_end": e1,
                           "axes": [str(sj.get("axis"))], "evid": [(ev0, ev1)]})

    def _merge(iv: list[tuple]) -> list[tuple]:
        out: list[tuple] = []
        for a, b in sorted(iv):
            if out and a <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        return out

    def _sec(f: int) -> float:
        return round(f / fps, 2)

    events = []
    for g in groups:
        stuck_iv = _merge(g["evid"])
        timeline = []
        cursor = g["env_start"]
        for a, b in stuck_iv:
            if a > cursor:
                timeline.append({"start_s": _sec(cursor), "end_s": _sec(a),
                                 "frames": [cursor, a], "state": "idle",
                                 "note": "静止且指令未推"})
            timeline.append({"start_s": _sec(a), "end_s": _sec(b),
                             "frames": [a, b], "state": "stuck",
                             "note": "指令在推,无响应(定罪证据)"})
            cursor = max(cursor, b)
        if g["env_end"] > cursor:
            timeline.append({"start_s": _sec(cursor), "end_s": _sec(g["env_end"]),
                             "frames": [cursor, g["env_end"]], "state": "idle",
                             "note": "静止且指令未推(常见:操作员发现卡死停手)"})
        stuck_s = sum(b - a for a, b in stuck_iv) / fps
        total_s = (g["env_end"] - g["env_start"]) / fps
        # 轴名去重保序
        axes = list(dict.fromkeys(g["axes"]))
        # 摘要只留四个自明量(2026-07-15 用户定:idle_before/after 是 timeline 的
        # 劣化摘要,删——idle 总量=freeze_total-stuck,细节看 timeline)
        events.append({
            "axes": axes,
            "stuck_start_sec": _sec(stuck_iv[0][0]),
            "stuck_seconds": round(stuck_s, 1),
            "freeze_start_sec": _sec(g["env_start"]),
            "freeze_total_seconds": round(total_s, 1),
            "timeline": timeline,
        })
    return events
