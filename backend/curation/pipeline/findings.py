"""Findings: the shell's side of a result record 2.0 (design doc 17 §1-§2, D56).

A module's algorithm (A-class, never edited) still answers the way v1 did - ``passed``, ``score`` and a
``detail`` dict. The shell turns that answer into the record's ``findings`` (problems, each one a code
of the module's registry catalogue, mapped to a taxonomy item), ``unassessable`` (items the module
covers but could not assess on this episode, and why) and ``readings`` (numbers that are no finding:
the scores among them). ``details`` stay exactly as the algorithm wrote them.

Everything here is a pure function of ``(passed, score, details, params)``: the record writer calls it
when a ``check`` call finishes an episode, and the parity tool and the evaluation call it on records
written before (a 1.0 record still has all three). The judgement lines are module parameters with
defaults (``param_schema``, registry 2.1), never constants of the evaluation tools.

Two guards keep the default policy equal to today's gates (P18): a module that fails an episode
(``passed is False``) yields at least one finding whose code blocks by default, and one that defers to a
person (``passed is None`` with a review line) at least one review finding - each module names the
code its fallback uses (``FALLBACK``) for an answer its ``details`` do not explain.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from ..contracts import modules as registry


@dataclass
class Derived:
    findings: list[dict] = field(default_factory=list)
    unassessable: list[dict] = field(default_factory=list)
    readings: dict = field(default_factory=dict)

    def add(self, module: str, code: str, message_zh: str, **kw) -> dict:
        f = finding(module, code, message_zh, **kw)
        self.findings.append(f)
        return f

    def cannot(self, item: str, reason: str, message_zh: str) -> None:
        if not any(u["item"] == item for u in self.unassessable):
            self.unassessable.append({"item": item, "reason": reason, "message_zh": message_zh})


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x: Any, nd: int = 4) -> Any:
    v = _num(x)
    return round(v, nd) if v is not None else x


def finding(module: str, code: str, message_zh: str, *, severity: str | None = None,
            scope: dict | None = None, frames: tuple[int, int] | list[int] | None = None,
            time_s: tuple[float, float] | list[float] | None = None, readings: dict | None = None,
            evidence: list[str] | None = None, unit: str | None = None) -> dict:
    """One finding of ``module`` with ``code`` (C2 ``finding``); its item comes from the registry."""
    spec = registry.finding_code(module, code)
    out: dict = {"code": code, "item": spec.item, "severity": severity or spec.severity,
                 "message_zh": message_zh}
    if scope:
        out["scope"] = {k: v for k, v in scope.items() if v not in (None, "", [])} or None
    if frames is not None:
        a, b = int(frames[0]), int(frames[1])
        out["frames"] = [min(a, b), max(a, b)]
    if time_s is not None:
        a, b = float(time_s[0]), float(time_s[1])
        out["time_s"] = [round(max(0.0, min(a, b)), 3), round(max(0.0, max(a, b)), 3)]
    if readings:
        out["readings"] = readings
    if evidence:
        out["evidence"] = list(evidence)
    if unit:
        out["unit"] = unit
    return out


#: module -> the function that derives its findings
DERIVERS: dict[str, Callable[[bool | None, float | None, dict, dict], Derived]] = {}
#: module -> (the code a failed episode falls back to, the code an episode handed to a person falls back to)
FALLBACK: dict[str, tuple[str | None, str | None]] = {}


def deriver(module: str, *, fail: str | None = None, defer: str | None = None):
    def register(fn):
        DERIVERS[module] = fn
        FALLBACK[module] = (fail, defer)
        return fn
    return register


def params_of(module: str, given: dict | None) -> dict:
    """The module's parameters with every schema default filled in."""
    props = registry.get(module).param_schema.get("properties") or {}
    out = {k: p["default"] for k, p in props.items() if "default" in p}
    out.update(given or {})
    return out


def derive(module: str, passed: bool | None, score: float | None, details: dict | None,
           params: dict | None = None, context: dict | None = None) -> Derived:
    """The findings, unassessable items and readings of one judged episode of ``module``. ``context``: facts
    the shell knows about the episode that the algorithm did not echo (motion_quality: the action semantics
    the reader settled on), kept as readings."""
    details = details if isinstance(details, dict) else {}
    fn = DERIVERS.get(module)
    out = fn(passed, score, details, params_of(module, params)) if fn else Derived()
    for key, value in (context or {}).items():
        out.readings.setdefault(key, value)
    fail, defer = FALLBACK.get(module, (None, None))
    spec = registry.get(module)
    levels = {c.code: c.level for c in spec.codes}
    if passed is False and fail and not any(levels.get(f["code"]) == "blocking" for f in out.findings):
        why = str(details.get("reason") or details.get("why") or "").strip()
        out.add(module, fail, f"「{spec.name_zh}」判定不通过" + (f"：{why}" if why else ""))
    # a record that says ``skipped`` holds no judgement to defer to a person (task_success on an
    # episode without a task text, D72): the deriver already said what it could not assess
    if passed is None and defer and not details.get("skipped") \
            and not any(levels.get(f["code"]) == "review" for f in out.findings) \
            and not any(levels.get(f["code"]) == "blocking" for f in out.findings):
        why = str(details.get("reason") or "").strip()
        out.add(module, defer, f"「{spec.name_zh}」需要人工确认" + (f"：{why}" if why else ""))
    return out


