"""Per-camera measurements: position, orientation, local time offset (design 12 §8.1-§8.3).

Everything compares the declared projection (the input under test, never modified) with the
independent observation of the same physical point / direction on the same frame. The primary numbers
are the raw, uncompensated residuals; lag-compensated residuals are diagnostics only and use the same
frame mask before and after compensation. Pure functions over arrays.
"""
from __future__ import annotations

import numpy as np

from . import timeline as TL
from .load import EefSample, declared_track, recompute_projection

#: Shorter projected axes make the 2D angle meaningless (it is a measurability floor, not a threshold).
DEFAULT_MIN_AXIS_LEN_PX = 25.0


def focal_media_px(sample: EefSample, camera_id: str) -> float | None:
    """Focal length in media pixels (calibration fx times the H scale), for mm-equivalent errors."""
    cam = sample.cameras[camera_id]
    cal = cam.calibration(sample)
    if cal is None:
        return None
    H = cam.H[np.isfinite(cam.H[:, 0, 0])]
    if not len(H):
        return None
    scale = float(np.sqrt(abs(np.linalg.det(H[0][:2, :2]))))
    return float(cal["K"][0][0]) * scale


def position(sample: EefSample, camera_id: str, point_id: str, obs_uv: np.ndarray) -> dict:
    """Residual of one point: ``e = |u_declared - u_visual|`` per frame, plus an mm equivalent."""
    decl = declared_track(sample, camera_id, point_id)
    e_vec = decl.uv - obs_uv
    comparable = np.isin(decl.status, ["valid", "out_of_frame"])
    valid = np.isfinite(e_vec).all(1) & comparable
    e = np.where(valid, np.linalg.norm(np.nan_to_num(e_vec), axis=1), np.nan)
    depth = decl.depth.copy()
    if not np.isfinite(depth).any():
        rec = recompute_projection(sample, camera_id, point_id)
        if rec is not None:
            depth = rec.depth
    f = focal_media_px(sample, camera_id)
    e_mm = e * depth / f * 1000.0 if f else np.full_like(e, np.nan)
    assurance = sample.points[point_id]["provenance"]["assurance"]
    return {"point_id": point_id, "e_px": e, "e_vec": np.where(valid[:, None], e_vec, np.nan), "e_mm": e_mm,
            "valid": valid, "declared_uv": decl.uv, "declared_depth": depth, "assurance": assurance}


def orientation(sample: EefSample, camera_id: str, axis_id: str, obs: dict[str, np.ndarray], *,
                min_len_px: float) -> dict:
    """Angle between the declared and the observed 2D direction of one axis (design 12 §8.2).

    Directed axes compare in 0-180 deg, undirected lines in 0-90 deg. Frames where either projected
    length is below ``min_len_px`` are ``not_observable`` (the axis points at the camera, or the
    fingers are closed) rather than zero error.
    """
    a = sample.axes[axis_id]
    s, e = a["start_point_id"], a["end_point_id"]
    d0, d1 = declared_track(sample, camera_id, s).uv, declared_track(sample, camera_id, e).uv
    dv = d1 - d0
    ov = obs[e] - obs[s]
    ld, lo = np.linalg.norm(dv, axis=1), np.linalg.norm(ov, axis=1)
    finite = np.isfinite(dv).all(1) & np.isfinite(ov).all(1)
    long_enough = finite & (ld >= min_len_px) & (lo >= min_len_px)
    with np.errstate(invalid="ignore", divide="ignore"):
        cos = np.einsum("ij,ij->i", dv, ov) / (ld * lo)
        cross = dv[:, 0] * ov[:, 1] - dv[:, 1] * ov[:, 0]
        signed = np.degrees(np.arctan2(cross, np.einsum("ij,ij->i", dv, ov)))
    if a["directed"]:
        ang = np.degrees(np.arccos(np.clip(cos, -1, 1)))
    else:
        ang = np.degrees(np.arccos(np.clip(np.abs(cos), 0, 1)))
        signed = (signed + 90.0) % 180.0 - 90.0
    ang = np.where(long_enough, ang, np.nan)
    signed = np.where(long_enough, signed, np.nan)
    return {"axis_id": axis_id, "directed": bool(a["directed"]), "angle_deg": ang, "signed_deg": signed,
            "valid": long_enough, "not_observable": finite & ~long_enough,
            "declared_len_px": np.where(finite, ld, np.nan), "observed_len_px": np.where(finite, lo, np.nan)}


def _shifted(t: np.ndarray, tracks: list[np.ndarray], taus: np.ndarray, gap_factor: float) -> np.ndarray:
    """(len(taus), n_points, N, 2) declared tracks evaluated at t + tau (D-E8)."""
    return np.stack([np.stack([TL.interp_track(t, x, t + tau, gap_factor=gap_factor) for x in tracks])
                     for tau in taus])


