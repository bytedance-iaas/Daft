"""Sub-item statuses from measurements and a threshold profile (design 12 §9).

``ok`` needs input, coverage and a threshold; without a profile only curves come out (``unknown`` with
``threshold_uncalibrated``). The ``demo`` profile is ``calibrated: false``: its ok / suspect are shown
and flagged uncalibrated. Episode level only summarises per sub-item (§9.3): any camera suspect ->
suspect; no averaging, no total score.
"""
from __future__ import annotations

import numpy as np

from . import contracts as C
from . import segments as SG
from .profile import Profile


def _q(x, pct):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return round(float(np.percentile(x, pct)), 3) if x.size else None


def _cell(status, reasons=(), **kw):
    return {"status": status, "reasons": sorted(set(r for r in reasons if r)), **kw}


def _coverage(requested, mapped, visible, valid):
    return {"requested": int(requested), "media_mapped": int(mapped), "observed_visible": int(visible),
            "valid": int(valid), "coverage": round(valid / requested, 3) if requested else 0.0,
            "success_in_visible": round(valid / visible, 3) if visible else None}


def _enough(valid, requested, prof: Profile) -> bool:
    c = prof.coverage
    return valid >= c["min_valid_frames"] and (valid / max(requested, 1)) >= c["min_valid_fraction"]


def position(cm, sample, prof: Profile | None) -> tuple[dict, list[dict]]:
    """Per point residual status; the camera is suspect if any assessable point is."""
    n = sample.n_frames
    mapped = int((sample.cameras[cm.camera_id].video_frame_index >= 0).sum())
    points, segs = {}, []
    for pid, p in cm.positions.items():
        e = p["e_px"]
        valid = int(p["valid"].sum())
        visible = int((cm.obs_vis[pid] == "visible").sum())
        m = {"median_px": _q(e, 50), "p95_px": _q(e, 95), "max_px": _q(e, 100), "median_mm_equiv": _q(p["e_mm"], 50),
             "median_u_px": _q(p["e_vec"][:, 0], 50), "median_v_px": _q(p["e_vec"][:, 1], 50),
             "hf_rms_px": cm.hf.get(pid, {}).get("hf_rms_px"), "hf_energy_share": cm.hf.get(pid, {}).get("hf_energy_share"),
             "assurance": p["assurance"]}
        cov = _coverage(n, mapped, visible, valid)
        if prof is None:
            points[pid] = _cell(C.UNKNOWN, [C.THRESHOLD_UNCALIBRATED], metrics=m, coverage=cov)
            continue
        if not _enough(valid, n, prof):
            points[pid] = _cell(C.UNKNOWN, [C.COVERAGE_INSUFFICIENT], metrics=m, coverage=cov)
            continue
        pp = prof.position
        rm = SG.rolling_median(e, pp["rolling_median_frames"])
        found = SG.hysteresis(rm, cm.t, on=pp["on_px"], off=pp["off_px"], min_duration_s=pp["min_duration_s"],
                              max_gap_s=pp["max_gap_s"], reason="position_residual")
        reasons = []
        if m["median_px"] is not None and m["median_px"] > pp["median_px"]:
            reasons.append("position_median_above_threshold")
        if found:
            reasons.append("position_sustained_residual")
        status = C.SUSPECT if reasons else C.OK
        for s in found:
            segs.append(s.as_dict(camera_id=cm.camera_id, subitem=C.POSITION, point_id=pid,
                                  evidence_frames=SG.worst_frames(e, s)))
        points[pid] = _cell(status, reasons, metrics=m, coverage=cov)
    return _combine(points, "points"), segs


def _combine(children: dict, key: str) -> dict:
    statuses = [c["status"] for c in children.values()]
    if not children:
        return _cell(C.UNKNOWN, [C.COVERAGE_INSUFFICIENT], **{key: {}})
    if C.SUSPECT in statuses:
        st = C.SUSPECT
    elif C.OK in statuses:
        st = C.OK
    else:
        st = C.UNKNOWN
    reasons = [r for c in children.values() for r in c["reasons"]] if st != C.OK else []
    return _cell(st, reasons, **{key: children})