def assessed(module: str, unassessable: list[dict]) -> list[str]:
    """The items the module covers minus those it could not assess (C2 record 2.0 ``assessed``)."""
    skip = {u["item"] for u in unassessable}
    return [item for item in registry.get(module).covers if item not in skip]


def short_camera(name: Any) -> str:
    """A camera's short name, as the report and the expectations write it (``observation.images.wrist``
    -> ``wrist``)."""
    return str(name).split(".")[-1]


# ---------------------------------------------------------------- frame stage

@deriver("visual_quality")
def _visual_quality(passed, score, d, p) -> Derived:
    """Per camera, from ``per_camera_detail`` (design doc 17 §2.2): frozen share, exposure, sharpness,
    the share of information-dead frames (1 - integrity while the camera is not frozen) and the cameras
    that carry no signal. The composite score is a reading (P18)."""
    out = Derived()
    m = "visual_quality"
    if score is not None:
        out.readings["score"] = _r(score)
    per = d.get("per_camera_detail") if isinstance(d.get("per_camera_detail"), dict) else {}
    if not per and not d.get("per_camera") and not (d.get("camera_liveness") or {}).get("dead_or_padded"):
        for item in registry.get(m).covers:
            out.cannot(item, "no_video", "没有一路相机的画面可以评估")
        return out
    if d.get("per_camera"):
        out.readings["per_camera"] = {short_camera(c): _r(v) for c, v in (d.get("per_camera") or {}).items()}
    for cam, cd in sorted(per.items()):
        if not isinstance(cd, dict):
            continue
        name = short_camera(cam)
        scope = {"camera": name}
        frozen = _num(cd.get("frozen_ratio"))
        if frozen is not None and frozen >= p["frozen_ratio_min"]:
            out.add(m, "frozen", f"{name} 画面冻结：相邻采样帧几乎不变的比例 {frozen:.0%}"
                                 f"（≥ {p['frozen_ratio_min']:.0%}）",
                    scope=scope, readings={"frozen_ratio": _r(frozen)})
        exposure = _num(cd.get("exposure"))
        if exposure is not None and exposure < p["exposure_min"]:
            out.add(m, "exposure_low", f"{name} 曝光不良：曝光分 {exposure:.2f}（低于 {p['exposure_min']}）",
                    scope=scope, readings={"exposure": _r(exposure)})
        sharp = _num(cd.get("sharpness"))
        if sharp is not None and sharp < p["sharpness_min"]:
            out.add(m, "sharpness_low", f"{name} 画面模糊：清晰度分 {sharp:.2f}（低于 {p['sharpness_min']}）",
                    scope=scope, readings={"sharpness": _r(sharp)})
        integ = _num(cd.get("integrity"))
        if integ is not None and (frozen is None or frozen < 0.95):     # frozen forces integrity to 0
            dead = 1.0 - integ
            if dead >= p["dead_share_min"]:
                out.add(m, "information_death", f"{name} 信息死亡帧偏多：灰度几乎无变化的帧占 {dead:.0%}"
                                                f"（≥ {p['dead_share_min']:.0%}）",
                        scope=scope, readings={"dead_share": _r(dead)})
    for cam in (d.get("camera_liveness") or {}).get("dead_or_padded") or []:
        name = short_camera(cam)
        out.add(m, "dead_or_padded", f"{name} 没有信号：画面近乎全黑且不变，或是填充的空画面",
                scope={"camera": name})
    return out


@deriver("video_action_sync", fail="misaligned_all")
def _video_action_sync(passed, score, d, p) -> Derived:
    """From the cross-camera verdict and ``per_camera`` (design doc 17 §2.2): the reject when every trusted
    camera agrees on one offset, single misaligned cameras, suspects, cameras whose correlation is too
    weak to read while the action moves (``low_corr``), and trusted cameras whose lags disagree."""
    out = Derived()
    m = "video_action_sync"
    verdict = str(d.get("verdict") or "")
    per = d.get("per_camera") if isinstance(d.get("per_camera"), dict) else {}
    if not per and verdict == "undecidable":
        why = str(d.get("reason") or "没有可比对的相机或状态量")
        for item in registry.get(m).covers:
            out.cannot(item, "not_applicable", why[:200])
        return out
    out.readings["per_camera"] = {cam: {k: _r(v) for k, v in c.items() if k in ("lag_s", "corr_peak", "code", "trusted")}
                                  for cam, c in sorted(per.items()) if isinstance(c, dict)}
    if d.get("consensus_lag_s") is not None:
        out.readings["consensus_lag_s"] = _r(d.get("consensus_lag_s"))
    if verdict == "misaligned_all":
        lag = _num(d.get("consensus_lag_s"))
        cams = sorted(c for c, v in per.items() if isinstance(v, dict) and v.get("code") == "misaligned")
        where = "提前" if lag is not None and lag < 0 else "滞后"
        out.add(m, "misaligned_all",
                f"全部可信相机一致{where} {abs(lag):.2f} 秒（{'、'.join(cams)}）" if lag is not None
                else "全部可信相机一致指向同一错位",
                scope={"cameras": cams} if cams else None,
                readings={"lag_s": _r(lag), "cameras": cams})
    else:
        for cam, c in sorted(per.items()):
            if isinstance(c, dict) and c.get("trusted") and c.get("code") == "misaligned":
                lag = _num(c.get("lag_s"))
                out.add(m, "camera_misaligned",
                        f"{cam} 与动作错位 {lag:+.2f} 秒，其余相机不同意，只标注不判废" if lag is not None
                        else f"{cam} 与动作错位，只标注不判废",
                        scope={"camera": cam}, readings={"lag_s": _r(lag), "corr_peak": _r(c.get("corr_peak"))})
        suspects = sorted(str(c) for c in d.get("suspect_cameras") or [])
        if suspects:
            out.add(m, "suspect", f"疑似错位但证据不足：{'、'.join(suspects)}",
                    scope={"cameras": suspects},
                    readings={"lag_s": {c: _r((per.get(c) or {}).get("lag_s")) for c in suspects}})
    weak = sorted(cam for cam, c in per.items() if isinstance(c, dict) and c.get("code") == "low_corr")
    if weak:
        out.add(m, "undecidable", f"测不准：{'、'.join(weak)} 有动作，但画面运动与动作的相关太弱",
                scope={"cameras": weak},
                readings={"corr_peak": {c: _r((per.get(c) or {}).get("corr_peak")) for c in weak}})
    trusted = {cam: _num(c.get("lag_s")) for cam, c in per.items()
               if isinstance(c, dict) and c.get("trusted") and _num(c.get("lag_s")) is not None}
    if len(trusted) >= 2:
        lo, hi = min(trusted.values()), max(trusted.values())
        if hi - lo > p["spread_tol_s"]:
            cams = sorted(trusted)
            out.add(m, "lag_inconsistent",
                    f"可信相机之间的滞后相差 {hi - lo:.2f} 秒（超过 {p['spread_tol_s']} 秒）："
                    + "，".join(f"{c} {trusted[c]:+.2f}" for c in cams),
                    scope={"cameras": cams}, readings={f"lag_{c}_s": _r(trusted[c]) for c in cams})
    return out


