"""The EEF module's channels: each gives, per sub-item and camera it can look at, a verdict and a confidence
(design doc 25 §6, D82).

Four channels: the CPU measurement (a third-person camera with a gripper reference, design doc 12 §8), a wrist
camera's own motion (design doc 22 §5.3), the model reviewing the CPU's windows (design doc 12 §10) and the
model's opinion on a whole marked clip (design doc 12 §10.5, design doc 20). A channel's ``verdict`` is ``issue``,
``ok`` or ``cannot_tell``; its ``confidence`` (0-1) is how much the evidence says so, and both become one
"inconsistency confidence" ``p`` aligned with the labels' low band L (design doc 25 §7.2): ``issue`` -> L + (1 - L) c
(at least "possibly inconsistent"), ``ok`` -> L (1 - c) (never that far; an ``ok`` with no confidence says nothing,
``cannot_tell``), ``cannot_tell`` -> none. The merge recomputes p with the task's own band. (v1.0 wrote ``issue`` -> c
and ``ok`` -> 1 - c: a CPU suspect at its own threshold came out "consistent"; a middle point of 0.5 put a weak ``ok`` -
two of them merged by the largest - at "possibly inconsistent", seen on dataset2's orientation; review rounds.)

A cell is keyed ``(sub_item, camera)``; the recorded state's motion has no camera (``None``). Nothing here reads
a video or asks a model: channels read the records the module already made.
"""
from __future__ import annotations

from typing import Any

from . import contracts as C

ISSUE, OK, CANNOT_TELL = "issue", "ok", "cannot_tell"
#: channel ids, as the records and the report name them
CPU, EGO, VLM_REVIEW, VLM_OPINION = "cpu", "ego", "vlm_review", "vlm_opinion"
#: the CPU side and the model side of a cell (design doc 25 §7.1 merges one of each)
CPU_SIDE, VLM_SIDE = (CPU, EGO), (VLM_REVIEW, VLM_OPINION)
#: the sub-items the model votes on in the review windows (position, orientation)
REVIEWED = (C.POSITION, C.ORIENTATION)
#: per camera, what the CPU measures on a third-person camera
CAMERA_ITEMS = (C.POSITION, C.ORIENTATION, C.TEMPORAL, C.CAMERA_MOTION)
#: the answer field of a review window per sub-item
REVIEW_FIELD = {C.POSITION: "position_support", C.ORIENTATION: "orientation_support"}
#: an opinion segment's aspect -> the sub-items it speaks for ("action": the motion's timing against the video)
ASPECT_ITEMS = {"position": (C.POSITION,), "orientation": (C.ORIENTATION,), "both": (C.POSITION, C.ORIENTATION),
                "action": (C.TEMPORAL,)}
#: what a whole-clip opinion covers: the arm prompt asks about the centre, the direction and the timing; the
#: handheld gripper's (umi-action-prompt/9) about the approach axis only
OPINION_ITEMS, UMI_OPINION_ITEMS = (C.POSITION, C.ORIENTATION, C.TEMPORAL), (C.ORIENTATION,)
#: a reading this many times its open threshold is full strength (0 at the threshold, linear between)
FULL_AT = 3.0
#: the labels' low band the p of a verdict is aligned with (the demo profile's; ``combine`` passes the task's)
LOW = 0.4


def p_of(verdict: str, confidence: float | None, low: float = LOW) -> float | None:
    """The inconsistency confidence of a verdict: an ``issue`` from the low band up, an ``ok`` below it, as far as
    the evidence goes; an ``ok`` with no confidence at all says nothing."""
    if verdict == CANNOT_TELL or confidence is None:
        return None
    c = min(1.0, max(0.0, float(confidence)))
    if verdict == ISSUE:
        return round(low + (1.0 - low) * c, 3)
    return None if c <= 0.0 else round(low * (1.0 - c), 3)