def lag_scan(t: np.ndarray, declared: list[np.ndarray], observed: list[np.ndarray], *, fps: float,
             search_s: tuple[float, float] = (-1.0, 1.0), frame_mask: np.ndarray | None = None,
             gap_factor: float = 2.0, steps_per_frame: int = 4) -> dict | None:
    """Scan ``tau`` for ``u_visual(t) ~ u_declared(t + tau)``; positive tau = the record lags the video.

    One frame mask for every tau (frames where the observation and every shifted declaration exist),
    median residual per tau after removing each point's median residual vector (a constant image offset
    must not pass for a lag), sub-frame refinement by a parabola, plus the evidence the design asks for:
    improvement over tau=0, the second-best minimum, motion energy and overlap.
    """
    taus = np.arange(search_s[0], search_s[1] + 1e-9, 1.0 / (fps * steps_per_frame))
    sh = _shifted(t, declared, taus, gap_factor)                       # (K, P, N, 2)
    obs = np.stack(observed)                                           # (P, N, 2)
    mask = np.isfinite(sh).all(axis=(0, 3)) & np.isfinite(obs).all(-1)  # (P, N)
    if frame_mask is not None:
        mask &= frame_mask[None, :]
    n_valid = int(mask.sum())
    if n_valid < 10:
        return None
    res = np.empty(len(taus))
    for k in range(len(taus)):
        diff = sh[k] - obs                                             # (P, N, 2)
        for p in range(len(diff)):                                     # offset-invariant, like a correlation:
            if mask[p].any():                                          # a constant image offset is not a lag
                diff[p] -= np.median(diff[p][mask[p]], axis=0)
        res[k] = np.median(np.linalg.norm(diff, axis=-1)[mask])
    k = int(np.argmin(res))
    tau = float(taus[k])
    if 0 < k < len(taus) - 1:
        y0, y1, y2 = res[k - 1], res[k], res[k + 1]
        den = y0 - 2 * y1 + y2
        if den > 1e-12:
            tau += 0.5 * (y0 - y2) / den * (taus[1] - taus[0])
    k0 = int(np.argmin(np.abs(taus)))
    # second minimum: best local minimum at least 2 frames away from the global one
    far = np.abs(taus - taus[k]) >= 2.0 / fps
    local_min = np.r_[False, (res[1:-1] <= res[:-2]) & (res[1:-1] <= res[2:]), False]
    second = float(res[far & local_min].min()) if (far & local_min).any() else None
    speed = []
    for x, m in zip(declared, mask):
        v = np.linalg.norm(np.gradient(x, t, axis=0), axis=1)
        speed.append(v[m])
    speed = np.concatenate(speed) if speed else np.array([])
    return {"taus_s": taus, "residual_px": res, "lag_s": tau, "lag_frames": tau * fps,
            "residual_at_zero_px": float(res[k0]), "residual_at_lag_px": float(res[k]),
            "improvement": float(res[k0] / res[k]) if res[k] > 1e-9 else float("inf"),
            "drop_px": float(res[k0] - res[k]), "second_min_px": second,
            "second_min_ratio": (second / float(res[k])) if second and res[k] > 1e-9 else None,
            "motion_px_s": float(np.median(speed)) if speed.size else 0.0,
            "frames": int(mask.any(0).sum()), "comparisons": n_valid, "edge_at_limit": k in (0, len(taus) - 1),
            "offset_removed": True}


def local_lags(t: np.ndarray, declared: list[np.ndarray], observed: list[np.ndarray], *, fps: float,
               search_s: tuple[float, float], window_s: float = 4.0, hop_s: float = 1.0,
               min_frames: int = 20, gap_factor: float = 2.0) -> list[dict]:
    """Sliding-window lag (design 12 §8.3): one scan per window, same mask rule inside each window."""
    out = []
    t0, t1 = float(np.nanmin(t)), float(np.nanmax(t))
    c = t0 + window_s / 2
    while c <= t1 - window_s / 2 + 1e-9:
        m = (t >= c - window_s / 2) & (t < c + window_s / 2)
        scan = lag_scan(t, declared, observed, fps=fps, search_s=search_s, frame_mask=m, gap_factor=gap_factor)
        if scan is not None and scan["frames"] >= min_frames:
            out.append({"center_s": round(c, 3), "lag_s": round(scan["lag_s"], 4),
                        "improvement": round(scan["improvement"], 3), "frames": scan["frames"],
                        "motion_px_s": round(scan["motion_px_s"], 2)})
        c += hop_s
    return out


def hf_share(e_vec: np.ndarray, t: np.ndarray, fps: float, *, cutoff_hz: float = 2.0, min_run: int = 16) -> dict:
    """High-frequency part (> cutoff) of a residual vector series, on contiguous valid runs only."""
    from .motion import _lowpass

    ok = np.isfinite(e_vec).all(1)
    hf = np.full(len(e_vec), np.nan)
    total = 0.0
    high = 0.0
    for a, b in TL.runs(ok):
        if b - a < min_run:
            continue
        x = e_vec[a:b]
        lp = _lowpass(x, fps, cutoff_hz)
        h = x - lp
        hf[a:b] = np.linalg.norm(h, axis=1)
        d = x - x.mean(0)
        total += float((d ** 2).sum())
        high += float((h ** 2).sum())
    return {"hf_px": hf, "hf_rms_px": float(np.sqrt(np.nanmean(hf ** 2))) if np.isfinite(hf).any() else None,
            "hf_energy_share": high / total if total > 0 else None}