# ---------------------------------------------------------------- numeric stage

@deriver("timestamp_check", fail="gap")
def _timestamp_check(passed, score, d, p) -> Derived:
    """One kind per failed record (v1 returns at its first problem): a gap (the first jump's frames), jitter,
    timestamps going backwards, a fragment shorter than the minimum, a single timestamp. The duration is a
    reading (the duration outliers are a dataset-level finding of the report, design doc 17 §1.2)."""
    out = Derived()
    m = "timestamp_check"
    for key in ("n", "duration_s", "dt_nominal", "max_dt", "jitter_ratio"):
        if d.get(key) is not None:
            out.readings[key] = _r(d.get(key))
    if passed is not False:
        return out
    reason = str(d.get("reason") or "")
    gaps = [g for g in d.get("gap_frames") or [] if isinstance(g, dict)]
    if gaps:
        first = gaps[0]
        k = int(first.get("frame", 0))
        out.add(m, "gap", (reason or f"第 {k + 1} 帧后间隔突增，疑似丢帧")
                + (f"（共 {len(gaps)} 处）" if len(gaps) > 1 else ""),
                frames=(k, k + 1), readings={"gaps": [{"frame": int(g.get("frame", 0)), "dt_s": _r(g.get("dt"))}
                                                      for g in gaps], "max_dt": _r(d.get("max_dt"))})
    elif "ts" in d and "frame" in d:
        k = int(d.get("frame") or 0)
        out.add(m, "out_of_order", reason or f"时间戳在第 {k + 1} 帧倒退或重复", frames=(k, k + 1),
                readings={"ts": d.get("ts")})
    elif "duration_s" in d and "dt_nominal" not in d:
        out.add(m, "fragment", reason or f"全长只有 {d.get('duration_s')} 秒，疑似采集中断的碎片",
                readings={"duration_s": _r(d.get("duration_s"))})
    elif "jitter_ratio" in d:
        ratio = _num(d.get("jitter_ratio"))
        out.add(m, "jitter", f"采样间隔抖动：偏离标称间隔的帧间隔占 {ratio:.1%}" if ratio is not None
                else "采样间隔抖动", readings={"jitter_ratio": _r(ratio)})
    elif "个时间戳" in reason:
        out.add(m, "single_stamp", reason)
    return out


#: v1's violation types -> the registry's codes (C1 2.0, kinematic_limits)
KINEMATIC_TYPES = ("joint_limit", "velocity_limit", "ee_reach", "ee_translation_velocity", "ee_rotation_velocity")
_KIN_NAMES = {"joint_limit": "关节位置越限", "velocity_limit": "关节速度越限", "ee_reach": "末端超出工作空间",
              "ee_translation_velocity": "末端平移速度越限", "ee_rotation_velocity": "末端转动速度越限"}