def judgement(verdict: str, confidence: float | None = None, *, why: str | None = None, **extra) -> dict[str, Any]:
    """One channel's word on one cell: verdict, confidence, ``p`` and what it rests on."""
    out: dict[str, Any] = {"verdict": verdict, "confidence": None if verdict == CANNOT_TELL else
                           round(min(1.0, max(0.0, float(confidence or 0.0))), 3)}
    out["p"] = p_of(verdict, out["confidence"])
    if why:
        out["why"] = why
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def strength(reading: float | None, threshold: float | None, full_at: float = FULL_AT) -> float:
    """How far past its open threshold a reading is: 0 at the threshold, 1 at ``full_at`` times it."""
    if reading is None or not threshold:
        return 0.0
    r = abs(float(reading)) / float(threshold)
    return min(1.0, max(0.0, (r - 1.0) / max(full_at - 1.0, 1e-9)))


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _worst_segment(segments: list[dict], camera: str | None, sub: str) -> dict | None:
    hits = [s for s in segments or [] if s.get("subitem") == sub and (camera is None or s.get("camera_id") == camera)]
    return max(hits, key=lambda s: _num(s.get("peak")) or 0.0) if hits else None


# ---------------------------------------------------------------- the CPU measurement (third-person cameras)

def _cpu_cell(sub: str, cell: dict, segments: list[dict], camera: str | None, prof, full_at: float) -> dict:
    """The CPU's word on one sub-item of one camera (or the recorded state's motion)."""
    status = (cell or {}).get("status")
    reasons = list((cell or {}).get("reasons") or [])
    if status not in (C.SUSPECT, C.OK):
        return judgement(CANNOT_TELL, why=(reasons[0] if reasons else (status or "not_measured")))
    seg = _worst_segment(segments, camera, sub)
    span = {"time_s": [seg["start_s"], seg["end_s"]], "evidence_frames": list(seg.get("evidence_frames") or [])} \
        if seg is not None and seg.get("start_s") is not None else {}
    reading, threshold, cover = None, None, 1.0
    if sub == C.POSITION:
        pts = (cell.get("points") or {}).values()
        pp = getattr(prof, "position", {}) or {}
        best = None
        for pt in pts:
            if pt.get("status") not in (C.SUSPECT, C.OK):
                continue
            m = pt.get("metrics") or {}
            r = max(_num(m.get("median_px")) or 0.0, 0.0) / float(pp.get("median_px") or 1e9)
            if seg is not None:
                r = max(r, (_num(seg.get("peak")) or 0.0) / float(pp.get("on_px") or 1e9))
            cov = _num(((pt.get("coverage") or {}).get("coverage"))) or 0.0
            key = (pt.get("status") == C.SUSPECT, r if status == C.SUSPECT else cov)
            if best is None or key > best[0]:
                best = (key, r, cov, m)
        if best is not None:
            _, r, cover, m = best
            reading, threshold = r, 1.0
            span.setdefault("readings", {"median_px": m.get("median_px"), "p95_px": m.get("p95_px")})
    elif sub == C.ORIENTATION:
        po = getattr(prof, "orientation", {}) or {}
        best = None
        for ax in (cell.get("axes") or {}).values():
            if ax.get("status") not in (C.SUSPECT, C.OK):
                continue
            m = ax.get("metrics") or {}
            r = (_num(m.get("median_deg")) or 0.0) / float(po.get("median_deg") or 1e9)
            if seg is not None:
                r = max(r, (_num(seg.get("peak")) or 0.0) / float(po.get("on_deg") or 1e9))
            cv = ax.get("coverage") or {}
            cov = (cv.get("valid") or 0) / max(cv.get("requested") or 1, 1)
            key = (ax.get("status") == C.SUSPECT, r if status == C.SUSPECT else cov)
            if best is None or key > best[0]:
                best = (key, r, cov, m)
        if best is not None:
            _, r, cover, m = best
            reading, threshold = r, 1.0
            span.setdefault("readings", {"median_deg": m.get("median_deg"), "p95_deg": m.get("p95_deg")})
    elif sub == C.TEMPORAL:
        m = cell.get("metrics") or {}
        reading = _num(m.get("lag_frames"))
        threshold = (getattr(prof, "temporal", {}) or {}).get("min_lag_frames")
        span.setdefault("readings", {"lag_frames": m.get("lag_frames"), "lag_s": m.get("lag_s")})
    elif sub == C.CAMERA_MOTION:
        m = cell.get("metrics") or {}
        reading = (_num(seg.get("peak")) if seg is not None else None) or _num(m.get("hf_rms_max_px"))
        threshold = (getattr(prof, "camera_motion", {}) or {}).get("hf_on_px")
        span.setdefault("readings", {"hf_rms_max_px": m.get("hf_rms_max_px")})
    elif sub == C.STATE_MOTION:
        m = cell.get("metrics") or {}
        ps = getattr(prof, "state_motion", {}) or {}
        r1 = (_num(m.get("hf_pos_rms_max_mm")) or 0.0) / float(ps.get("hf_on_mm") or 1e9)
        r2 = (_num(m.get("spike_count")) or 0.0) / float(ps.get("spikes_suspect") or 1e9)
        reading, threshold = max(r1, r2), 1.0
        span.setdefault("readings", {"hf_pos_rms_max_mm": m.get("hf_pos_rms_max_mm"), "spike_count": m.get("spike_count")})
    if status == C.OK:
        return judgement(OK, cover, coverage=round(cover, 3), **span)
    s = strength(reading, threshold, full_at)
    return judgement(ISSUE, s * cover, why=(reasons[0] if reasons else None), strength=round(s, 3),
                     coverage=round(cover, 3), **span)