def orientation(cm, sample, prof: Profile | None) -> tuple[dict, list[dict]]:
    axes, segs = {}, []
    for aid, o in cm.orientations.items():
        a = o["angle_deg"]
        valid = int(o["valid"].sum())
        m = {"median_deg": _q(a, 50), "p95_deg": _q(a, 95), "signed_median_deg": _q(o["signed_deg"], 50),
             "signed_iqr_deg": (round(_q(o["signed_deg"], 75) - _q(o["signed_deg"], 25), 3) if valid else None),
             "directed": o["directed"], "not_observable_frames": int(o["not_observable"].sum())}
        cov = {"requested": sample.n_frames, "valid": valid}
        if prof is None:
            axes[aid] = _cell(C.UNKNOWN, [C.THRESHOLD_UNCALIBRATED], metrics=m, coverage=cov)
            continue
        po = prof.orientation
        if valid < po["min_valid_frames"]:
            axes[aid] = _cell(C.UNKNOWN, [C.ORIENTATION_NOT_OBSERVABLE], metrics=m, coverage=cov)
            continue
        rm = SG.rolling_median(a, po["rolling_median_frames"])
        found = SG.hysteresis(rm, cm.t, on=po["on_deg"], off=po["off_deg"], min_duration_s=po["min_duration_s"],
                              max_gap_s=po["max_gap_s"], reason="orientation_residual")
        reasons = []
        if m["median_deg"] is not None and m["median_deg"] > po["median_deg"]:
            reasons.append("orientation_median_above_threshold")
        if found:
            reasons.append("orientation_sustained_residual")
        for s in found:
            segs.append(s.as_dict(camera_id=cm.camera_id, subitem=C.ORIENTATION, axis_id=aid,
                                  evidence_frames=SG.worst_frames(a, s)))
        axes[aid] = _cell(C.SUSPECT if reasons else C.OK, reasons, metrics=m, coverage=cov)
    if not axes:
        return _cell(C.UNKNOWN, [C.ORIENTATION_NOT_OBSERVABLE], axes={}), segs
    return _combine(axes, "axes"), segs


def temporal(cm, prof: Profile | None) -> dict:
    lag = cm.lag
    if lag is None:
        return _cell(C.UNKNOWN, [C.LAG_NOT_IDENTIFIABLE], metrics={}, local=[])
    m = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in lag.items()
         if k not in ("taus_s", "residual_px")}
    local = cm.local_lags
    if prof is None:
        return _cell(C.UNKNOWN, [C.THRESHOLD_UNCALIBRATED], metrics=m, local=local)
    pt = prof.temporal
    if lag["motion_px_s"] < pt["min_motion_px_s"] or lag["edge_at_limit"]:
        return _cell(C.UNKNOWN, [C.LAG_NOT_IDENTIFIABLE], metrics=m, local=local)
    big = abs(lag["lag_frames"]) >= pt["min_lag_frames"]
    clear = lag["improvement"] >= pt["min_improvement"] and lag["drop_px"] >= pt["min_drop_px"]
    if big and clear:
        return _cell(C.SUSPECT, ["time_offset"], metrics=m, local=local)
    if big:
        return _cell(C.UNKNOWN, [C.LAG_NOT_IDENTIFIABLE], metrics=m, local=local)
    return _cell(C.OK, [], metrics=m, local=local)


def camera_motion(cm, prof: Profile | None) -> tuple[dict, list[dict]]:
    bg, hf = cm.background, cm.background_hf
    if bg is None:
        return _cell(C.UNKNOWN, [C.BACKGROUND_SUPPORT_INSUFFICIENT], metrics={}), []
    n = len(bg["dx_px"])
    fitted = int(np.isfinite(bg["dx_px"]).sum())
    m = {"hf_rms_max_px": _q(hf["hf_rms_px"], 100), "hf_rms_p50_px": _q(hf["hf_rms_px"], 50),
         "range_dx_px": (round(float(np.nanmax(bg["dx_px"]) - np.nanmin(bg["dx_px"])), 3) if fitted else None),
         "range_dy_px": (round(float(np.nanmax(bg["dy_px"]) - np.nanmin(bg["dy_px"])), 3) if fitted else None),
         "support_median": _q(bg["support"], 50), "fitted_frames": fitted}
    if prof is None:
        return _cell(C.UNKNOWN, [C.THRESHOLD_UNCALIBRATED], metrics=m), []
    pc = prof.camera_motion
    if fitted < pc["min_support_fraction"] * n:
        return _cell(C.UNKNOWN, [C.BACKGROUND_SUPPORT_INSUFFICIENT], metrics=m), []
    found = SG.hysteresis(hf["hf_rms_px"], cm.t, on=pc["hf_on_px"], off=pc["hf_off_px"],
                          min_duration_s=pc["min_duration_s"], max_gap_s=pc["max_gap_s"], reason="camera_motion")
    segs = [s.as_dict(camera_id=cm.camera_id, subitem=C.CAMERA_MOTION,
                      evidence_frames=SG.worst_frames(hf["hf_px"], s)) for s in found]
    return _cell(C.SUSPECT if found else C.OK, ["background_high_frequency_motion"] if found else [], metrics=m), segs