@deriver("kinematic_limits", fail="data_invalid")
def _kinematic_limits(passed, score, d, p) -> Derived:
    """The violations of the robot's spec, one finding per kind and joint (the frames are a reading: v1 lists
    single frames, not stretches). A check that cannot compare the action with the spec (the action's meaning
    unknown, delta control, a unit mismatch, a draft spec, a spec that the data contradicts from the start)
    could not assess ACT-4; a malformed action fails as ``data_invalid``."""
    out = Derived()
    m = "kinematic_limits"
    if d.get("profile"):
        out.readings["profile"] = d.get("profile")
    if d.get("n_violations") is not None:
        out.readings["n_violations"] = int(d.get("n_violations") or 0)
    if passed is None:
        out.cannot("ACT-4", "not_applicable", str(d.get("reason") or "没有和规格表做极限对照")[:200])
        return out
    groups: dict[tuple[str, str], list[dict]] = {}
    for v in d.get("violations") or []:
        if isinstance(v, dict) and v.get("type") in KINEMATIC_TYPES:
            groups.setdefault((v["type"], str(v.get("joint", ""))), []).append(v)
    for (kind, joint), vs in sorted(groups.items()):
        frames = sorted({int(v.get("frame", 0)) for v in vs})
        worst = max((abs(_num(v.get("value")) or 0.0) for v in vs), default=None)
        limit = vs[0].get("limit")
        channel = None if joint in ("", "xyz", "rpy") else f"joint {joint}"
        where = f"关节 {joint}" if channel else ("末端位置" if joint == "xyz" else "末端姿态" if joint == "rpy" else "")
        out.add(m, kind, f"{where}{_KIN_NAMES[kind]}：{len(vs)} 处，首次在第 {frames[0] + 1} 帧，"
                         f"最大 {worst:.4g}（限值 {limit}）" if worst is not None else f"{where}{_KIN_NAMES[kind]}",
                scope={"channel": channel} if channel else None,
                readings={"frames": frames[:20], "max_value": _r(worst), "limit": limit})
    if passed is False and not out.findings:
        out.add(m, "data_invalid", str(d.get("reason") or "动作数据无法与规格表对照"))
    return out


def _sub_score(out: Derived, m: str, code: str, value: Any, line: float, text: str, *,
               high_below: float | None = None, scope: dict | None = None, frames=None, time_s=None,
               readings: dict | None = None) -> None:
    v = _num(value)
    if v is None or v >= line:
        return
    severity = "high" if high_below is not None and v < high_below else None
    out.add(m, code, f"{text}：{v:.2f}（低于 {line}）", severity=severity, scope=scope, frames=frames,
            time_s=time_s, readings={code.rsplit("_", 1)[0] if code.endswith("_low") else code: _r(v), **(readings or {})})


#: readings that are 0-1 scores, per module: the report's ``score_hist`` (design doc 17 §5.1) - the retired
#: composite score and the sub-items' own scores
SCORE_READINGS = {
    "motion_quality": ("score", "smoothness", "spike", "gripper_jitter", "actuator_saturation", "fluency"),
    "visual_quality": ("score",),
}


@deriver("motion_quality")
def _motion_quality(passed, score, d, p) -> Derived:
    """Every sub-item reports on its own (the composite score is retired, P18; it stays a reading): smoothness,
    spikes, gripper jitter, actuator saturation, stuck actuators (with their frozen stretch), fluency, and the
    idle opening and closing (TASK-1). A sub-item v1 left empty (None) could not be assessed - its own reason,
    or no state columns."""
    out = Derived()
    m = "motion_quality"
    if score is not None:
        out.readings["score"] = _r(score)
    for key in ("smoothness", "spike", "gripper_jitter", "actuator_saturation", "stuck", "fluency",
                "path_efficiency", "joint_stability", "active_ratio", "idle_head_s", "idle_tail_s",
                "idle_mid_count", "idle_mid_total_s"):
        if d.get(key) is not None:
            out.readings[key] = _r(d.get(key))
    _sub_score(out, m, "smoothness_low", d.get("smoothness"), p["smoothness_min"], "动作不平滑，平滑度分",
               high_below=p["severe_below"])
    spike_frames = [int(f) + 1 for f in d.get("spike_frames") or [] if isinstance(f, (int, float))]
    _sub_score(out, m, "spike", d.get("spike"), p["spike_min"], "动作有孤立尖刺，尖刺分",
               high_below=p["severe_below"], readings={"spike_frames": spike_frames[:20]} if spike_frames else None)
    _sub_score(out, m, "gripper_jitter", d.get("gripper_jitter"), p["gripper_jitter_min"], "夹爪来回抖动，夹爪平稳分",
               readings={"flip_hz": d.get("gripper_flip_hz")} if d.get("gripper_flip_hz") else None)
    axes = d.get("saturation_axes") if isinstance(d.get("saturation_axes"), dict) else {}
    worst_axis = max(axes, key=lambda a: _num(axes[a]) or 0.0) if axes else None
    _sub_score(out, m, "actuator_saturation", d.get("actuator_saturation"), p["saturation_min"],
               "执行器跟不上指令（饱和），响应分", scope={"channel": str(worst_axis)} if worst_axis else None,
               readings={"gap_ratio": _r(d.get("saturation_gap_ratio"))} if d.get("saturation_gap_ratio") is not None else None)
    _sub_score(out, m, "fluency_low", d.get("fluency"), p["fluency_min"], "操作不流畅（中途空转多），流畅度分")
    for s in d.get("stuck_joints") or []:
        if not isinstance(s, dict):
            continue
        a, b = s.get("freeze_start_frame"), s.get("freeze_end_frame")
        joint = s.get("joint")
        out.add(m, "stuck", f"关节 {joint} 卡死：第 {int(a) + 1}–{int(b) + 1} 帧指令在变、读数不动" if a is not None and b is not None
                else f"关节 {joint} 卡死", scope={"channel": f"joint {joint}"},
                frames=(int(a), int(b)) if a is not None and b is not None else None,
                readings={"max_dead_run": s.get("max_dead_run")})
    head, tail = _num(d.get("idle_head_s")), _num(d.get("idle_tail_s"))
    if head is not None and head >= p["idle_edge_min_s"]:
        out.add(m, "idle_opening", f"开头空转 {head:.1f} 秒才开始动作", time_s=(0.0, head),
                readings={"idle_s": _r(head)})
    if tail is not None and tail >= p["idle_edge_min_s"]:
        out.add(m, "idle_closing", f"动作结束后空转 {tail:.1f} 秒", readings={"idle_s": _r(tail)})
    if head is None and tail is None:                      # no idle reading at all: the edges were not looked at
        out.cannot("TASK-1", "not_applicable", str(d.get("reason") or "算不出开头与结尾的空转")[:200])
    # sub-items v1 could not compute: why (its own reason, else no state columns)
    no_state = d.get("stuck") is None and "stuck_reason" not in d and "stuck_strategy" not in d
    for key, item, why_key in (("smoothness", "ACT-1", None), ("spike", "ACT-2", "spike_reason"),
                               ("gripper_jitter", "ACT-5", "gripper_reason"),
                               ("actuator_saturation", "ACT-4", "saturation_reason"),
                               ("stuck", "ACT-8", "stuck_reason"), ("fluency", "TASK-7", "fluency_reason")):
        if d.get(key) is None:
            why = str(d.get(why_key) or "") if why_key else ""
            if not why and key in ("actuator_saturation", "stuck") and no_state:
                out.cannot(item, "no_state_columns", "数据集没有状态量，评估不了执行响应与卡死")
            else:
                out.cannot(item, "not_applicable", (why or "这一项对本条数据算不出来")[:200])
    return out


