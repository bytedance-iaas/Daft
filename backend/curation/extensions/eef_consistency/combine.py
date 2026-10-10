"""The EEF module's merge and output (design doc 25 §7, D81, D82): the channels are never averaged.

Per sub-item and camera, the CPU side (the measurement, or a wrist camera's own motion) and the model side (its
review of the CPU's windows, or its opinion on the whole clip) each give an inconsistency confidence p
(:mod:`.channels`). The cell takes the largest; one side at the high band and the other below the low band is a
``conflict`` (the only thing a person is asked); one side alone is capped (``single_source``, saying what is
missing and why); the model saying most of a camera's windows track something else voids the CPU there
(``tracking_invalid``). The episode is its worst cell - not an average over cameras (D-E4) - and its output is a
label, p and one sentence of grounds.

The bands and the cap are the threshold profile's (``merge``); the demo profile's are not calibrated: p orders,
it is not a probability (§7.2).
"""
from __future__ import annotations

from typing import Any

from . import channels as CH
from . import contracts as C

#: design doc 25 §7.1-§7.2 (demo; uncalibrated): p >= high "inconsistent", [low, high) "possibly", < low "consistent"
DEFAULTS = {"high": 0.7, "low": 0.4, "single_source_cap": 0.8, "full_at": CH.FULL_AT}
INCONSISTENT, POSSIBLY, CONSISTENT, CANNOT_TELL = "inconsistent", "possibly_inconsistent", "consistent", "cannot_tell"
LABEL_ZH = {INCONSISTENT: "不一致", POSSIBLY: "可能不一致", CONSISTENT: "一致", CANNOT_TELL: "判断不了"}
CONFLICT, SINGLE_SOURCE, TRACKING_INVALID = "conflict", "single_source", "tracking_invalid"
#: why a cell has one side only (``single_source.missing``)
NO_REFERENCE, VLM_OFF, NO_BACKEND, MODEL_NO_ANSWER, MODEL_UNSURE, NOT_ASKED, MODEL_CANNOT_SEE = (
    "no_gripper_reference", "vlm_off", "no_vlm_backend", "model_no_answer", "model_unsure", "not_asked",
    "model_cannot_see")
NOT_MEASURED, CPU_UNSURE = "not_measured", "cpu_cannot_tell"
SUB_ZH = {C.POSITION: "位置", C.ORIENTATION: "朝向", C.TEMPORAL: "时间对齐", C.CAMERA_MOTION: "画面运动",
          C.STATE_MOTION: "记录抖动", C.EGO_MOTION: "自运动"}
CHANNEL_ZH = {CH.CPU: "CPU", CH.EGO: "自运动", CH.VLM_REVIEW: "模型复核", CH.VLM_OPINION: "模型意见"}
MISSING_ZH = {NO_REFERENCE: "没给夹爪参考", VLM_OFF: "没开 VLM", NO_BACKEND: "没有模型后端", MODEL_NO_ANSWER: "模型没答",
              MODEL_UNSURE: "模型拿不准", NOT_ASKED: "没问模型这一项", MODEL_CANNOT_SEE: "这一项模型看不了",
              NOT_MEASURED: "这一项 CPU 不测", CPU_UNSURE: "CPU 判断不了"}


def settings(prof=None) -> dict:
    return {**DEFAULTS, **(getattr(prof, "merge", None) or {})}


def label_of(p: float | None, cfg: dict) -> str:
    if p is None:
        return CANNOT_TELL
    return INCONSISTENT if p >= cfg["high"] else POSSIBLY if p >= cfg["low"] else CONSISTENT


def _missing(side: tuple, why: str | None, reviewed: bool = False) -> str:
    """Why one side gave nothing on a cell: the model was not asked, did not answer, answered without taking a side
    (no vote, a tie, an "ok" with no confidence) or cannot see the sub-item; the CPU does not measure it or could not
    tell (too few comparable frames, ...)."""
    if side == CH.VLM_SIDE:
        if why in ("no_vote", "tie", "no_confidence"):
            return MODEL_UNSURE
        if why in (MODEL_NO_ANSWER, NOT_ASKED, VLM_OFF, NO_BACKEND):
            return why
        return NOT_ASKED if reviewed else MODEL_CANNOT_SEE
    if why in (NO_REFERENCE, None, NOT_MEASURED):
        return why or NOT_MEASURED
    return CPU_UNSURE


