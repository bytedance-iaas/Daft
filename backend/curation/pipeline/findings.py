"""Findings: the shell's side of a result record 2.0 (design doc 17 §1-§2, D56).

A module's algorithm (A-class, never edited) still answers the way v1 did - ``passed``, ``score`` and a
``detail`` dict. The shell turns that answer into the record's ``findings`` (problems, each one a code
of the module's registry catalogue, mapped to a taxonomy item), ``unassessable`` (items the module
covers but could not assess on this episode, and why) and ``readings`` (numbers that are no finding:
the scores among them). ``details`` stay exactly as the algorithm wrote them.

Everything here is a pure function of ``(passed, score, details, params)``: the record writer calls it
when a ``check`` call finishes an episode. The judgement lines are module parameters with defaults
(``param_schema``, registry 2.1), never constants of the evaluation tools.

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
    if passed is False and fail and not any(levels.get(f["code"]) == "blocking" or f["code"] == fail
                                            for f in out.findings):
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
    out.readings["per_camera"] = {cam: {k: _r(v) for k, v in c.items()
                                        if k in ("lag_s", "corr_peak", "code", "trusted", "basis", "whole_stable", "instability")}
                                  for cam, c in sorted(per.items()) if isinstance(c, dict)}
    # the windowed median (second estimator, 2026-10-13): its summary beside each camera's reading
    for cam, c in sorted(per.items()):
        lw = c.get("lag_windows") if isinstance(c, dict) else None
        if isinstance(lw, dict) and cam in out.readings["per_camera"]:
            out.readings["per_camera"][cam]["lag_windows"] = {k: _r(lw.get(k)) for k in ("lag_s", "n_trusted", "n_windows", "iqr_s")}
    if d.get("consensus_lag_s") is not None:
        out.readings["consensus_lag_s"] = _r(d.get("consensus_lag_s"))
    if verdict == "misaligned_all":
        lag = _num(d.get("consensus_lag_s"))
        cams = sorted(c for c, v in per.items() if isinstance(v, dict) and v.get("code") == "misaligned")
        where = "提前" if lag is not None and lag < 0 else "滞后"
        # the same words as the other sync texts (2026-10-13): what happened, then every camera's reading
        per_cam = "，".join(f"{c} {_num(per[c].get('lag_s')):+.2f}" for c in cams if _num(per[c].get("lag_s")) is not None)
        out.add(m, "misaligned_all",
                (f"全部相机一致比动作{'早' if lag < 0 else '晚'} {abs(lag):.2f} 秒" if lag is not None else "全部相机一致错开")
                + (f"：{per_cam}" if per_cam else ""),
                scope={"cameras": cams} if cams else None,
                readings={"lag_s": _r(lag), "cameras": cams})
    else:
        # AV-1 from each camera's best reading (7.5): its trusted whole-episode lag, else its steady windowed median
        # beyond the same tolerance the whole-episode rule uses (global_lag's lag_tol_s, 0.25 s) - as MV-3 already does;
        # on the 17-dataset benchmark the windowed route adds one control alarm (rh20t ep1, 10 Hz) in 139
        best = _best_readings(per, p)
        for cam, c in sorted(per.items()):
            if not isinstance(c, dict):
                continue
            lag, kind, code = best.get(cam, (None, None, None))
            whole = c.get("trusted") and c.get("code") == "misaligned"
            if whole:
                lag = _num(c.get("lag_s"))
            elif not (kind == "windowed" and lag is not None and abs(lag) > SINGLE_CAMERA_TOL_S):
                continue
            text = f"{cam} 比动作{'早' if lag < 0 else '晚'} {abs(lag):.2f} 秒" if lag is not None else f"{cam} 与动作错位"
            out.add(m, "camera_misaligned", text,
                    scope={"camera": cam}, readings={"lag_s": _r(lag), "corr_peak": _r(c.get("corr_peak")),
                                                     "basis": (str(c.get("basis") or "whole") if whole else "windowed_median")})
        suspects = sorted(str(c) for c in d.get("suspect_cameras") or [])
        if suspects:
            # the same words as the other AV-1 texts (2026-10-13): the camera, its whole-episode reading (not trusted),
            # its windowed median when it has one
            def _suspect(cam: str) -> str:
                c = per.get(cam) if isinstance(per.get(cam), dict) else {}
                lag = _num(c.get("lag_s")); lw = c.get("lag_windows") if isinstance(c.get("lag_windows"), dict) else {}
                wl = _num(lw.get("lag_s"))
                t = f"{cam} 疑似错位：整条读数 {lag:+.2f} 不可信" if lag is not None else f"{cam} 疑似错位：整条读数不可信"
                return t + (f"，分窗 {wl:+.2f}" if wl is not None else "")
            out.add(m, "suspect", "；".join(_suspect(c) for c in suspects),
                    scope={"cameras": suspects},
                    readings={"lag_s": {c: _r((per.get(c) or {}).get("lag_s")) for c in suspects}})
    weak = sorted(cam for cam, c in per.items() if isinstance(c, dict) and c.get("code") == "low_corr")
    if weak:
        # 相似度 is the word the diagnoses use for the correlation peak (no 相关 in a customer's text)
        out.add(m, "undecidable", "；".join(
            f"{c} 测不准：画面运动和动作对不上号" + (f"（相似度 {_num((per.get(c) or {}).get('corr_peak')):.2f}）"
                                             if _num((per.get(c) or {}).get("corr_peak")) is not None else "") for c in weak),
                scope={"cameras": weak},
                readings={"corr_peak": {c: _r((per.get(c) or {}).get("corr_peak")) for c in weak}})
    trusted = {cam: _num(c.get("lag_s")) for cam, c in per.items()
               if isinstance(c, dict) and c.get("trusted") and _num(c.get("lag_s")) is not None}
    if len(trusted) >= 2:
        lo, hi = min(trusted.values()), max(trusted.values())
        if hi - lo > p["spread_tol_s"]:
            cams = sorted(trusted)
            out.add(m, "lag_inconsistent", _unsync_text({c: trusted[c] for c in cams}),
                    scope={"cameras": cams}, readings={f"lag_{c}_s": _r(trusted[c]) for c in cams})
    # 借峰检验(MV-3，2026-10-13)：一路可信地错位给出 Δ 后，测不准的路不必自己定峰——它的相关曲线
    # 在 Δ 处若明显低于曲线最高值，Δ 就不是这一路的解释，两路不一致。比值门槛沿用单路峰可信度的
    # 1.25（global_lag 的 min_peak_ratio：并列双峰的比值落在 1.0～1.1，0.25 的余量挡伪冠军）。
    # 没信号的路（low_corr / no_motion）曲线是噪声，不参与；Δ 落在曲线高处答不了，记为无法核实。
    # 来历：Qian 的 MV-3 合成样本 ep15（wrist 可信 −0.97 s，front 峰不尖却在 0 处明显更高）漏报。
    if verdict != "misaligned_all":
        refs = {cam: lag for cam, lag in trusted.items() if (per.get(cam) or {}).get("code") == "misaligned"}
        unverified = []
        for cam, c in sorted(per.items()):
            if not isinstance(c, dict) or c.get("trusted") or c.get("code") in ("low_corr", "no_motion") or not refs:
                continue
            curve = c.get("xc_curve")
            hit = False
            for ref, delta in sorted(refs.items()):
                at = _curve_at(curve, delta)
                if at is None:
                    continue
                mx = max(float(x) for x in curve["corr"])
                ratio = mx / max(at, 1e-6)
                if ratio >= BORROW_PEAK_RATIO:
                    best = _best_readings(per, p)
                    out.add(m, "lag_inconsistent", _unsync_text({c: best.get(c, (None,))[0] for c in (cam, ref)}),
                            scope={"cameras": sorted([ref, cam])},
                            readings={"ref_camera": ref, "lag_ref_s": _r(delta), "camera": cam,
                                      "corr_at_ref_lag": _r(at), "corr_max": _r(mx), "ratio": _r(ratio)})
                    hit = True
                    break
            if not hit and refs:
                unverified.append(cam)
        if unverified and refs:
            out.readings["mv3_unverified_cameras"] = unverified
    # 分窗中位数之差(MV-3，2026-10-13)：整条读数在远机位上峰宽 0.5～1.4 s，0.2 s 的错开读不出；每路
    # 分窗取可信窗的中位数后比两路之差，门槛取 3 帧（max(0.1 s, 3·dt)），只在两路都「稳」时比。
    # 「稳」= 可信窗 ≥ 3 且四分位距 ≤ 0.15 s：12 个数据集 82 条的扫描里（2 s 窗），这组条件下没错开的条目
    # 31 对两路都稳、零误报，两路之差 p90 0.07 s；IQR 放到 0.3 就出 2 条误报。错开 0.2 s 的注入条目报出 3/5，
    # 漏的两条是腕部相机本身不稳（IQR ≥ 0.4）。比整条读数的 0.3 s 规则细，两条规则各出各的发现，同一条最多报一次。
    # 「稳」的两个门槛与差的门槛是任务的模块参数(registry 8.1);稳不稳的判断本身在 windows_stable,报告层与界面同用。
    # 每路读数的 lag_windows.stable 随读数落盘:界面取校正量、报告算覆盖率都读它,不在别处重算这条规则。
    from ..core.checks.video_action_sync import windows_stable

    min_trusted, max_iqr = int(p["window_min_trusted"]), float(p["window_max_iqr_s"])
    solid = {}
    for cam, c in per.items():
        lw = c.get("lag_windows") if isinstance(c, dict) else None
        if not isinstance(lw, dict) or cam not in out.readings["per_camera"]:
            continue
        stable = windows_stable(lw, min_trusted=min_trusted, max_iqr_s=max_iqr)
        out.readings["per_camera"][cam]["lag_windows"]["stable"] = stable
        if stable:
            solid[cam] = (_num(lw.get("lag_s")), _num(lw.get("dt_s")) or 0.0)
    if verdict != "misaligned_all" and not any(f["code"] == "lag_inconsistent" for f in out.findings):
        if len(solid) >= 2:
            lags_w = {cam: v[0] for cam, v in solid.items()}
            tol = max(float(p["window_spread_tol_s"]), WINDOW_SPREAD_FRAMES * max(v[1] for v in solid.values()))
            lo_c, hi_c = min(lags_w, key=lags_w.get), max(lags_w, key=lags_w.get)
            spread = lags_w[hi_c] - lags_w[lo_c]
            if spread > tol:
                cams = sorted(lags_w)
                out.add(m, "lag_inconsistent", _unsync_text(lags_w),
                        scope={"cameras": cams},
                        readings={"basis": "windowed_median", "spread_s": _r(spread), "tol_s": _r(tol),
                                  **{f"lag_{c}_s": _r(lags_w[c]) for c in cams}})
    return out


#: 借峰检验的比值门槛，与 global_lag 的 min_peak_ratio 同源（见 _video_action_sync 末尾的说明）
BORROW_PEAK_RATIO = 1.25
def _best_readings(per: dict, p: dict) -> dict:
    """每路相机最好的读数（整条可信 → 整条；否则分窗稳 → 分窗；都没有 → None），给发现文案用：
    人要看的是每路错开多少、两路相差多少，不是哪条规则报的（2026-10-13 用户点名 ep15 读不出来）。"""
    from ..core.checks.video_action_sync import windows_stable

    out = {}
    for cam, c in sorted(per.items()):
        if not isinstance(c, dict):
            continue
        lag = _num(c.get("lag_s"))
        if c.get("trusted") and lag is not None and c.get("code") in ("aligned", "misaligned"):
            out[cam] = (lag, str(c.get("basis") or "whole"), c.get("code"))
            continue
        if c.get("instability"):
            # 2026-10-14:整段与逐窗各自稳定却不同、整段峰靠一小段数据撑着、各肢体读数矛盾——这一路没有「最好读数」,
            # 分窗中位数也不单独拿出来报
            out[cam] = (None, None, None)
            continue
        lw = c.get("lag_windows")
        if windows_stable(lw, min_trusted=int(p["window_min_trusted"]), max_iqr_s=float(p["window_max_iqr_s"])) \
                and _num(lw.get("lag_s")) is not None:
            out[cam] = (_num(lw.get("lag_s")), "windowed", None)
        else:
            out[cam] = (None, None, None)
    return out


def _unsync_text(lags: dict) -> str:
    """The one wording of every MV-3 finding (2026-10-13, the user's): 「两路相机不同步：wrist −0.97，front +0.03，相差
    1.00 秒」 - each camera's reading (None: 测不准) and the spread; which rule read it stays in the readings."""
    cams = sorted(lags)
    nums = [v for v in lags.values() if v is not None]
    text = ("两路相机不同步：" if len(cams) == 2 else f"{len(cams)} 路相机不同步：") + "，".join(
        f"{c} {lags[c]:+.2f}" if lags[c] is not None else f"{c} 测不准" for c in cams)
    if len(nums) >= 2:
        text += f"，相差 {max(nums) - min(nums):.2f} 秒"
    return text


#: 单路错位的容差（秒），与整条规则 global_lag 的 lag_tol_s 默认值相同（≈ 2～3 帧，P5.1 校准）；分窗读数超过它就报 AV-1
SINGLE_CAMERA_TOL_S = 0.25

#: 分窗中位数之差的门槛下限（帧）：互相关的滞后分辨率是一个采样周期，3 帧与整条规则的容差下限（2 帧）同源、
#: 再留一帧给两路各自的量化误差；秒数门槛与「稳」的两个条件是模块参数（registry 8.1），来历见 _video_action_sync
WINDOW_SPREAD_FRAMES = 3


def _curve_at(curve, lag_s: float):
    """The correlation curve's value at ``lag_s`` (linear interpolation); None when there is no usable curve
    or the lag lies outside the scanned window."""
    if not isinstance(curve, dict):
        return None
    lags, corr = curve.get("lags_s"), curve.get("corr")
    if not lags or not corr or len(lags) != len(corr) or len(lags) < 2:
        return None
    try:
        xs = [float(x) for x in lags]
        ys = [float(y) for y in corr]
        x = float(lag_s)
    except (TypeError, ValueError):
        return None
    if x < xs[0] or x > xs[-1]:
        return None
    for i in range(1, len(xs)):
        if xs[i] >= x:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


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
    approx = str(d.get("quality") or "") == "approximate"      # 速度限值是经验包络,不许硬判(2026-10-14 so101)
    for (kind, joint), vs in sorted(groups.items()):
        frames = sorted({int(v.get("frame", 0)) for v in vs})
        worst = max((abs(_num(v.get("value")) or 0.0) for v in vs), default=None)
        limit = vs[0].get("limit")
        channel = None if joint in ("", "xyz", "rpy") else f"joint {joint}"
        where = f"关节 {joint}" if channel else ("末端位置" if joint == "xyz" else "末端姿态" if joint == "rpy" else "")
        code, name = kind, _KIN_NAMES[kind]
        if kind == "velocity_limit" and approx:
            code, name = "velocity_limit_approx", " 速度超出近似限值"
        out.add(m, code, f"{where}{name}：{len(vs)} 处，首次在第 {frames[0] + 1} 帧，"
                         f"最大 {worst:.4g}（限值 {limit}{'，规格待核' if code == 'velocity_limit_approx' else ''}）"
                if worst is not None else f"{where}{name}",
                scope={"channel": channel} if channel else None,
                readings={"frames": frames[:20], "max_value": _r(worst), "limit": limit})
    # the command (a leader arm, a joystick) moved faster than the robot may: a note, the robot's own reading decides
    cmd: dict[str, list[dict]] = {}
    for v in d.get("command_velocity_violations") or []:
        if isinstance(v, dict):
            cmd.setdefault(str(v.get("joint", "")), []).append(v)
    for joint, vs in sorted(cmd.items()):
        frames = sorted({int(v.get("frame", 0)) for v in vs})
        worst = max((abs(_num(v.get("value")) or 0.0) for v in vs), default=0.0)
        tail = "读数未超" if d.get("basis") == "state" else "无读数可核"
        out.add(m, "command_velocity_limit",
                f"关节 {joint} 指令速度超限：{len(vs)} 处，首次在第 {frames[0] + 1} 帧，最大 {worst:.4g}（限值 {vs[0].get('limit')}；{tail}）",
                scope={"channel": f"joint {joint}"},
                readings={"frames": frames[:20], "max_value": _r(worst), "limit": vs[0].get("limit")})
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
                "idle_mid_count", "idle_mid_total_s",
                # per column (2026-10-14): which column the smoothness came from, and the switch-like columns left out
                "smoothness_cols", "smoothness_min_col", "discrete_cols", "velocity_cols", "nonjoint_cols", "jagged_amp"):
        if d.get(key) is not None:
            out.readings[key] = _r(d.get(key))
    _sub_score(out, m, "smoothness_low", d.get("smoothness"), p["smoothness_min"], "动作不平滑，平滑度分",
               high_below=p["severe_below"],
               readings={"smoothness_cols": d.get("smoothness_cols")} if isinstance(d.get("smoothness_cols"), dict) else None)
    for s in d.get("jagged") or []:
        if not isinstance(s, dict) or s.get("start") is None:
            continue
        a, b = int(s["start"]), int(s["end"])
        which = "读数" if s.get("stream") == "state" else "指令"
        out.add(m, "jagged", f"第 {int(s['col'])} 列的{which}在第 {a + 1}–{b + 1} 帧来回抖动：每帧都反向，"
                             f"幅度有这列整条变化范围的 {100 * float(s.get('amp_rel') or 0.0):.0f}%",
                scope={"channel": f"col {int(s['col'])}"}, frames=(a, b),
                readings={"amp_rel": _r(s.get("amp_rel")), "step_corr": _r(s.get("step_corr")), "stream": s.get("stream")})
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
    for key, item, why_key in (("smoothness", "ACT-1", "smoothness_reason"), ("spike", "ACT-2", "spike_reason"),
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

def _evidence_span(d: dict) -> tuple[dict | None, list[dict]]:
    """The primary answer's evidence (video protocol): its first stretch and all of them."""
    items = [e for e in ((d.get("video_assessment") or {}).get("evidence") or []) if isinstance(e, dict)
             and _num(e.get("start_s")) is not None and _num(e.get("end_s")) is not None]
    return (items[0] if items else None), items


@deriver("task_success", fail="failure", defer="uncertain")
def _task_success(passed, score, d, p) -> Derived:
    """The one video judgement (D71): failure or an abstention; an episode without a task text is not judged
    (D72, LABEL-2). The completion estimate is a reading; the primary answer's evidence stretch is the failure's
    interval (P19)."""
    out = Derived()
    m = "task_success"
    if d.get("skipped") == "no_task_text":
        # D72: the episode has no task text, so there was no judgement to derive from
        text = "没有任务标注，没有做任务成败判定"
        out.add(m, "task_text_missing", text, readings={"task_text_source": "无"})
        out.cannot("TASK-4", "no_task_text", text)
        return out
    completion = d.get("video_completion")
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
        out.add(m, "uncertain", "任务成败拿不准" + (f"：{reason[:200]}" if reason else ""))
    if not str(d.get("task_desc_source") or ""):
        out.cannot("LABEL-2", "not_applicable", "记录里没有任务描述的来源")
    return out


#: camera_defects' answer levels -> a finding's severity (design doc 17 §2.2: minor -> low, severe -> medium)
CAMERA_LEVELS = {"minor": "low", "severe": "medium"}
_CAMERA_ITEMS = (("glitch", "IMG-5", "花屏"), ("shake", "IMG-6", "相机抖动"), ("contamination", "IMG-7", "镜头污染"),
                 ("occlusion", "IMG-8", "视觉遮挡"))
# the kinds of IMG-7: what is ON the lens. "obstruction" is something stuck to it, not IMG-8's occlusion
_CONTAMINATION = {"dirt": "脏污", "smudge": "污渍", "water": "水滴", "obstruction": "糊住镜头", "other": "其他"}
_OCCLUSION = {"arm": "机械臂", "body": "机身", "person": "人", "object": "物件", "cable": "线缆", "other": "其他"}
_VIEW_KIND = {"wrist": "腕部相机", "fixed": "固定机位"}
_VIEW_ARM = {"left": "左臂", "right": "右臂", "single": "单臂"}
_REGION = {"left": "画面左侧", "right": "画面右侧", "top": "画面上方", "bottom": "画面下方",
           "center": "画面中央", "whole": "整幅画面"}


@deriver("camera_defects")
def _camera_defects(passed, score, d, p) -> Derived:
    """Per camera and item, a minor or severe answer is a finding with the stretches the model named; an
    item no camera answered could not be assessed (``model_no_answer``). The camera identity (MV-2) is
    one finding per camera whose picture contradicts its name or duplicates another camera."""
    out = Derived()
    m = "camera_defects"
    if d.get("clean_ratio") is not None:
        out.readings["clean_ratio"] = _r(d.get("clean_ratio"))
    per = d.get("per_camera") if isinstance(d.get("per_camera"), dict) else {}
    items = d.get("items") if isinstance(d.get("items"), dict) else {}
    if not per:
        why = str(d.get("reason") or "没有逐机位复核的回答")
        reason = "video_undecodable" if d.get("undecodable") else "model_no_answer"     # D93
        for _, item, _ in _CAMERA_ITEMS:
            out.cannot(item, reason, why[:200])
        # the arm pairing is this module's own measurement: it stands even when the request that
        # carried the picture questions brought nothing back
        _camera_identity(out, m, d, {})
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
            if key == "occlusion" and entry.get("source") in _OCCLUSION and entry.get("source") != "other":
                label = f"被{_OCCLUSION[entry['source']]}遮挡"
            note = str(entry.get("note") or "").strip()
            extra = {}
            if key == "contamination":
                extra["kind"] = entry.get("kind")
            where = ""
            if key == "occlusion":
                extra["source"] = entry.get("source")
                for name in ("coverage", "duration_s", "share"):       # how much, how long, what share
                    if isinstance(entry.get(name), (int, float)):
                        extra[name] = _r(entry[name])
                extra["region"] = entry.get("region")
                parts = []
                if isinstance(entry.get("coverage"), (int, float)):
                    parts.append(f"遮住 {round(float(entry['coverage']) * 100)}% 画面")
                if entry.get("region") in _REGION:
                    parts.append(_REGION[entry["region"]])
                if isinstance(entry.get("duration_s"), (int, float)) and entry["duration_s"] > 0:
                    parts.append(f"共 {_r(entry['duration_s'])} 秒")
                where = f"，{'、'.join(parts)}" if parts else ""
            out.add(m, key, f"{short_camera(cam)} {label}（{'严重' if level == 'severe' else '轻微'}）{where}"
                            + (f"：{note[:120]}" if note else ""),
                    severity=CAMERA_LEVELS[level], scope={"camera": short_camera(cam)},
                    time_s=(float(times[0][0]), float(times[0][1])) if times else None,
                    readings={"level": level, "times": times[:8], **extra})
    _camera_identity(out, m, d, per)
    return out


def _camera_identity(out: Derived, m: str, d: dict, per: dict) -> None:
    """MV-2: which arm each camera measurably rides (the reading no single camera can give) and what
    each camera's own picture says it is, against what the dataset declares for it."""
    arms = d.get("arms") if isinstance(d.get("arms"), dict) else {}
    if str(d.get("identity") or "unknown") == "unknown":
        why = str(arms.get("note") or "").strip()
        # which half was missing: the model's mount comparison, or the measurement that was taken
        # and did not settle it. Never "the model did not answer" about a reading nobody asked it for.
        cause = str(arms.get("reason") or "") or ("video_undecodable" if d.get("undecodable")
                                                  else "model_no_answer")
        out.cannot("MV-2", cause,
                   "没有一路相机的画面被对照过数据集的说明，也没有测出左右臂是否串线"
                   + (f"（{why[:120]}）" if why else ""))
    if arms.get("identity") == "crossed":
        crossed = [short_camera(c) for c in (arms.get("crossed") or []) if isinstance(c, str)]
        note = str(arms.get("note") or "").strip()
        # the note names each camera and the arm it rides, so the headline does not list them again
        out.add(m, "identity", "左右臂、机位身份串线：" + (note[:160] if note else
                               f"{'、'.join(crossed) or '未指明相机'} 骑的不是数据集给它的那条臂"),
                severity="medium", scope={"cameras": crossed} if crossed else None,
                readings={"arms": "crossed", "crossed": crossed,
                          "basis": arms.get("basis") or None,
                          "margin": (arms.get("readings") or {}).get("margin")})
    for cam, e in sorted((per or {}).items()):
        view = (e or {}).get("view") or {}
        what = "、".join(x for x in (_VIEW_KIND.get(view.get("kind")), _VIEW_ARM.get(view.get("arm"))) if x)
        note = str(view.get("note") or "").strip()
        if view.get("matches_name") == "no":
            out.add(m, "identity", f"{short_camera(cam)} 的画面与数据集的说明不符" + (f"（画面是{what}）" if what else "")
                                   + (f"：{note[:120]}" if note else ""),
                    severity="medium", scope={"camera": short_camera(cam)},
                    readings={"kind": view.get("kind"), "arm": view.get("arm")})


@deriver("eef_video_consistency")
def _eef_video_consistency(passed, score, d, p) -> Derived:
    """Design doc 25 §7 (D81): the merged cells' opinions - ``inconsistent`` (info) per sub-item and camera at
    "possibly" or above, ``conflict`` (review, ``eef_check``) where the two channels say opposite things,
    ``ego_motion_suspect`` for a wrist camera's own motion - and the comparison with the dataset's own record,
    reported only. A record without ``merged`` (the module said nothing on the episode) has no finding."""
    if isinstance(d.get("merged"), dict):
        return _eef_merged(d)
    out = Derived()
    _ego_motion(out, d)
    _eef_record(out, d)
    return out


def _eef_record(out: Derived, d: dict) -> None:
    """The upload against the dataset's own record (design doc 12 §8.7): reported only."""
    record = d.get("record") if isinstance(d.get("record"), dict) else {}
    if record.get("status") == "suspect":
        if record.get("source") == "internal":       # the trajectory is the record's own (design doc 25 §6.1)
            internal = record.get("internal") or {}
            p95 = ((internal.get("position_mm") or {}).get("p95"))
            out.add("eef_video_consistency", "record_mismatch",
                    "数据集的末端位姿列与关节角正解不一致" + (f"（位置差 p95 {p95:.1f} mm）" if isinstance(p95, (int, float)) else ""),
                    readings={"status": "suspect", "source": "internal", "reasons": list(record.get("reasons") or [])})
            return
        why = sorted({str(r) for src in (record.get("sources") or {}).values() if isinstance(src, dict)
                      for r in src.get("reasons") or []})
        out.add("eef_video_consistency", "record_mismatch",
                "上传的轨迹与数据集自己记录的末端位姿不一致" + (f"：{'、'.join(why)}" if why else ""),
                readings={"status": "suspect", "source": "upload", "reasons": why})


#: a merged cell's label -> its finding's severity (design doc 25 §7.3)
_EEF_SEVERITY = {"inconsistent": "high", "possibly_inconsistent": "medium"}


def _eef_merged(d: dict) -> Derived:
    out = Derived()
    m = "eef_video_consistency"
    merged = d["merged"]
    for cell in merged.get("cells") or []:
        if not isinstance(cell, dict) or cell.get("p") is None:
            continue
        flags = list(cell.get("flags") or [])
        label = str(cell.get("label") or "")
        conflict = "conflict" in flags
        if not conflict and label not in _EEF_SEVERITY:
            continue
        sub, cam = cell.get("subitem"), cell.get("camera")
        code = "conflict" if conflict else "ego_motion_suspect" if sub == "ego_motion" else "inconsistent"
        sources = {name: {k: src.get(k) for k in ("verdict", "p", "confidence", "why") if src.get(k) is not None}
                   for name, src in (cell.get("sources") or {}).items() if isinstance(src, dict)}
        readings = {"subitem": sub, "p": _r(cell.get("p"), 3), "label": label, "flags": flags, "sources": sources}
        for k in ("missing", "supported_hypotheses"):
            if cell.get(k):
                readings[k] = cell[k]
        kw: dict = {"severity": "medium" if conflict else _EEF_SEVERITY[label]}
        if cam:
            kw["scope"] = {"camera": str(cam)}
        span = cell.get("time_s")
        if isinstance(span, (list, tuple)) and len(span) == 2 and _num(span[0]) is not None and _num(span[1]) is not None:
            kw["time_s"] = (_num(span[0]), _num(span[1]))
        from ..extensions.eef_consistency import combine as CB

        out.add(m, code, CB.grounds(cell)[:300], readings=readings, **kw)
    episode = merged.get("episode") if isinstance(merged.get("episode"), dict) else {}
    if episode.get("p") is None:                    # no channel said anything: nothing was compared
        why = str(episode.get("reason") or (d.get("opinion") or {}).get("failure") or "没有任何渠道给出结论")
        out.cannot("MV-4", "not_applicable", why[:200])
    _ego_motion(out, d)                             # AV-1 without a timed reading (the finding is the cell's)
    _eef_record(out, d)
    return out


def _ego_motion(out: Derived, d: dict) -> None:
    """A wrist camera's own motion against the recorded poses (design doc 22 §5.3): its time offset reading is the
    episode's AV-1, unassessable without one; the finding itself is the merged cell's (design doc 25 §7)."""
    ego = d.get("ego_motion") if isinstance(d.get("ego_motion"), dict) else {}
    cams = ego.get("cameras") if isinstance(ego.get("cameras"), dict) else {}
    timed = [c for c in cams.values() if isinstance(c, dict) and c.get("status") in ("ok", "suspect") and c.get("lag")]
    if not timed:
        out.cannot("AV-1", "not_applicable", "没有腕部相机自运动的时间读数（只有带位姿的腕部相机才有）")


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
    # FILE-7 (registry 8.3) needs a size to compare: the record carries the one measured per camera,
    # and without it - no video, a file that would not open, a format with no declared size - the
    # item reads unassessed rather than clean
    if not d.get("resolution"):
        out.cannot("FILE-7", "no_video", "没有量到任何一路相机的画面尺寸")
    else:
        out.readings["resolution"] = d.get("resolution")
    return out


# ---------------------------------------------------------------- dataset level

@deriver("duplicate_check", fail="duplicate")
def _duplicate(passed, score, d, p) -> Derived:
    """An episode identical to others of the dataset (D87, design doc 26 §1): every member of the group names the
    others (``same_as``); the group is its lowest member. Reported only - nothing is kept first or dropped."""
    out = Derived()
    same = sorted(int(e) for e in d.get("same_as") or [])
    if passed is False and same:
        names = "、".join(f"ep{e:06d}" for e in same)
        out.add("duplicate_check", "duplicate", f"与 {names} 完全相同（动作与各路画面字节一致）",
                readings={"group_id": int(d.get("group_id", min(same))), "same_as": same})
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
    timestamps' duration outliers, the action semantics nobody could settle, the EEF cameras whose calibration
    is more likely wrong than their episodes (design doc 25 §7.4). Every finding carries ``unit: dataset``."""
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
            bad = next((s["nonjoint_columns"] for s in sem if s.get("nonjoint_columns")), None)
            if bad:
                found.append(finding(module, "columns_not_joints",
                                     f"{len(bad)} 列名字是关节，数值却是 0～1 的开合量（{'、'.join(bad)}）。"
                                     "这几列不进平滑度与同步检查，它们名下的关节在记录里没有读数",
                                     unit="dataset", readings={"columns": list(bad)}))
    elif module == "eef_video_consistency":
        from ..extensions.eef_consistency import calibration

        for s in calibration.suspects(ok):
            found.append(finding(module, "calibration_suspect", s["message"], scope={"camera": s["camera"]},
                                 unit="dataset", readings=s["readings"]))
    return found, readings