# ---------------------------------------------------------------- vlm stage

#: task_success verdicts of v1 that end on a person (passed None) other than the label guard's hold
_TASK_SUCCESS = ("success", "arbitration_success", "recovery", "endstate_success")
_TASK_FAILURE = ("failure", "arbitration_failure")


def _evidence_span(d: dict) -> tuple[dict | None, list[dict]]:
    """The primary answer's evidence (video protocol): its first stretch and all of them."""
    items = [e for e in ((d.get("video_assessment") or {}).get("evidence") or []) if isinstance(e, dict)
             and _num(e.get("start_s")) is not None and _num(e.get("end_s")) is not None]
    return (items[0] if items else None), items


@deriver("task_success", fail="failure", defer="uncertain")
def _task_success(passed, score, d, p) -> Derived:
    """v1's verdict (video protocol in production, the frame-probe protocol in older records): failure,
    an abstention, the label guard's hold (which today also asks the task verdict), a mid-way recovery
    (frame protocol only; the video protocol does not report mistakes), and a task text the platform wrote
    itself (LABEL-2). The completion estimate is a reading; the primary answer's evidence stretch is the
    failure's interval (P19)."""
    out = Derived()
    m = "task_success"
    if d.get("skipped") == "no_task_text":
        # D72: the episode has no task text, so there was no judgement to derive from
        text = "没有任务标注，没有做任务成败判定"
        out.add(m, "task_text_missing", text, readings={"task_text_source": "无"})
        for item in ("TASK-4", "LABEL-4"):
            out.cannot(item, "no_task_text", text)
        out.cannot("TASK-10", "not_applicable", "视频判定协议只判成败，不报告中途的失误")
        return out
    verdict = str(d.get("verdict") or "")
    completion = d.get("video_completion", d.get("completion_final"))
    if completion is not None:
        out.readings["completion"] = _r(completion)
    if d.get("task_type"):
        out.readings["task_type"] = d.get("task_type")
    reason = str(d.get("reason") or "").strip()
    first, items = _evidence_span(d)
    if passed is False:
        kw: dict = {}
        if first is not None and (d.get("video_assessment") or {}).get("verdict") == "failure":
            kw = {"time_s": (_num(first["start_s"]), _num(first["end_s"])),
                  "scope": {"camera": short_camera(first.get("camera"))} if first.get("camera") else None,
                  "readings": {"evidence": [{"camera": short_camera(e.get("camera")), "start_s": _r(e.get("start_s")),
                                             "end_s": _r(e.get("end_s")), "observation": str(e.get("observation") or "")[:200]}
                                            for e in items[:6]]}}
        out.add(m, "failure", "任务没有完成" + (f"：{reason[:200]}" if reason else ""), **kw)
    elif passed is None:
        if verdict == "label_conflict_suspect":
            lc = d.get("label_check") or {}
            out.add(m, "label_conflict_suspect",
                    f"模型判失败，但画面描述与标注不一致：标注「{str(lc.get('annotation') or '')[:60]}」，"
                    f"画面「{str(lc.get('caption') or '')[:60]}」" if lc else "判失败但标注与画面疑似不符，先不判废",
                    readings={"annotation": lc.get("annotation"), "caption": lc.get("caption")} if lc else None)
        out.add(m, "uncertain", "任务成败拿不准" + (f"：{reason[:200]}" if reason else ""))
    if verdict == "recovery" or "recovery_dip" in (d.get("rules") or []):
        out.add(m, "recovery", "中途进度回落后又完成了任务", readings={"dip": _r(d.get("dip", (d.get("raw") or {}).get("dip")))})
    elif d.get("input_mode") == "video" or d.get("protocol"):
        out.cannot("TASK-10", "not_applicable", "视频判定协议只判成败，不报告中途的失误")
    source = str(d.get("task_desc_source") or "")
    if source in ("自产caption", "无"):
        # "自产caption": a record from before D72, judged with a caption the platform wrote
        out.add(m, "task_text_missing", "没有任务标注，用的是当时平台生成的描述" if source == "自产caption"
                else "没有任务标注，也没有描述", readings={"task_text_source": source})
    elif not source:
        out.cannot("LABEL-2", "not_applicable", "记录里没有任务描述的来源")
    return out