def cpu_channel(detail: dict | None, prof, *, full_at: float = FULL_AT) -> dict:
    """{(sub, camera): judgement} of the CPU measurement on the third-person cameras, and the recorded state."""
    out: dict = {}
    if not isinstance(detail, dict):
        return out
    segments = detail.get("segments") or []
    for cid, cam in (detail.get("cameras") or {}).items():
        cells = cam.get("subitems") or {}
        for sub in CAMERA_ITEMS:
            if sub in cells:
                out[(sub, cid)] = _cpu_cell(sub, cells[sub], segments, cid, prof, full_at)
    state = detail.get("state_motion")
    if isinstance(state, dict) and state.get("status") is not None:
        out[(C.STATE_MOTION, None)] = _cpu_cell(C.STATE_MOTION, state, segments, None, prof, full_at)
    return out


# ---------------------------------------------------------------- the model reviewing the CPU's windows

def review_channel(review: dict | None, detail: dict | None) -> tuple[dict, dict]:
    """({(sub, camera): judgement} of the model's review on position and orientation, {camera: tracking}).

    A sub-item the CPU found suspect is reviewed on its candidate windows (the stretches the CPU flagged); any
    other on the camera's other windows. Refutes outnumbering supports -> ``issue``, the other way -> ``ok``, a tie
    or no vote -> ``cannot_tell``; confidence = |refutes - supports| / answered windows x answered share. The
    ``tracking`` of a camera says whether most answering windows think the green cross follows something else."""
    out: dict = {}
    tracking: dict = {}
    if not isinstance(review, dict):
        return out, tracking
    cpu_cams = (detail or {}).get("cameras") or {}
    for cid, cam in (review.get("cameras") or {}).items():
        windows = [w for w in (cam or {}).get("windows") or [] if isinstance(w, dict)]
        answered = [w for w in windows if w.get("status") == "answered" and isinstance(w.get("answer"), dict)]
        t = {"support": 0, "refute": 0}
        for w in answered:
            said = w["answer"].get("tracking_target_correct")
            if said in t:
                t[said] += 1
        tracking[cid] = {"votes": t, "invalid": t["refute"] > t["support"]}
        cells = ((cpu_cams.get(cid) or {}).get("subitems") or {})
        for sub in REVIEWED:
            suspect = (cells.get(sub) or {}).get("status") == C.SUSPECT
            mine = [w for w in windows if (w.get("kind") == "candidate" and w.get("subitem") == sub) == suspect]
            got = [w for w in mine if w in answered]
            v = {"support": 0, "refute": 0}
            for w in got:
                said = w["answer"].get(REVIEW_FIELD[sub])
                if said in v:
                    v[said] += 1
            extra = {"votes": v, "answered": len(got), "asked": len(mine), "windows": "candidate" if suspect else "uniform"}
            if not mine:
                out[(sub, cid)] = judgement(CANNOT_TELL, why="not_asked", **extra)
            elif not got:
                out[(sub, cid)] = judgement(CANNOT_TELL, why="model_no_answer", **extra)
            elif v["support"] == v["refute"]:
                out[(sub, cid)] = judgement(CANNOT_TELL, why="no_vote" if not v["support"] else "tie", **extra)
            else:
                verdict = ISSUE if v["refute"] > v["support"] else OK
                conf = abs(v["refute"] - v["support"]) / len(got) * (len(got) / len(mine))
                out[(sub, cid)] = judgement(verdict, conf, **extra)
    return out, tracking