def _rebanded(side: dict | None, cfg: dict) -> dict | None:
    if side is None:
        return None
    p = CH.p_of(side.get("verdict", CH.CANNOT_TELL), side.get("confidence"), cfg["low"])
    out = dict(side, p=p)
    if p is None and side.get("verdict") == CH.OK and not out.get("why"):
        out["why"] = "no_confidence"                   # an "ok" that says nothing
    return out


def merge_cell(sub: str, camera: str | None, cpu: dict | None, vlm: dict | None, cfg: dict, *,
               tracking_invalid: bool = False, cpu_name: str = CH.CPU, vlm_name: str | None = None,
               vlm_missing: str | None = None, cpu_missing: str | None = None) -> dict[str, Any]:
    """One cell: the largest p of the two sides, with its marks (§7.1)."""
    flags: list[str] = []
    sources: dict[str, Any] = {}
    # each side's p against the task's own low band (channels.p_of), and so its label
    cpu = _rebanded(cpu, cfg)
    vlm = _rebanded(vlm, cfg)
    if cpu is not None:
        sources[cpu_name] = cpu
    if vlm is not None and vlm_name:
        sources[vlm_name] = vlm
    cpu_p = cpu.get("p") if cpu else None
    if tracking_invalid and cpu_name == CH.CPU and sub in CH.REVIEWED and cpu_p is not None:
        flags.append(TRACKING_INVALID)               # the green cross followed something else: no CPU reading here
        cpu_p = None
    vlm_p = vlm.get("p") if vlm else None
    out: dict[str, Any] = {"subitem": sub, "camera": camera, "sources": sources}
    ps = [x for x in (cpu_p, vlm_p) if x is not None]
    if len(ps) == 2:
        p = max(ps)
        if max(ps) >= cfg["high"] and min(ps) < cfg["low"]:
            flags.append(CONFLICT)
    elif len(ps) == 1:
        p = min(ps[0], cfg["single_source_cap"])
        if cpu_p is None:
            why = TRACKING_INVALID if TRACKING_INVALID in flags else \
                cpu_missing or _missing(CH.CPU_SIDE, (cpu or {}).get("why"))
        else:
            why = vlm_missing or _missing(CH.VLM_SIDE, (vlm or {}).get("why"),
                                          reviewed=cpu_name == CH.CPU and sub in CH.REVIEWED)
        flags.append(SINGLE_SOURCE)
        out["missing"] = why
    else:
        p = None
        whys = [x.get("why") for x in (cpu, vlm) if x and x.get("why")]
        if vlm_missing:
            whys.append(vlm_missing)
        out["why"] = sorted(set(whys))
    out.update(p=None if p is None else round(p, 3), label=label_of(p, cfg), flags=flags)
    for src in (sources.get(cpu_name), sources.get(vlm_name or "")):
        if src and src.get("time_s") and "time_s" not in out:
            out["time_s"] = list(src["time_s"])
            if src.get("evidence_frames"):
                out["evidence_frames"] = list(src["evidence_frames"])
    return out