def input_consistency(sample, camera_id: str) -> dict:
    stats = sample.consistency.get("cameras", {}).get(camera_id)
    if not stats:
        return _cell(C.UNSUPPORTED, [C.PROJECTION_MISSING])
    worst = max(v["max_px"] for v in stats.values())
    m = {"max_px": round(worst, 4), "tolerance_px": C.REPROJECTION_TOLERANCE_PX,
         "points": {k: {"max_px": round(v["max_px"], 4), "median_px": round(v["median_px"], 4)} for k, v in stats.items()}}
    if worst >= C.REPROJECTION_TOLERANCE_PX:
        return _cell(C.SUSPECT, [C.INPUT_INCONSISTENT], metrics=m)
    return _cell(C.OK, [], metrics=m)


def state_motion(l0: dict | None, sample, prof: Profile | None) -> tuple[dict, list[dict]]:
    if l0 is None:
        return _cell(C.UNKNOWN, [C.TOO_FEW_STATES], metrics={}), []
    s = l0["summary"]
    m = {k: s[k] for k in ("n_states", "duplicates_removed", "time_source", "state_rate_hz", "nyquist_hz",
                           "speed_p95_m_s", "ang_speed_p95_deg_s", "hf_pos_std_mm", "hf_pos_rms_max_mm",
                           "hf_rot_rms_max_deg", "spike_count", "spike_frames")}
    if prof is None:
        return _cell(C.UNKNOWN, [C.THRESHOLD_UNCALIBRATED], metrics=m), []
    ps = prof.state_motion
    per = l0["per_state"]
    found = SG.hysteresis(per["hf_pos_rms_m"] * 1e3, per["t"], on=ps["hf_on_mm"], off=ps["hf_off_mm"],
                          min_duration_s=ps["min_duration_s"], max_gap_s=ps["max_gap_s"], reason="state_high_frequency")
    reasons = ["state_high_frequency"] if found else []
    if s["spike_count"] >= ps["spikes_suspect"]:
        reasons.append("state_spikes")
    fi = per["frame_index"]
    segs = [sg.as_dict(subitem=C.STATE_MOTION, start_frame=int(fi[sg.start]), end_frame=int(fi[sg.end]),
                       evidence_frames=[int(fi[i]) for i in SG.worst_frames(per["hf_pos_rms_m"], sg)])
            for sg in found]
    return _cell(C.SUSPECT if reasons else C.OK, reasons, metrics=m), segs


def episode_summary(cameras: dict, state: dict) -> dict:
    """§9.3: per sub-item, any camera suspect -> suspect; otherwise ok / unknown / unsupported."""
    out = {}
    for k in C.CAMERA_SUBITEMS:
        cells = [c["subitems"][k] for c in cameras.values() if k in c["subitems"]]
        statuses = [c["status"] for c in cells]
        assessable = sum(s in (C.OK, C.SUSPECT) for s in statuses)
        if C.SUSPECT in statuses:
            st = C.SUSPECT
        elif C.OK in statuses:
            st = C.OK
        elif C.ERROR in statuses:
            st = C.ERROR
        elif statuses and all(s == C.UNSUPPORTED for s in statuses):
            st = C.UNSUPPORTED
        else:
            st = C.UNKNOWN
        out[k] = {"status": st, "cameras_assessable": assessable, "cameras": len(cells),
                  "suspect_cameras": [cid for cid, c in cameras.items() if c["subitems"].get(k, {}).get("status")
                                      == C.SUSPECT]}
    out[C.STATE_MOTION] = {"status": state["status"], "cameras_assessable": None, "cameras": None,
                           "suspect_cameras": []}
    return out