#: camera_defects' answer levels -> a finding's severity (design doc 17 §2.2: minor -> low, severe -> medium)
CAMERA_LEVELS = {"minor": "low", "severe": "medium"}
_CAMERA_ITEMS = (("glitch", "IMG-5", "花屏"), ("shake", "IMG-6", "画面抖动"), ("contamination", "IMG-7", "镜头污染或遮挡"))
_CONTAMINATION = {"dirt": "脏污", "smudge": "污渍", "water": "水滴", "obstruction": "遮挡", "other": "其他"}


@deriver("camera_defects")
def _camera_defects(passed, score, d, p) -> Derived:
    """Per camera and item, a minor or severe answer is a finding with the stretches the model named; an
    item no camera answered could not be assessed (``model_no_answer``)."""
    out = Derived()
    m = "camera_defects"
    if d.get("clean_ratio") is not None:
        out.readings["clean_ratio"] = _r(d.get("clean_ratio"))
    per = d.get("per_camera") if isinstance(d.get("per_camera"), dict) else {}
    items = d.get("items") if isinstance(d.get("items"), dict) else {}
    if not per:
        why = str(d.get("reason") or "没有逐机位复核的回答")
        for _, item, _ in _CAMERA_ITEMS:
            out.cannot(item, "model_no_answer", why[:200])
        return out
    for key, item, name in _CAMERA_ITEMS:
        if items.get(key, "unknown") == "unknown":
            out.cannot(item, "model_no_answer", f"没有一路相机回答「{name}」")
        for cam, e in sorted(per.items()):
            entry = (e or {}).get(key) or {}
            level = entry.get("level")
            if level not in CAMERA_LEVELS:
                continue
            times = [t for t in entry.get("times") or [] if isinstance(t, (list, tuple)) and len(t) == 2]
            label = name
            if key == "contamination" and entry.get("kind") in _CONTAMINATION and entry.get("kind") != "other":
                label = f"镜头{_CONTAMINATION[entry['kind']]}"
            note = str(entry.get("note") or "").strip()
            out.add(m, key, f"{short_camera(cam)} {label}（{'严重' if level == 'severe' else '轻微'}）"
                            + (f"：{note[:120]}" if note else ""),
                    severity=CAMERA_LEVELS[level], scope={"camera": short_camera(cam)},
                    time_s=(float(times[0][0]), float(times[0][1])) if times else None,
                    readings={"level": level, "times": times[:8],
                              **({"kind": entry.get("kind")} if key == "contamination" else {})})
    return out


@deriver("eef_video_consistency", fail="inconsistent", defer="unsettled")
def _eef_video_consistency(passed, score, d, p) -> Derived:
    """The decision (design doc 12 appendix C.9): reject, a person to settle, or - without a gripper
    reference - the model's opinion; plus the comparison with the dataset's own record (reported only)."""
    out = Derived()
    m = "eef_video_consistency"
    dec = d.get("decision") if isinstance(d.get("decision"), dict) else {}
    outcome = dec.get("outcome")
    reason = str(d.get("reason") or dec.get("reason") or "").strip()
    if d.get("overall"):
        out.readings["overall"] = d.get("overall")
    if passed is False or outcome == "reject":
        cams = sorted({str(c.get("camera_id")) for c in dec.get("confirmed") or [] if isinstance(c, dict) and c.get("camera_id")})
        out.add(m, "inconsistent", reason or "末端投影与画面不符", scope={"cameras": cams} if cams else None,
                readings={"confirmed": [{k: c.get(k) for k in ("subitem", "camera_id") if c.get(k)}
                                        for c in dec.get("confirmed") or [] if isinstance(c, dict)]})
    elif passed is None or outcome == "human":
        codes = sorted({str(h.get("code")) for h in dec.get("human") or [] if isinstance(h, dict) and h.get("code")})
        out.add(m, "unsettled", reason.removeprefix("需要人工裁决：") or "末端投影与画面是否相符要人看",
                readings={"why": codes})
    opinion = d.get("opinion") if isinstance(d.get("opinion"), dict) else {}
    if opinion.get("status") == "not_assessable":   # no trajectory for the episode: nothing was compared
        out.cannot("MV-4", "not_applicable", str(opinion.get("failure") or reason or "这一条没有轨迹")[:200])
    if opinion.get("flagged"):
        best, best_cam = None, None
        for cid, cam in (opinion.get("cameras") or {}).items():
            for s in (cam or {}).get("segments") or []:
                if isinstance(s, dict) and (best is None or (_num(s.get("confidence")) or 0) > (_num(best.get("confidence")) or 0)):
                    best, best_cam = s, cid
        kw = {}
        if best is not None and _num(best.get("start_s")) is not None and _num(best.get("end_s")) is not None:
            kw = {"time_s": (_num(best["start_s"]), _num(best["end_s"])), "scope": {"camera": str(best_cam)}}
        out.add(m, "opinion_mismatch", f"模型认为末端投影与画面不符（最高置信 {_num(opinion.get('max_confidence')) or 0:.2f}）",
                readings={"max_confidence": _r(opinion.get("max_confidence")), "segments": opinion.get("segments")}, **kw)
    _ego_motion(out, d)
    record = d.get("record") if isinstance(d.get("record"), dict) else {}
    if record.get("status") == "suspect":
        why = sorted({str(r) for src in (record.get("sources") or {}).values() if isinstance(src, dict)
                      for r in src.get("reasons") or []})
        out.add(m, "record_mismatch", "上传的轨迹与数据集自己记录的末端位姿不一致" + (f"：{'、'.join(why)}" if why else ""),
                readings={"status": "suspect", "reasons": why})
    return out


