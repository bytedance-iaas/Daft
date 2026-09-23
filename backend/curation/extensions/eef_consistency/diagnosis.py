"""Diagnostic hypotheses (design 12 §8.6). They never change a status.

Each hypothesis is computed only when the sub-item it would explain is already ``suspect``, and comes
with its supporting evidence, the fitted quantity, the residual improvement after the fit and a
support flag. A PnP fit is never written back into the calibration.
"""
from __future__ import annotations

import cv2
import numpy as np

from . import contracts as C
from . import geometry as G
from .load import EefSample
from .profile import Profile


def _hyp(kind: str, camera_id: str | None, supported: bool, confidence: str, **kw) -> dict:
    return {"hypothesis": kind, "camera_id": camera_id, "supported": bool(supported), "confidence": confidence, **kw}


def extrinsics(sample: EefSample, cm, prof: Profile) -> dict | None:
    """Refit the camera pose from (3D model points, observed pixels) pairs: small ΔT and a residual
    that drops to the noise floor support a wrong declared extrinsic (dataset2 ep6)."""
    cam = sample.cameras[cm.camera_id]
    cal = cam.calibration(sample)
    if cal is None or cal["extrinsics_mode"] != "static" or not sample.has_absolute_pose:
        return None
    obj, img, pred = [], [], []
    for pid, p in cm.positions.items():
        offsets = G.point_offsets(sample.points[pid], sample.gripper)
        if offsets is None:
            continue
        P = G.reference_points(sample.T_reference_eef, offsets)
        ok = p["valid"] & np.isfinite(P).all(1)
        if not ok.any():
            continue
        Hinv = np.linalg.inv(cam.H[ok])
        uv_cal = G.apply_homography(Hinv, cm.obs[pid][ok])
        obj.append(P[ok])
        img.append(uv_cal)
        pred.append(p["declared_uv"][ok] - cm.obs[pid][ok])
    if not obj:
        return None
    obj, img = np.concatenate(obj), np.concatenate(img)
    before = float(np.median(np.linalg.norm(np.concatenate(pred), axis=1)))
    if len(obj) < 12:
        return None
    K = np.asarray(cal["K"], float)
    dist = np.asarray(cal["distortion_coefficients"], float) if cal["model"] == G.BROWN else None
    T_ref_cam = np.asarray(cal["T_reference_camera"], float)
    T_cam_ref = G.se3_inverse(T_ref_cam)
    rvec0, _ = cv2.Rodrigues(T_cam_ref[:3, :3])
    tvec0 = T_cam_ref[:3, 3].reshape(3, 1)
    if cal["model"] == G.FISHEYE:
        return None                                  # first cut: pinhole / Brown only
    ok, rvec, tvec, inl = cv2.solvePnPRansac(obj.astype(np.float64), img.astype(np.float64), K, dist, rvec0.copy(),
                                             tvec0.copy(), useExtrinsicGuess=True, reprojectionError=4.0,
                                             iterationsCount=200, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok or inl is None or len(inl) < 12:
        return _hyp("extrinsics_error", cm.camera_id, False, "low", reason="pnp_failed")
    inl = inl.ravel()
    rvec, tvec = cv2.solvePnPRefineLM(obj[inl], img[inl], K, dist, rvec, tvec)
    R_fit, _ = cv2.Rodrigues(rvec)
    T_cam_ref_fit = G.se3(R_fit, tvec.ravel())
    delta = T_cam_ref_fit @ T_ref_cam                  # declared camera frame -> fitted camera frame
    proj, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
    after = float(np.median(np.linalg.norm(proj.reshape(-1, 2) - img, axis=1)))
    pd = prof.diagnosis
    improvement = (before - after) / before if before > 0 else 0.0
    dt_mm = float(np.linalg.norm(delta[:3, 3])) * 1e3
    dr_deg = float(G.rotation_angle_deg(delta[:3, :3]))
    plausible = dt_mm <= pd["pnp_max_translation_mm"] and dr_deg <= pd["pnp_max_rotation_deg"]
    supported = plausible and after <= pd["fit_max_residual_px"] and improvement >= pd["fit_min_improvement"]
    return _hyp("extrinsics_error", cm.camera_id, supported, "medium" if supported else "low", plausible=plausible,
                fitted={"delta_translation_mm": round(dt_mm, 2),
                        "declared_origin_in_fitted_camera_m": [round(float(x), 4) for x in delta[:3, 3]],
                        "delta_rotation_deg": round(dr_deg, 3)},
                residual_before_px=round(before, 3), residual_after_px=round(after, 3),
                improvement=round(improvement, 3), pairs=int(len(obj)), inliers=int(len(inl)),
                note="fit is not written back into the calibration")


def time_offset(cm, subitem: dict, prof: Profile) -> dict | None:
    lag = cm.lag
    if lag is None:
        return None
    supported = subitem["status"] == C.SUSPECT and lag["improvement"] >= prof.diagnosis["lag_min_improvement"]
    return _hyp("time_offset", cm.camera_id, supported, "high" if lag["improvement"] >= 3 else "medium",
                fitted={"lag_s": round(lag["lag_s"], 4), "lag_frames": round(lag["lag_frames"], 3),
                        "sign": "record lags the video" if lag["lag_s"] > 0 else "video lags the record"},
                residual_before_px=round(lag["residual_at_zero_px"], 3),
                residual_after_px=round(lag["residual_at_lag_px"], 3), improvement=round(lag["improvement"], 3),
                local_lags=cm.local_lags[:12],
                note="compensated residual is diagnostic only; same frame mask before and after")


def _body_rotation_fit(sample: EefSample, cm, frames: np.ndarray) -> dict | None:
    """Fit one constant rotation dR in the EEF frame, ``T_reference_eef(t) @ dR``, to the observed points.

    A constant orientation error projects to a 2D angle that changes with the pose, so the test is 3D:
    if a single dR carries the declared points onto the observations (residual down to the noise
    floor), the orientation error is constant. Needs form B (pose + calibration); None otherwise.
    """
    from scipy.optimize import least_squares
    from scipy.spatial.transform import Rotation

    cam = sample.cameras[cm.camera_id]
    cal = cam.calibration(sample)
    if cal is None or not sample.has_absolute_pose or cal["model"] == G.FISHEYE:
        return None
    T_cam = cam.T_reference_camera
    groups = []
    for pid, p in cm.positions.items():
        offsets = G.point_offsets(sample.points[pid], sample.gripper)
        if offsets is None:
            continue
        ok = frames & p["valid"] & np.isfinite(offsets).all(1) & np.isfinite(sample.T_reference_eef[:, 0, 0])
        if ok.sum() >= 3:
            groups.append((sample.T_reference_eef[ok], offsets[ok], T_cam[ok], cam.H[ok], cm.obs[pid][ok]))
    if not groups or sum(len(g[0]) for g in groups) < 12:
        return None

    def pred(w):
        R = Rotation.from_rotvec(w).as_matrix()
        out = []
        for T, off, Tc, H, _ in groups:
            uv, _ = G.project_chain(T, off @ R.T, Tc, cal["K"], cal["model"], cal["distortion_coefficients"], H)
            out.append(uv)
        return np.concatenate(out)

    obs = np.concatenate([g[4] for g in groups])
    # a tiny ridge (0.05 px per degree) pins what the points cannot see - e.g. a spin about an axis that
    # every observed point lies on - to zero instead of letting it wander
    fit = least_squares(lambda w: np.r_[(pred(w) - obs).ravel(), 0.05 * np.degrees(w)], np.zeros(3),
                        loss="soft_l1", f_scale=3.0)
    before = float(np.median(np.linalg.norm(pred(np.zeros(3)) - obs, axis=1)))
    after = float(np.median(np.linalg.norm(pred(fit.x) - obs, axis=1)))
    angle = float(np.degrees(np.linalg.norm(fit.x)))
    axis = (fit.x / np.linalg.norm(fit.x)).tolist() if angle > 1e-6 else [0.0, 0.0, 0.0]
    return {"angle_deg": angle, "axis_eef": axis, "before": before, "after": after, "pairs": int(len(obs))}


def constant_orientation(sample: EefSample, cm, orient_cell: dict, pos_cell: dict, segments: list[dict],
                         prof: Profile) -> list[dict]:
    out = []
    pd = prof.diagnosis
    pos_meds = [p["metrics"]["median_px"] for p in pos_cell.get("points", {}).values()
                if p["metrics"].get("median_px") is not None]
    fixed_point = bool(pos_meds) and min(pos_meds) <= prof.position["median_px"]
    for aid, a in orient_cell.get("axes", {}).items():
        if a["status"] != C.SUSPECT:
            continue
        o = cm.orientations[aid]
        frames = np.zeros(sample.n_frames, bool)
        for s in segments:
            if s.get("camera_id") == cm.camera_id and s.get("subitem") == C.ORIENTATION and s.get("axis_id") == aid:
                frames[s["start_frame"]:s["end_frame"] + 1] = True
        if not frames.any():
            frames = o["valid"].copy()
        m = a["metrics"]
        fit = _body_rotation_fit(sample, cm, frames)
        if fit is None:                              # form A: only the 2D spread is available
            iqr = m.get("signed_iqr_deg")
            steady = iqr is not None and iqr <= pd["orientation_max_iqr_deg"]
            out.append(_hyp("constant_orientation_error", cm.camera_id, steady and fixed_point, "low", axis_id=aid,
                            fitted={"signed_median_deg": m.get("signed_median_deg"), "signed_iqr_deg": iqr},
                            evidence={"angle_steady_2d": steady, "a_point_stays_put": fixed_point}))
            continue
        improvement = (fit["before"] - fit["after"]) / fit["before"] if fit["before"] > 0 else 0.0
        supported = fit["after"] <= pd["fit_max_residual_px"] and improvement >= pd["fit_min_improvement"]
        out.append(_hyp("constant_orientation_error", cm.camera_id, supported, "medium" if supported else "low",
                        axis_id=aid,
                        fitted={"delta_rotation_deg": round(fit["angle_deg"], 3),
                                "rotation_axis_eef": [round(x, 3) for x in fit["axis_eef"]],
                                "signed_median_deg_2d": m.get("signed_median_deg")},
                        residual_before_px=round(fit["before"], 3), residual_after_px=round(fit["after"], 3),
                        improvement=round(improvement, 3), pairs=fit["pairs"], frames=int(frames.sum()),
                        evidence={"a_point_stays_put": fixed_point},
                        note="one constant rotation in the EEF frame, fitted on the suspect frames only"))
    return out


def tcp_axial(sample: EefSample, cm, pos_cell: dict, prof: Profile) -> list[dict]:
    """A wrong point definition along the approach axis (flange vs fingertip, the DROID trap): fit one
    offset ``d`` along local z per suspect point; supported if it brings the residual to the noise floor."""
    from scipy.optimize import least_squares

    cam = sample.cameras[cm.camera_id]
    cal = cam.calibration(sample)
    if cal is None or not sample.has_absolute_pose or cal["model"] == G.FISHEYE:
        return []
    out = []
    pd = prof.diagnosis
    for pid, p in cm.positions.items():
        if pos_cell.get("points", {}).get(pid, {}).get("status") != C.SUSPECT:
            continue
        offsets = G.point_offsets(sample.points[pid], sample.gripper)
        if offsets is None:
            continue
        ok = p["valid"] & np.isfinite(offsets).all(1)
        if ok.sum() < 12:
            continue
        T, off, Tc, H, obs = sample.T_reference_eef[ok], offsets[ok], cam.T_reference_camera[ok], cam.H[ok], cm.obs[pid][ok]

        def pred(d):
            uv, _ = G.project_chain(T, off + np.array([0.0, 0.0, float(d[0])]), Tc, cal["K"], cal["model"],
                                    cal["distortion_coefficients"], H)
            return uv

        fit = least_squares(lambda d: (pred(d) - obs).ravel(), np.zeros(1), loss="soft_l1", f_scale=3.0)
        before = float(np.median(np.linalg.norm(pred(np.zeros(1)) - obs, axis=1)))
        after = float(np.median(np.linalg.norm(pred(fit.x) - obs, axis=1)))
        improvement = (before - after) / before if before > 0 else 0.0
        supported = after <= pd["fit_max_residual_px"] and improvement >= pd["fit_min_improvement"]
        out.append(_hyp("tcp_axial_offset", cm.camera_id, supported, "medium" if supported else "low", point_id=pid,
                        fitted={"offset_along_approach_mm": round(float(fit.x[0]) * 1e3, 2)},
                        residual_before_px=round(before, 3), residual_after_px=round(after, 3),
                        improvement=round(improvement, 3)))
    return out


def drift(cm, pos_cell: dict, others: list[dict], prof: Profile) -> dict | None:
    if pos_cell["status"] != C.SUSPECT:
        return None
    # camera-level causes must be on this camera; pose / timing causes are shared by all cameras
    explained = any(h["supported"] and ((h["hypothesis"] in ("extrinsics_error", "tcp_axial_offset")
                                         and h["camera_id"] == cm.camera_id)
                                        or h["hypothesis"] in ("time_offset", "constant_orientation_error"))
                    for h in others)
    shares = [h.get("hf_energy_share") for h in cm.hf.values() if h.get("hf_energy_share") is not None]
    share = float(np.median(shares)) if shares else None
    low_freq = share is not None and share <= prof.diagnosis["drift_max_hf_share"]
    return _hyp("pose_drift_or_kinematics", cm.camera_id, low_freq and not explained, "low",
                fitted={"hf_energy_share": None if share is None else round(share, 3)},
                evidence={"low_frequency": low_freq, "explained_by_other_hypothesis": explained})


def jitter_source(cm, cam_cells: dict, state_cell: dict, prof: Profile) -> dict | None:
    """§8.5 combinations: background moves with the gripper -> video; the state shakes but the picture
    does not -> record; only the gripper oscillates -> local end-effector motion."""
    hf = [h["hf_rms_px"] for h in cm.hf.values() if h.get("hf_rms_px") is not None]
    ehf = max(hf) if hf else None
    cam_motion = cam_cells[C.CAMERA_MOTION]["status"] == C.SUSPECT
    state = state_cell["status"] == C.SUSPECT
    local = ehf is not None and ehf >= prof.position["local_hf_px"]
    if not (cam_motion or state or local):
        return None
    if cam_motion:
        source = "video"
    elif state:
        source = "record"
    else:
        source = "local_end_effector_motion"
    return _hyp("jitter_source", cm.camera_id, True, "medium", fitted={"source": source},
                evidence={"camera_motion_suspect": cam_motion, "state_motion_suspect": state,
                          "position_hf_rms_px": None if ehf is None else round(ehf, 3)})


def hypotheses(sample: EefSample, cams: dict, cells: dict, state_cell: dict, prof: Profile | None,
               segments: list[dict] | None = None) -> list[dict]:
    if prof is None:
        return []
    out: list[dict] = []
    segments = segments or []
    for cid, cm in cams.items():
        c = cells[cid]["subitems"]
        mine: list[dict] = []
        if c[C.TEMPORAL]["status"] == C.SUSPECT:
            h = time_offset(cm, c[C.TEMPORAL], prof)
            if h:
                mine.append(h)
        if c[C.POSITION]["status"] == C.SUSPECT:
            h = extrinsics(sample, cm, prof)
            if h:
                mine.append(h)
            mine += tcp_axial(sample, cm, c[C.POSITION], prof)
        if c[C.ORIENTATION]["status"] == C.SUSPECT:
            mine += constant_orientation(sample, cm, c[C.ORIENTATION], c[C.POSITION], segments, prof)
        out += mine
    for cid, cm in cams.items():                     # drift last: it is what remains once the others are ruled out
        c = cells[cid]["subitems"]
        d = drift(cm, c[C.POSITION], out, prof)
        if d:
            out.append(d)
        j = jitter_source(cm, c, state_cell, prof)
        if j:
            out.append(j)
    return out