def merge(*, cpu: dict | None = None, review: tuple[dict, dict] | None = None, opinion: dict | None = None,
          ego: dict | None = None, cfg: dict | None = None, vlm_missing: str | None = None,
          hypotheses: list | None = None) -> dict[str, Any]:
    """All cells of an episode and its output (``details.merged``).

    ``cpu`` / ``review`` (channels, tracking) / ``opinion`` / ``ego``: the channels' cells; ``vlm_missing``: why the
    model gave nothing at all (VLM switched off, no backend); ``hypotheses``: the CPU's diagnosis, named in the
    grounds of a position cell it supports."""
    cfg = {**DEFAULTS, **(cfg or {})}
    cpu = cpu or {}
    rev_cells, tracking = review if review else ({}, {})
    opinion = opinion or {}
    ego = ego or {}
    keys = list(dict.fromkeys([*cpu, *ego, *rev_cells, *opinion]))
    cells = []
    for sub, cam in keys:
        if (sub, cam) in ego:
            cpu_side, cpu_name = ego.get((sub, cam)), CH.EGO
        else:
            cpu_side, cpu_name = cpu.get((sub, cam)), CH.CPU
        if (sub, cam) in rev_cells:
            vlm_side, vlm_name = rev_cells[(sub, cam)], CH.VLM_REVIEW
        elif (sub, cam) in opinion:
            vlm_side, vlm_name = opinion[(sub, cam)], CH.VLM_OPINION
        else:
            vlm_side, vlm_name = None, None
        miss_vlm = vlm_missing if vlm_side is None and vlm_missing else (
            MODEL_CANNOT_SEE if vlm_side is None and cpu_name == CH.CPU and sub not in CH.REVIEWED else None)
        miss_cpu = NO_REFERENCE if cpu_side is None and vlm_name == CH.VLM_OPINION and sub in CH.CAMERA_ITEMS else None
        cell = merge_cell(sub, cam, cpu_side, vlm_side, cfg, tracking_invalid=bool((tracking.get(cam) or {}).get("invalid")),
                          cpu_name=cpu_name, vlm_name=vlm_name, vlm_missing=miss_vlm, cpu_missing=miss_cpu)
        if sub == C.POSITION and hypotheses:
            sup = sorted({h.get("hypothesis") for h in hypotheses if isinstance(h, dict) and h.get("supported")
                          and h.get("camera_id") in (None, cam) and h.get("hypothesis")})
            if sup:
                cell["supported_hypotheses"] = sup
        cells.append(cell)
    return {"cells": cells, "episode": episode_of(cells, cfg), "bands": {k: cfg[k] for k in ("high", "low", "single_source_cap")}}


def episode_of(cells: list[dict], cfg: dict) -> dict[str, Any]:
    """The episode's output: its worst cell (largest p, a conflict first on a tie), label, p, flags and grounds."""
    rated = [c for c in cells if c.get("p") is not None]
    if not rated:
        whys = sorted({w for c in cells for w in c.get("why") or []})
        return {"label": CANNOT_TELL, "p": None, "subitem": None, "camera": None, "flags": [], "why": whys,
                "reason": "判断不了：" + ("、".join(whys) if whys else "没有任何渠道给出结论"),
                "conflicts": 0, "cells_rated": 0, "cells": len(cells)}
    worst = max(rated, key=lambda c: (c["p"], CONFLICT in c["flags"]))
    conflicts = sum(CONFLICT in c["flags"] for c in cells)
    return {"label": worst["label"], "p": worst["p"], "subitem": worst["subitem"], "camera": worst["camera"],
            "flags": list(worst["flags"]), "reason": grounds(worst), "conflicts": conflicts,
            "cells_rated": len(rated), "cells": len(cells)}


def _fmt(p) -> str:
    return "—" if p is None else f"{p:.2f}"


def grounds(cell: dict) -> str:
    """One sentence: label · p, the sub-item and camera, what each side said (§7.2)."""
    where = SUB_ZH.get(cell["subitem"], cell["subitem"]) + (f"（相机 {cell['camera']}）" if cell.get("camera") else "")
    parts = []
    for name, src in (cell.get("sources") or {}).items():
        said = {CH.ISSUE: "认为不一致", CH.OK: "认为一致", CH.CANNOT_TELL: "判断不了"}.get(src.get("verdict"), "")
        parts.append(f"{CHANNEL_ZH.get(name, name)}{said}（{_fmt(src.get('p'))}）")
    head = f"{LABEL_ZH[cell['label']]} · {_fmt(cell.get('p'))}"
    if CONFLICT in cell["flags"]:
        head += " · 冲突"
    tail = []
    if TRACKING_INVALID in cell["flags"]:
        tail.append("模型多数认为绿十字跟错了目标，CPU 读数作废")
    if SINGLE_SOURCE in cell["flags"] and cell.get("missing"):
        tail.append(f"只有一个渠道：{MISSING_ZH.get(cell['missing'], cell['missing'])}")
    if cell.get("supported_hypotheses"):
        tail.append("诊断支持：" + "、".join(cell["supported_hypotheses"]))
    return f"{head}：{where}，" + "，".join(parts + tail)