# ---------------------------------------------------------------- the model's opinion on a whole marked clip

def opinion_channel(opinion: dict | None, *, umi: bool = False) -> dict:
    """{(sub, camera): judgement} of the whole-clip opinion. A camera's p is the model's own mismatch confidence
    (the highest of its segments on the sub-item, 0 without one) pulled towards the middle by the share of its
    clips the model answered: p = 0.5 + (m - 0.5) x share."""
    out: dict = {}
    if not isinstance(opinion, dict):
        return out
    items = UMI_OPINION_ITEMS if umi else OPINION_ITEMS
    for cid, cam in (opinion.get("cameras") or {}).items():
        cam = cam or {}
        status = cam.get("status")
        if status in (None, "skipped"):
            continue                                   # not a camera the opinion looked at
        clips = [c for c in cam.get("clips") or [] if isinstance(c, dict)]
        answered = [c for c in clips if c.get("status") == "answered"]
        if not answered:
            for sub in items:
                out[(sub, cid)] = judgement(CANNOT_TELL, why="model_no_answer", clips=len(clips))
            continue
        share = len(answered) / max(len(clips), 1)
        segs = [s for s in cam.get("segments") or [] if isinstance(s, dict)]
        for sub in items:
            mine = [s for s in segs if sub in ASPECT_ITEMS.get(str(s.get("aspect")), ())]
            best = max(mine, key=lambda s: _num(s.get("confidence")) or 0.0) if mine else None
            m = (_num(best.get("confidence")) or 0.0) if best is not None else 0.0
            e = 0.5 + (m - 0.5) * share
            verdict = ISSUE if m > 0.5 else OK if m < 0.5 else CANNOT_TELL
            extra = {"max_confidence": round(m, 3) if best is not None else None, "share": round(share, 3),
                     "segments": len(mine)}
            if best is not None and best.get("start_s") is not None:
                extra.update(time_s=[best["start_s"], best["end_s"]], evidence_frames=list(best.get("evidence_frames") or []))
            out[(sub, cid)] = judgement(verdict, abs(2 * e - 1), **extra)
    return out


# ---------------------------------------------------------------- a wrist camera's own motion

def ego_channel(ego: dict | None, prof_ego: dict | None = None, *, full_at: float = FULL_AT) -> dict:
    """{(ego_motion, camera): judgement} of the wrist cameras' own motion against their poses."""
    from . import egomotion as EM

    out: dict = {}
    if not isinstance(ego, dict):
        return out
    cfg = {**EM.DEFAULTS, **(prof_ego or {})}
    for cid, cam in (ego.get("cameras") or {}).items():
        cam = cam or {}
        status = cam.get("status")
        cover = _num((cam.get("metrics") or {}).get("coverage"))
        cover = 1.0 if cover is None else cover
        if status == "ok":
            out[(C.EGO_MOTION, cid)] = judgement(OK, cover, coverage=round(cover, 3))
        elif status == "suspect":
            segs = [s for s in cam.get("segments") or [] if isinstance(s, dict)]
            worst = max(segs, key=lambda s: (_num(s.get("band_level")) or 0, _num(s.get("magnitude")) or 0)) if segs else None
            s_ = 0.0
            if worst is not None:
                if worst.get("reason") == "time_offset":
                    s_ = strength(_num(worst.get("lag_s")) or _num(worst.get("magnitude")), cfg.get("lag_min_s"), full_at)
                else:
                    s_ = strength(_num(worst.get("magnitude")), cfg.get("rotation_on_deg"), full_at)
            extra = {"strength": round(s_, 3), "coverage": round(cover, 3)}
            if worst is not None and worst.get("start_s") is not None:
                extra.update(time_s=[worst["start_s"], worst["end_s"]], evidence_frames=list(worst.get("evidence_frames") or []),
                             band=worst.get("band"), reason=worst.get("reason"))
            out[(C.EGO_MOTION, cid)] = judgement(ISSUE, s_ * cover, **extra)
        else:
            out[(C.EGO_MOTION, cid)] = judgement(CANNOT_TELL, why=cam.get("reason") or status or "not_measured")
    return out
