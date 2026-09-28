"""Report section of the EEF-video consistency module (design doc 12 §11.3-§11.4).

Built from the result records' ``details``: the section summary (sub-item status counts, overall
distribution, coverage, supported hypotheses) and the three detail tables of the registry,
``eef_camera_metrics`` / ``eef_segments`` / ``eef_diagnosis``. A missing measurement stays missing
(never 0 px); the physical point and its assurance travel with the numbers.
"""
from __future__ import annotations

import json

from . import contracts as C

STATUSES = (C.OK, C.SUSPECT, C.UNKNOWN, C.UNSUPPORTED, C.ERROR)
SUBITEMS = (C.POSITION, C.ORIENTATION, C.TEMPORAL, C.STATE_MOTION, C.CAMERA_MOTION, C.INPUT_CONSISTENCY)


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def summary(results: dict) -> dict:
    """Flat keys for the report section (the console's default view shows scalars as stats and
    ``[{name, count}]`` lists as bar charts); ``subitem_status`` keeps the full status matrix."""
    overall: dict[str, int] = {}
    per = {k: {s: 0 for s in STATUSES} for k in SUBITEMS}
    coverage: list[float] = []
    hyps: dict[str, int] = {}
    uncalibrated = False
    profile = None
    for rec in results.values():
        d = rec.get("details") or {}
        if d.get("assessment_mode") == "vlm_opinion":     # no CPU reading (design doc 12 §10.5)
            continue
        o = d.get("overall") or "error"
        overall[o] = overall.get(o, 0) + 1
        for k in SUBITEMS:
            st = ((d.get("summary") or {}).get(k) or {}).get("status")
            if st in per[k]:
                per[k][st] += 1
        for cam in (d.get("cameras") or {}).values():
            pts = ((cam.get("subitems") or {}).get(C.POSITION) or {}).get("points") or {}
            covs = [p["coverage"]["coverage"] for p in pts.values() if p.get("coverage")]
            if covs:
                coverage.append(min(covs))
        for h in d.get("diagnosis") or []:
            if h.get("supported"):
                key = h["hypothesis"] if h["hypothesis"] != "jitter_source" else \
                    f"jitter_source={(h.get('fitted') or {}).get('source')}"
                hyps[key] = hyps.get(key, 0) + 1
        uncalibrated = uncalibrated or bool(d.get("uncalibrated"))
        profile = profile or d.get("threshold_profile")
    coverage.sort()
    series = lambda counts: [{"name": k, "count": v} for k, v in counts.items() if v]  # noqa: E731
    return {"uncalibrated": uncalibrated,
            "threshold_profile": f"{profile['name']} {profile['version']}" if profile else None,
            "candidates": overall.get("candidate", 0), "assessed": overall.get("assessed", 0),
            "partially_assessable": overall.get("partially_assessable", 0),
            "not_assessable": overall.get("not_assessable", 0), "errors": overall.get("error", 0),
            "coverage_median": coverage[len(coverage) // 2] if coverage else None,
            "coverage_min": coverage[0] if coverage else None, "cameras_measured": len(coverage),
            "suspect_by_subitem": series({k: v[C.SUSPECT] for k, v in per.items()}),
            "unknown_by_subitem": series({k: v[C.UNKNOWN] for k, v in per.items()}),
            "supported_hypotheses": series(dict(sorted(hyps.items(), key=lambda kv: -kv[1]))),
            "subitem_status": per}


def _camera_rows(ep: int, d: dict) -> list[dict]:
    rows = []
    state = d.get("state_motion") or {}
    for cid, cam in (d.get("cameras") or {}).items():
        sub = cam.get("subitems") or {}
        pos, ori, tem = sub.get(C.POSITION) or {}, sub.get(C.ORIENTATION) or {}, sub.get(C.TEMPORAL) or {}
        mot, con = sub.get(C.CAMERA_MOTION) or {}, sub.get(C.INPUT_CONSISTENCY) or {}
        pts = pos.get("points") or {}
        worst = max(pts.items(), key=lambda kv: kv[1].get("metrics", {}).get("median_px") or -1, default=(None, {}))
        wm = worst[1].get("metrics") or {}
        covs = [p["coverage"]["coverage"] for p in pts.values() if p.get("coverage")]
        axes = ori.get("axes") or {}
        worst_axis = max(axes.values(), key=lambda a: (a.get("metrics") or {}).get("median_deg") or -1,
                         default={})
        tm = tem.get("metrics") or {}
        rows.append({
            "episode_index": ep, "camera": cid, "mount": cam.get("mount"), "overall": d.get("overall"),
            "position": pos.get("status"), "position_point": worst[0], "position_assurance": wm.get("assurance"),
            "position_median_px": _num(wm.get("median_px")), "position_p95_px": _num(wm.get("p95_px")),
            "position_median_mm_equiv": _num(wm.get("median_mm_equiv")),
            "orientation": ori.get("status"),
            "orientation_median_deg": _num((worst_axis.get("metrics") or {}).get("median_deg")),
            "temporal": tem.get("status"), "lag_s": _num(tm.get("lag_s")),
            "lag_improvement": _num(tm.get("improvement")),
            "camera_motion": mot.get("status"),
            "background_hf_rms_max_px": _num((mot.get("metrics") or {}).get("hf_rms_max_px")),
            "input_consistency": con.get("status"),
            "reprojection_max_px": _num((con.get("metrics") or {}).get("max_px")),
            "state_motion": state.get("status"),
            "state_hf_rms_max_mm": _num((state.get("metrics") or {}).get("hf_pos_rms_max_mm")),
            "coverage": min(covs) if covs else None,
            "observation": ((cam.get("observation") or {}).get("seed_method")),
            "reasons": ";".join(sorted({r for c in sub.values() for r in c.get("reasons") or []})),
        })
    if not rows:
        rows.append({"episode_index": ep, "camera": "", "overall": d.get("overall"),
                     "reasons": ";".join(d.get("reasons") or []), "position_median_px": None,
                     "orientation_median_deg": None, "lag_s": None, "coverage": None})
    return rows


def _record(rec: dict) -> dict | None:
    """``details.record`` of a run that was given a record mapping; None otherwise (nothing to report)."""
    r = (rec.get("details") or {}).get("record")
    if not r or C.RECORD_MAPPING_MISSING in (r.get("reasons") or []):
        return None
    return r


def record_summary(results: dict) -> dict:
    """design 12 §8.7 (D-E16): the upload against the dataset's own record, reported only - every episode
    counts, opinion-only ones included (the comparison needs no gripper reference); empty when the run
    was given no record mapping."""
    status = {s: 0 for s in STATUSES}
    sources: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    lags: list[float] = []
    internal = 0
    seen = 0
    for rec in results.values():
        r = _record(rec)
        if r is None:
            continue
        seen += 1
        if r.get("status") in status:
            status[r["status"]] += 1
        for k, src in (r.get("sources") or {}).items():
            cell = sources.setdefault(k, {s: 0 for s in STATUSES})
            if src.get("status") in cell:
                cell[src["status"]] += 1
            for why in src.get("reasons") or []:
                reasons[f"{k}:{why}"] = reasons.get(f"{k}:{why}", 0) + 1
            lag = (src.get("time_offset") or {}).get("lag_frames")
            if lag is not None:
                lags.append(abs(float(lag)))
        if (r.get("internal") or {}).get("consistent") is False:
            internal += 1
    if not seen:
        return {}
    lags.sort()
    series = lambda counts: [{"name": k, "count": v} for k, v in counts.items() if v]  # noqa: E731
    return {"record_compared": status[C.OK] + status[C.SUSPECT], "record_suspect": status[C.SUSPECT],
            "record_status": series(status), "record_by_source": sources,
            "record_reasons": series(dict(sorted(reasons.items(), key=lambda kv: -kv[1]))),
            "record_lag_median_frames": lags[len(lags) // 2] if lags else None,
            "record_internal_inconsistent": internal}


def record_rows(results: dict) -> list[dict]:
    out: list[dict] = []
    for ep, rec in sorted(results.items()):
        r = _record(rec) or {}
        for k, src in (r.get("sources") or {}).items():
            res = src.get("residual") or {}
            dev = ((src.get("relation") or {}).get("deviation")) or {}
            out.append({"episode_index": int(ep), "source": k, "status": src.get("status"),
                        "alignment": src.get("alignment") or "", "frames_compared": src.get("frames_compared"),
                        "position_p95_mm": _num((res.get("position_mm") or {}).get("p95")),
                        "rotation_p95_deg": _num((res.get("rotation_deg") or {}).get("p95")),
                        "constant_deviation_mm": _num(dev.get("translation_norm_mm")),
                        "constant_deviation_deg": _num(dev.get("rotation_deg")),
                        "relation_declared": bool((src.get("relation") or {}).get("declared")),
                        "lag_frames": _num((src.get("time_offset") or {}).get("lag_frames")),
                        "reasons": ";".join(src.get("reasons") or [])})
        if not r.get("sources") and r:
            out.append({"episode_index": int(ep), "source": "", "status": r.get("status"),
                        "reasons": ";".join(r.get("reasons") or [])})
    return out


def table_rows(table: str, results: dict) -> list[dict]:
    if table == "eef_review_windows":
        return review_rows(results)
    if table == "eef_record":
        return record_rows(results)
    out: list[dict] = []
    for ep, rec in sorted(results.items()):
        d = rec.get("details") or {}
        if table == "eef_camera_metrics":
            out += _camera_rows(int(ep), d)
        elif table == "eef_segments":
            for s in d.get("segments") or []:
                out.append({"episode_index": int(ep), "camera": s.get("camera_id") or "(state)",
                            "subitem": s.get("subitem"), "target": s.get("point_id") or s.get("axis_id") or "",
                            "start_s": _num(s.get("start_s")), "end_s": _num(s.get("end_s")),
                            "duration_s": _num(s.get("duration_s")), "peak": _num(s.get("peak")),
                            "mean": _num(s.get("mean")), "reasons": ";".join(s.get("reasons") or []),
                            "evidence_frames": ",".join(str(f) for f in s.get("evidence_frames") or [])})
        elif table == "eef_diagnosis":
            for h in d.get("diagnosis") or []:
                out.append({"episode_index": int(ep), "camera": h.get("camera_id") or "",
                            "hypothesis": h.get("hypothesis"), "supported": bool(h.get("supported")),
                            "confidence": h.get("confidence"),
                            "fitted": json.dumps(h.get("fitted") or {}, ensure_ascii=False),
                            "residual_before_px": _num(h.get("residual_before_px")),
                            "residual_after_px": _num(h.get("residual_after_px"))})
    return out


def review_summary(results: dict) -> dict:
    """The model's side of the section: how many episodes were reviewed completely, how many windows got
    an answer and why the others did not, and how often the model agreed with the CPU."""
    status: dict[str, int] = {}
    classes: dict[str, int] = {}
    failures: dict[str, int] = {}
    n = {"windows": 0, "answered": 0, "failed": 0, "tracking_suspect": 0, "requests": 0, "cache_hits": 0,
         "truncated": 0}
    agree = votes = 0
    for rec in results.values():
        d = rec.get("details") or {}
        if d.get("assessment_mode") == "vlm_opinion":     # asked for an opinion, not reviewed
            continue
        rv = d.get("review") or {}
        st = rv.get("status") or ("error" if rec.get("verdict") == "error" else "not_reviewed")
        status[st] = status.get(st, 0) + 1
        sm = rv.get("summary") or {}
        for k in ("windows", "answered", "failed", "tracking_suspect", "requests", "cache_hits"):
            n[k] += int(sm.get(k) or 0)
        n["truncated"] += bool(sm.get("truncated"))
        for k in ("support", "refute", "uncertain", "not_observable"):
            if sm.get(k):
                classes[k] = classes.get(k, 0) + int(sm[k])
        cams = d.get("cameras") or {}
        for cid, cam in (rv.get("cameras") or {}).items():
            cells = (cams.get(cid) or {}).get("subitems") or {}
            for w in cam.get("windows") or []:
                code = (w.get("failure") or {}).get("code")
                if code:
                    failures[code] = failures.get(code, 0) + 1
                a = w.get("answer") or {}
                for sub, key in ((C.POSITION, "position_support"), (C.ORIENTATION, "orientation_support")):
                    said = a.get(key)
                    cpu = C.SUSPECT if (w.get("kind") == "candidate" and w.get("subitem") == sub) \
                        else (cells.get(sub) or {}).get("status")
                    if said in ("support", "refute") and cpu in (C.OK, C.SUSPECT):
                        votes += 1
                        agree += (said == "refute") == (cpu == C.SUSPECT)
    series = lambda counts: [{"name": k, "count": v} for k, v in counts.items() if v]  # noqa: E731
    return {"reviewed": status.get("completed", 0), "incomplete": status.get("incomplete", 0),
            "not_reviewed": status.get("not_reviewed", 0),
            "tracking_suspect": n["tracking_suspect"], "windows": n["windows"], "windows_answered": n["answered"],
            "windows_failed": n["failed"], "truncated_episodes": n["truncated"], "vlm_requests": n["requests"],
            "cache_hits": n["cache_hits"], "model_cpu_agreement": round(agree / votes, 3) if votes else None,
            "model_votes": votes, "review_classes": series(classes), "failure_codes": series(failures)}


def verdict_summary(results: dict) -> dict:
    """What the module decided (D-E12): pass / reject / to a person, why people are asked and which
    sub-items the rejects came from."""
    outcomes = {"pass": 0, "reject": 0, "human": 0, "error": 0}
    human: dict[str, int] = {}
    rejected: dict[str, int] = {}
    for rec in results.values():
        dec = (rec.get("details") or {}).get("decision") or {}
        o = dec.get("outcome") or ("error" if rec.get("verdict") == "error" else None)
        if o in outcomes:
            outcomes[o] += 1
        for h in dec.get("human") or []:
            human[h["code"]] = human.get(h["code"], 0) + 1
        for c in dec.get("confirmed") or []:
            rejected[c.get("subitem", "")] = rejected.get(c.get("subitem", ""), 0) + 1
    series = lambda counts: [{"name": k, "count": v} for k, v in counts.items() if v]  # noqa: E731
    return {"judged_pass": outcomes["pass"], "judged_reject": outcomes["reject"], "to_human": outcomes["human"],
            "outcomes": series({"pass": outcomes["pass"], "reject": outcomes["reject"], "human": outcomes["human"],
                                "error": outcomes["error"]}),
            "human_reasons": series(dict(sorted(human.items(), key=lambda kv: -kv[1]))),
            "reject_subitems": series(rejected)}


def review_rows(results: dict) -> list[dict]:
    out: list[dict] = []
    for ep, rec in sorted(results.items()):
        d = rec.get("details") or {}
        for cid, cam in ((d.get("review") or {}).get("cameras") or {}).items():
            for w in cam.get("windows") or []:
                a = w.get("answer") or {}
                c = w.get("conflict") or {}
                seg = w.get("segment") or {}
                out.append({"episode_index": int(ep), "camera": cid, "kind": w.get("kind"),
                            "subitem": w.get("subitem") or "", "point": w.get("point_id") or "",
                            "axis": w.get("axis_id") or "", "frames": ",".join(str(f) for f in w.get("frames") or []),
                            "start_s": _num(seg.get("start_s")), "status": w.get("status"),
                            "review_status": a.get("review_status") or "",
                            "position_support": a.get("position_support") or "",
                            "orientation_support": a.get("orientation_support") or "",
                            "tracking_target_correct": a.get("tracking_target_correct") or "",
                            "offset": (f"{a.get('offset_direction')}/{a.get('offset_magnitude_class')}"
                                       if a else ""),
                            "conflict": f"{c.get('subitem')}: CPU {c.get('cpu')} / VLM {c.get('vlm')}" if c else "",
                            "failure": (w.get("failure") or {}).get("code") or "",
                            "explanation": a.get("explanation") or "", "attempts": w.get("attempts")})
        if not (d.get("review") or {}).get("cameras"):
            rv = d.get("review") or {}
            out.append({"episode_index": int(ep), "camera": "", "kind": "", "status": rv.get("status") or "",
                        "review_status": "", "conflict": "", "failure": ";".join(rv.get("reasons") or [])})
    return out