#: the band of a wrist camera's bad stretch -> the finding's severity (design doc 22 §5.3)
_EGO_SEVERITY = {"minor": "low", "moderate": "medium", "severe": "high"}
_EGO_BAND_ZH = {"minor": "轻", "moderate": "中", "severe": "重"}


def _ego_motion(out: Derived, d: dict) -> None:
    """A wrist camera's own motion against the recorded poses (design doc 22 §5.3): one ``ego_motion_suspect`` at
    the episode's worst stretch; its time offset reading is the episode's AV-1 (unassessable without one)."""
    m = "eef_video_consistency"
    ego = d.get("ego_motion") if isinstance(d.get("ego_motion"), dict) else {}
    cams = ego.get("cameras") if isinstance(ego.get("cameras"), dict) else {}
    timed = [c for c in cams.values() if isinstance(c, dict) and c.get("status") in ("ok", "suspect") and c.get("lag")]
    if not timed:
        out.cannot("AV-1", "not_applicable", "没有腕部相机自运动的时间读数（只有带位姿的腕部相机才有）")
    if ego.get("status") != "suspect":
        return
    worst = ego.get("worst") if isinstance(ego.get("worst"), dict) else None
    lags = {cid: c["lag"].get("lag_s") for cid, c in cams.items() if isinstance(c, dict) and (c.get("lag") or {}).get("flagged")}
    kw = {}
    if worst is not None:
        if _num(worst.get("start_s")) is not None and _num(worst.get("end_s")) is not None:
            kw["time_s"] = (_num(worst["start_s"]), _num(worst["end_s"]))
        if worst.get("camera"):
            kw["scope"] = {"camera": str(worst["camera"])}
        kw["severity"] = _EGO_SEVERITY.get(str(worst.get("band")), None)
        band = _EGO_BAND_ZH.get(str(worst.get("band")), "")
        where = f"第 {int(worst['start_frame']) + 1}–{int(worst['end_frame']) + 1} 帧" if worst.get("start_frame") is not None else ""
        if worst.get("reason") == "time_offset":
            lag = _num(worst.get("lag_s")) or 0.0
            what = f"位姿约{'晚' if lag > 0 else '早'} {abs(lag):.2f} s"
        else:
            what = f"画面里的转动与位姿差 {_num(worst.get('magnitude')) or 0:.1f}°"
        detail = "，".join(x for x in (where, band) if x)
        message = f"腕部相机的运动与记录的位姿不一致：{what}" + (f"（{detail}）" if detail else "")
    else:
        message = "腕部相机的运动与记录的位姿不一致"
    readings = {"status": "suspect",
                "cameras": {cid: {k: c.get("metrics", {}).get(k) for k in ("rotation_median_deg", "rotation_p95_deg",
                                                                            "direction_median_deg", "lag_s", "lag_confidence",
                                                                            "coverage")} | {"status": c.get("status")}
                            for cid, c in cams.items() if isinstance(c, dict)},
                "segments": [{"camera": cid, **{k: s.get(k) for k in ("start_frame", "end_frame", "start_s", "end_s",
                                                                        "reason", "magnitude", "unit", "band")}}
                             for cid, c in cams.items() if isinstance(c, dict) for s in c.get("segments") or []]}
    if lags:
        readings["lag_s"] = lags
    out.add(m, "ego_motion_suspect", message, readings=readings, **kw)


# ---------------------------------------------------------------- data integrity

#: v1's row validation messages (``ingest/validate.py``) -> the split codes of ``row_invalid``
_ROW_INVALID = (("action 缺失或为空", "action_missing"), ("视频文件不存在", "video_missing"),
                ("时间戳非严格递增", "timestamps_invalid"), ("视频时间边界非法", "metadata_invalid"),
                ("fps 非法", "metadata_invalid"), ("长度", "length_mismatch"), ("帧数", "length_mismatch"),
                ("NaN", "values_invalid"), ("dtype", "values_invalid"), ("二维", "values_invalid"))


def integrity_code(f: dict) -> str | None:
    """The registry code of one of the integrity module's own findings (design doc 14 §4.2 -> 17 §2.2)."""
    code = str(f.get("code") or "")
    err = str((f.get("args") or {}).get("error") or "")         # the reader's own words, when it gave any
    msg = str(f.get("message") or "")
    if code == "row_invalid":
        text = err or msg
        return next((c for needle, c in _ROW_INVALID if needle in text), "values_invalid")
    if code == "file_truncated" and (msg.startswith("录制中断") or err.startswith("录制中断")):
        return "cut_unreadable"
    try:
        registry.finding_code("data_integrity", code)
    except KeyError:
        return None
    return code


