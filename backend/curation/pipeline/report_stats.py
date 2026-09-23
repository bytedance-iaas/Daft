"""Chart-ready aggregates of the report's module sections (design doc 06 §6.2, F6.2).

Every function here reads records the revision already has - ``details`` exactly as
the checks wrote them - and returns counts, means and small per-camera arrays, never
a list of episodes: the report page shows statistics only, one episode at a time is
the Episode tab's job (``GET /tasks/{id}/episodes/{index}``). Nothing here feeds a
verdict or a list; ``curation report`` writes what it returns into ``report.json``
and nothing reads it back.

Shapes, so the page can draw them without knowing a module:

* a series is ``[{name, count}]`` (bars); names are language-neutral codes (the page
  translates them) or the data's own labels (skill families, reason heads);
* per-camera rows are ``[{camera, ...}]`` with short camera names;
* scalars are plain numbers or strings.

Older details lack some keys; every reader tolerates that and simply counts less.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from statistics import mean, median

#: score_hist: ten bins over 0-1, "0.0–0.1" ... "0.9–1.0" (1.0 falls in the last one)
SCORE_BINS = 10
#: a camera reading below this is "low" (the mockup's 低于 0.6 的相机读数)
LOW_CAMERA_SCORE = 0.6
#: how many reason heads a series keeps; the rest are summed into OTHER
TOP_REASONS = 6
OTHER = "其它"
#: bin widths duration_hist may use, seconds
_NICE_WIDTHS = (0.5, 1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 1800, 3600)


# ---------------------------------------------------------------- helpers

def num(v) -> float | None:
    """A finite float, or None (bools, strings, NaN and missing values are not numbers)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def series(counts, order=(), *, keep_zero: bool = False) -> list[dict]:
    """``[{name, count}]``: the names of ``order`` first (with zeros when ``keep_zero``),
    then the rest by count, largest first, ties by name."""
    counts = Counter({str(k): int(v) for k, v in dict(counts).items()})
    out = [{"name": str(k), "count": counts.get(str(k), 0)} for k in order
           if keep_zero or counts.get(str(k), 0)]
    seen = {str(k) for k in order}
    rest = sorted(((k, v) for k, v in counts.items() if k not in seen and v),
                  key=lambda kv: (-kv[1], kv[0]))
    return out + [{"name": k, "count": v} for k, v in rest]


def top_series(counts: Counter, n: int = TOP_REASONS) -> list[dict]:
    """The ``n`` largest entries, the remainder summed into one ``其它`` entry."""
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    head = [{"name": str(k), "count": int(v)} for k, v in items[:n] if v]
    rest = sum(v for _, v in items[n:])
    return head + ([{"name": OTHER, "count": int(rest)}] if rest else [])


def score_bins(scores) -> list[int]:
    counts = [0] * SCORE_BINS
    for s in scores:
        f = num(s)
        if f is None:
            continue
        # the epsilon keeps 0.3 (0.30000000000000004 x 10) and friends in their own bin
        counts[min(SCORE_BINS - 1, max(0, int(f * SCORE_BINS + 1e-9)))] += 1
    return counts


def score_hist(scores) -> list[dict]:
    """Ten bins over 0-1 (``0.0–0.1`` ... ``0.9–1.0``), zeros kept: a stable axis."""
    return [{"name": f"{i / SCORE_BINS:.1f}–{(i + 1) / SCORE_BINS:.1f}", "count": n}
            for i, n in enumerate(score_bins(scores))]


