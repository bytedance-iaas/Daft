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
    return {"assessment_mode": "advisory", "affects_dataset_verdict": False, "uncalibrated": uncalibrated,
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


def table_rows(table: str, results: dict) -> list[dict]:
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