@deriver("data_integrity", fail="structure_invalid")
def _data_integrity(passed, score, d, p) -> Derived:
    """The module's own findings, one each, with the file, the camera or topic, and the stretch of a broken
    file on the episode's time axis (a LeRobot v3 file is shared: its window is subtracted, P19)."""
    out = Derived()
    m = "data_integrity"
    windows = {str(f.get("file")): f.get("window_s") for f in d.get("files") or [] if isinstance(f, dict)
               and isinstance(f.get("window_s"), (list, tuple))}
    for f in d.get("findings") or []:
        if not isinstance(f, dict) or f.get("level") == "dataset":
            continue
        code = integrity_code(f)
        if code is None:
            continue
        scope = {"camera": f.get("camera"), "file": f.get("file") or (f.get("args") or {}).get("topic")}
        kw: dict = {}
        span = f.get("span_s")
        if isinstance(span, (list, tuple)) and len(span) == 2 and _num(span[0]) is not None:
            start, end = _num(span[0]), _num(span[1])
            win = windows.get(str(f.get("file")))
            if win is not None and _num(win[0]) is not None:
                start -= _num(win[0])
                end = (end - _num(win[0])) if end is not None else (_num(win[1]) - _num(win[0]) if _num(win[1]) is not None else None)
            if end is not None:
                kw["time_s"] = (start, end)
            else:
                kw["readings"] = {"from_s": _r(start)}
        out.add(m, code, str(f.get("message") or code), scope=scope, **kw)
    for key in ("outcome", "tiers"):
        if d.get(key) is not None:
            out.readings[key] = d.get(key)
    return out


# ---------------------------------------------------------------- dataset level

@deriver("dedup", fail="duplicate")
def _dedup(passed, score, d, p) -> Derived:
    """An episode byte-identical to an earlier one: its group is the first member's index (every copy points at
    it). Which member a group keeps is aggregate's call (design doc 17 §4.5)."""
    out = Derived()
    if passed is False and d.get("duplicate_of") is not None:
        first = int(d["duplicate_of"])
        out.add("dedup", "duplicate", f"与 ep{first:06d} 字节级完全重复",
                readings={"group_id": first, "duplicate_of": first})
    return out



# ---------------------------------------------------------------- dataset-level findings

def _quartiles(values: list[float]) -> tuple[float, float]:
    v = sorted(values)

    def q(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)

    return q(0.25), q(0.75)


def dataset_level(module: str, records: dict[int, dict], params: dict | None = None, *,
                  integrity: dict | None = None) -> tuple[list[dict], dict]:
    """(dataset-level findings, dataset-level readings) of ``module`` over a task's records (design doc 17
    §1.2: counted once per dataset): the integrity module's own (``integrity``: its ``dataset.json``), the
    timestamps' duration outliers, the action semantics nobody could settle. Every finding carries
    ``unit: dataset``."""
    p = params_of(module, params)
    ok = {e: r for e, r in records.items() if isinstance(r, dict) and r.get("status") == "ok"}
    found: list[dict] = []
    readings: dict = {}
    if module == "data_integrity":
        for f in (integrity or {}).get("findings") or []:
            code = integrity_code(f) if isinstance(f, dict) else None
            if code in ("orphan_files", "dark_camera", "table_overlap"):
                found.append(finding(module, code, str(f.get("message") or code), unit="dataset",
                                     readings=f.get("args") or None))
    elif module == "timestamp_check":
        durations = {e: _num((r.get("readings") or {}).get("duration_s")) for e, r in ok.items()}
        durations = {e: v for e, v in durations.items() if v is not None}
        if len(durations) >= 8:
            q1, q3 = _quartiles(list(durations.values()))
            k = float(p["duration_outlier_iqr"])
            lo, hi = q1 - k * (q3 - q1), q3 + k * (q3 - q1)
            out = sorted(e for e, v in durations.items() if v < lo or v > hi)
            readings.update(duration_q1_s=_r(q1), duration_q3_s=_r(q3))
            if out and q3 > q1:
                found.append(finding(module, "duration_outlier",
                                     f"{len(out)} 条时长离群（四分位区间 {q1:.1f}–{q3:.1f} 秒之外 {k:g} 倍四分位距）："
                                     + "、".join(f"ep{e}（{durations[e]:.1f} 秒）" for e in out[:10]),
                                     unit="dataset", readings={"episodes": out, "low_s": _r(lo), "high_s": _r(hi)}))
    elif module == "motion_quality":
        active = [_num((r.get("readings") or {}).get("active_ratio")) for r in ok.values()]
        active = [a for a in active if a is not None]
        if active:
            readings["active_ratio_mean"] = _r(sum(active) / len(active))
        sem = [(r.get("readings") or {}).get("action_semantics") for r in ok.values()]
        sem = [s for s in sem if isinstance(s, dict)]
        if sem:
            readings["action_semantics"] = {k: sem[0].get(k) for k in ("source", "action_space", "control_mode", "preflight")
                                            if sem[0].get(k)}
            unsure = [s for s in sem if s.get("undetermined")]
            if unsure:
                found.append(finding(module, "action_semantics_undetermined",
                                     f"判断不了动作数据的含义（{len(unsure)} / {len(sem)} 条）：运动学极限与依赖动作语义的子项没有评估",
                                     unit="dataset", readings={"episodes": len(unsure)}))
    return found, readings