def _trim(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def duration_hist(values, max_bins: int = 12) -> list[dict]:
    """Episode durations in round-width bins (``"10–15"`` seconds), at most ``max_bins``."""
    vals = [v for v in (num(x) for x in values) if v is not None and v >= 0]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    width, start, n = _NICE_WIDTHS[-1], 0.0, 1
    for w in _NICE_WIDTHS:
        start = math.floor(lo / w) * w
        n = max(1, math.ceil((hi - start) / w - 1e-9))
        width = w
        if n <= max_bins:
            break
    counts = [0] * n
    for v in vals:
        counts[min(n - 1, max(0, int((v - start) // width)))] += 1
    return [{"name": f"{_trim(start + i * width)}–{_trim(start + (i + 1) * width)}", "count": c}
            for i, c in enumerate(counts)]


def reason_head(text) -> str:
    """The category of a free-text reason: up to its first clause mark, numbers elided.

    ``末态物证 0.38 在灰区(0.25~0.45),证据不足以硬判`` -> ``末态物证 … 在灰区``: reasons
    that differ only in the numbers they quote fall into one bar.
    """
    s = str(text or "").strip()
    s = re.split(r"[(（:：,，;；。]", s, maxsplit=1)[0]
    s = re.sub(r"[-+]?\d+(?:\.\d+)?", "…", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:40] or "未注明"


def short_camera(name) -> str:
    """``observation.images.wrist`` -> ``wrist`` (the detail tables' camera names)."""
    return str(name).split(".")[-1]


def _details(rec: dict) -> dict:
    d = rec.get("details")
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- every module

def generic(records: list[dict], scores: list[float]) -> dict:
    """What any verdict module gets: the score distribution, why it abstained (reason
    heads), and which step failed for the episodes that wait for a retry."""
    out: dict = {}
    if scores:
        out["score_hist"] = score_hist(scores)
    heads = Counter(reason_head(_details(r).get("reason")) for r in records
                    if r.get("verdict") == "abstain")
    if heads:
        out["abstain_reason_counts"] = top_series(heads)
    steps: Counter = Counter()
    for r in records:
        if r.get("verdict") != "error":
            continue
        incidents = (r.get("error") or {}).get("incidents") if isinstance(r.get("error"), dict) else None
        names = {str(i.get("step") or "unknown") for i in incidents or [] if isinstance(i, dict)}
        for step in names or {"unknown"}:
            steps[step] += 1
    if steps:
        out["error_steps"] = series(steps)
    return out


# ---------------------------------------------------------------- timestamp_check

#: why a timestamp check failed, from the keys the check leaves in ``details``
TS_FAIL_KINDS = ("out_of_order", "gap", "fragment", "jitter")


def timestamp_fail_kind(d: dict) -> str:
    if d.get("gap_frames"):
        return "gap"                     # a jump: dropped frames
    if "jitter_ratio" in d:
        return "jitter"                  # too many irregular intervals
    if "ts" in d and "frame" in d:
        return "out_of_order"            # time went backwards or repeated
    if "duration_s" in d and "dt_nominal" not in d:
        return "fragment"                # shorter than the minimum duration
    return "other"                       # e.g. fewer than two timestamps


def timestamp_stats(records: list[dict]) -> dict:
    fails = Counter(timestamp_fail_kind(_details(r)) for r in records if r.get("verdict") == "fail")
    out: dict = {"fail_reasons": series(fails, TS_FAIL_KINDS, keep_zero=True)}
    durations = [v for v in (num(_details(r).get("duration_s")) for r in records) if v is not None]
    if durations:
        out.update({"duration_total_s": round(sum(durations), 1),
                    "duration_median_s": round(median(durations), 2),
                    "duration_min_s": round(min(durations), 2),
                    "duration_max_s": round(max(durations), 2),
                    "duration_hist": duration_hist(durations)})
    return out


# ---------------------------------------------------------------- kinematic_limits

def _joint_key(name: str):
    return (0, int(name), "") if name.lstrip("-").isdigit() else (1, 0, name)


def kinematic_stats(records: list[dict]) -> dict:
    """Episodes with a violation, by violation type and by joint (an episode counts once
    per type and per joint); the limits profile the check used."""
    by_type: Counter = Counter()
    by_joint: Counter = Counter()
    profiles: Counter = Counter()
    episodes = 0
    for r in records:
        d = _details(r)
        if d.get("profile"):
            profiles[str(d["profile"])] += 1
        v = [x for x in d.get("violations") or [] if isinstance(x, dict)]
        if r.get("verdict") == "error" or not v:
            continue
        episodes += 1
        for t in {str(x.get("type") or "other") for x in v}:
            by_type[t] += 1
        for j in {str(x.get("joint")) for x in v if x.get("joint") is not None and x.get("joint") != ""}:
            by_joint[j] += 1
    out: dict = {"violation_episodes": episodes, "violations_by_type": series(by_type),
                 "violations_by_joint": series(by_joint, sorted(by_joint, key=_joint_key))}
    if profiles:
        out["limits_profile"] = profiles.most_common(1)[0][0]
    return out


# ---------------------------------------------------------------- motion_quality

#: sub-scores in the order of the check; True = part of the total, False = reported only
MOTION_SUBSCORES = (("smoothness", True), ("spike", True), ("gripper_jitter", True),
                    ("actuator_saturation", True), ("path_efficiency", False),
                    ("joint_stability", False), ("fluency", False))
#: where a sub-score that could not be computed says why (motion_quality's details)
MOTION_NA_REASONS = {"actuator_saturation": "saturation_reason", "spike": "spike_reason",
                     "gripper_jitter": "gripper_reason", "fluency": "fluency_reason",
                     "stuck": "stuck_reason"}


def motion_stats(records: list[dict]) -> dict:
    judged = [r for r in records if r.get("verdict") != "error"]
    subs = []
    for key, in_total in MOTION_SUBSCORES:
        vals: list[float] = []
        na = 0
        why: Counter = Counter()
        for r in judged:
            d = _details(r)
            if key not in d:
                continue
            v = num(d.get(key))
            if v is None:
                na += 1
                reason = d.get(MOTION_NA_REASONS.get(key, ""))
                if reason:
                    why[str(reason)] += 1
            else:
                vals.append(v)
        if not vals and not na:
            continue
        entry = {"name": key, "mean": round(mean(vals), 4) if vals else None, "n": len(vals),
                 "na": na, "in_total": in_total}
        if why:
            entry["na_reason"] = why.most_common(1)[0][0]
        subs.append(entry)
    out: dict = {"subscores": subs}
    details = [_details(r) for r in judged]
    out["stuck_episodes"] = sum(1 for d in details if d.get("stuck_joints"))
    unassessable = [d for d in details if "stuck" in d and d.get("stuck") is None]
    if unassessable:
        out["stuck_unassessable"] = len(unassessable)
        why = Counter(str(d["stuck_reason"]) for d in unassessable if d.get("stuck_reason"))
        if why:
            out["stuck_na_reason"] = why.most_common(1)[0][0]
    if any(k in d for d in details for k in ("idle_head_s", "idle_tail_s", "idle_mid_count")):
        idle = Counter()
        for d in details:
            if (num(d.get("idle_head_s")) or 0) > 0:
                idle["head"] += 1
            if (num(d.get("idle_mid_count")) or 0) > 0:
                idle["mid"] += 1
            if (num(d.get("idle_tail_s")) or 0) > 0:
                idle["tail"] += 1
        out["idle_episodes"] = series(idle, ("head", "mid", "tail"), keep_zero=True)
    active = [v for v in (num(d.get("active_ratio")) for d in details) if v is not None]
    if active:
        out["active_ratio_mean"] = round(mean(active), 4)
    return out


# ---------------------------------------------------------------- visual_quality

def _camera_scores(d: dict) -> dict[str, float | None]:
    per = d.get("per_camera_detail")
    if isinstance(per, dict):
        return {str(k): num(v.get("score")) if isinstance(v, dict) else num(v) for k, v in per.items()}
    per = d.get("per_camera")                        # older details: {camera: score}
    if isinstance(per, dict):
        return {str(k): num(v.get("score")) if isinstance(v, dict) else num(v) for k, v in per.items()}
    return {}


def visual_stats(records: list[dict]) -> dict:
    """Per camera: readings, mean, readings below 0.6, placeholder channels (not scored),
    its weight and a ten-bin histogram; the quality parameters the check used."""
    cams: dict[str, dict] = {}

    def slot(name) -> dict:
        return cams.setdefault(short_camera(name), {"scores": [], "placeholder": 0,
                                                    "weights": Counter()})

    params = None
    for r in records:
        if r.get("verdict") == "error":
            continue
        d = _details(r)
        for cam, s in _camera_scores(d).items():
            if s is not None:
                slot(cam)["scores"].append(s)
            else:
                slot(cam)
        for cam in d.get("padded_channels") or []:
            slot(cam)["placeholder"] += 1
        weights = d.get("camera_weights")
        for cam, w in weights.items() if isinstance(weights, dict) else ():
            if num(w) is not None:
                slot(cam)["weights"][num(w)] += 1
        if params is None and isinstance(d.get("params"), dict):
            params = d["params"]
    rows = []
    for cam in sorted(cams):
        c = cams[cam]
        s = c["scores"]
        row = {"camera": cam, "n": len(s), "mean": round(mean(s), 4) if s else None,
               "low": sum(1 for x in s if x < LOW_CAMERA_SCORE), "placeholder": c["placeholder"],
               "hist": score_bins(s)}
        if c["weights"]:
            row["weight"] = c["weights"].most_common(1)[0][0]
        rows.append(row)
    out: dict = {"cameras": rows, "low_camera_readings": sum(r["low"] for r in rows),
                 "placeholder_readings": sum(r["placeholder"] for r in rows)}
    if params:
        for key in ("blur_ref_var", "frame_max_side"):
            if num(params.get(key)) is not None:
                out[key] = params[key]
    return out


# ---------------------------------------------------------------- video_action_sync

#: episode-level readings: v1's badge (``annotated`` with only suspect cameras reads
#: 疑似错位, ``misaligned_all`` is the only verdict that drops the episode)
SYNC_VERDICTS = ("aligned", "annotated", "suspect", "undecidable", "misaligned")


def sync_kind(d: dict) -> str | None:
    v = d.get("verdict")
    if v == "misaligned_all":
        return "misaligned"
    if v == "annotated":
        return "suspect" if d.get("suspect_cameras") and not d.get("flagged_cameras") else "annotated"
    if v in ("aligned", "undecidable"):
        return str(v)
    return None


def sync_stats(records: list[dict], lag_tol_s: float) -> dict:
    """Episode verdicts, flagged camera readings and the per-camera health of the whole
    dataset (``sync_health``, v1's dataset-level diagnosis) - its counts, never its lists."""
    from ..core.checks.video_action_sync import sync_health

    kinds: Counter = Counter()
    readings: Counter = Counter()
    flagged = 0
    per_episode: dict[str, dict] = {}
    for r in records:
        if r.get("verdict") == "error":
            continue
        d = _details(r)
        k = sync_kind(d)
        if k is None:
            continue
        kinds[k] += 1
        flagged += len(d.get("flagged_cameras") or [])
        per = d.get("per_camera")
        if isinstance(per, dict):
            cams = {str(c): v for c, v in per.items() if isinstance(v, dict)}
            per_episode[f"ep{int(r['episode_index']):06d}"] = {**d, "per_camera": cams}
            for c in cams:
                readings[c] += 1
    out: dict = {"verdicts": series(kinds, SYNC_VERDICTS, keep_zero=True),
                 "flagged_camera_readings": flagged, "lag_tol_s": lag_tol_s}
    if per_episode:
        health = sync_health(per_episode, lag_tol_s=lag_tol_s)
        out["cameras"] = [{"camera": cam, "readings": readings.get(cam, 0), **v}
                          for cam, v in (health.get("per_camera") or {}).items()]
        out["sync_advice"] = str(health.get("advice") or "").replace("**", "")
        out["negative_lag_episodes"] = len(health.get("negative_lag_episodes") or [])
    return out


# ---------------------------------------------------------------- task_success

#: the layers of the judgement (v1's 判定口径): scoring, the per-camera review, the
#: label guard before a kill, the evidence arbitration
TASK_LAYERS = ("probe", "endstate", "label_guard", "arbitration")


def task_stats(records: list[dict]) -> dict:
    judged = [r for r in records if r.get("verdict") != "error"]
    codes: Counter = Counter()
    abstain: Counter = Counter()
    sources: Counter = Counter()
    layers: Counter = Counter()
    for r in judged:
        d = _details(r)
        code = str(d.get("verdict") or "other")
        codes[code] += 1
        if r.get("verdict") == "abstain":
            abstain[code] += 1
        if d.get("task_desc_source"):
            sources[str(d["task_desc_source"])] += 1
        if d.get("verdict") or d.get("init_verdict"):
            layers["probe"] += 1
        if d.get("review") or d.get("cam_votes"):
            layers["endstate"] += 1
        if isinstance(d.get("label_check"), dict):
            layers["label_guard"] += 1
        if isinstance(d.get("arbitration"), dict):
            layers["arbitration"] += 1
    return {"judgements": series(codes), "abstain_by_judgement": series(abstain),
            "text_sources": series(sources), "layers": series(layers, TASK_LAYERS, keep_zero=True)}


# ---------------------------------------------------------------- dedup

def dedup_stats(groups: dict) -> dict:
    """Duplicate groups by size (``"2"``: pairs)."""
    sizes = Counter(str(len(g)) for g in (groups or {}).get("action_collisions") or []
                    if isinstance(g, (list, tuple)) and g)
    return {"group_sizes": series(sizes, sorted(sizes, key=int))}


# ---------------------------------------------------------------- skill_profile

def _display(entry, slug: str) -> str:
    """A family or sub-skill's name: the Chinese one when the profile has it."""
    return str((entry or {}).get("name_zh") or slug) if isinstance(entry, dict) else str(slug)


def skill_stats(records: list[dict], profile: dict, audit: dict | None) -> dict:
    """The family distribution (with each family's sub-skills), the label disagreements
    the audit found by tier, and what the grouping text came from.

    Counts come from the records (each episode's family and sub-skill), names from the
    profile (its Chinese names when it has them); a profile without records - older
    runs - gives its own counts."""
    fams = (profile or {}).get("families") or {}
    under = set((profile or {}).get("undersampled") or [])
    judged = [_details(r) for r in records if r.get("verdict") != "error"]
    by_family = Counter(str(d["family"]) for d in judged if d.get("family"))
    by_sub = Counter((str(d["family"]), str(d.get("subskill") or "")) for d in judged if d.get("family"))
    total = sum(by_family.values())
    tree = []
    if by_family:
        for slug, n in by_family.items():
            f = fams.get(slug) if isinstance(fams.get(slug), dict) else {}
            known = f.get("subskills") if isinstance(f.get("subskills"), dict) else {}
            subs = [{"name": _display(known.get(sub), sub) if sub else "—", "count": c}
                    for (fam, sub), c in by_sub.items() if fam == slug]
            subs.sort(key=lambda x: (-x["count"], x["name"]))
            tree.append({"name": _display(f, slug), "count": n, "pct": round(100.0 * n / total, 2),
                         "undersampled": slug in under, "subskills": subs})
    else:
        for slug, f in fams.items():
            if not isinstance(f, dict):
                continue
            subs = [{"name": _display(s, name), "count": int(num(s.get("count")) or 0)}
                    for name, s in (f.get("subskills") or {}).items() if isinstance(s, dict)]
            subs.sort(key=lambda x: (-x["count"], x["name"]))
            tree.append({"name": _display(f, slug), "count": int(num(f.get("count")) or 0),
                         "pct": num(f.get("pct")), "undersampled": slug in under, "subskills": subs})
    tree.sort(key=lambda x: (-x["count"], x["name"]))
    tiers = audit if isinstance(audit, dict) else {}
    high = len(tiers.get("high") or [])
    review = len(tiers.get("mid_for_review") or [])
    sources = Counter(str(_details(r)["grouping_text_source"]) for r in records
                      if r.get("verdict") != "error" and _details(r).get("grouping_text_source"))
    return {"family_distribution": [{"name": f["name"], "count": f["count"]} for f in tree],
            "family_tree": tree, "label_disagreements": high + review,
            "disagreement_high": high, "disagreement_review": review,
            "unstable": len(tiers.get("low_caption_unstable") or []),
            "grouping_sources": series(sources)}
