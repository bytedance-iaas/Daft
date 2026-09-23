"""L0 numeric trajectory (design 12 §8.4) and background / camera motion (§8.5).

L0 looks at the recorded state only: actual Δt, linear and angular speed, robust spikes and the
high-frequency share of the pose. Repeated states from resampling (the same ``source_state_index``
on consecutive frames, or bit-identical poses) are collapsed first, and the robot clock is used when
the sample declares one, so presentation-rate duplication does not read as jitter. Frequencies are
only resolved below Nyquist of the actual state rate.
"""
from __future__ import annotations

import dataclasses

import numpy as np
from scipy import signal

from . import geometry as G
from . import timeline as TL
from .load import EefSample

HF_CUTOFF_HZ = 2.0            # low-frequency motion vs high-frequency jitter (dataset1 README: drift < 1 %)
ROLLING_WINDOW_S = 1.0
SPIKE_Z = 8.0                 # robust z of the acceleration magnitude
SPIKE_FLOOR_M_S2 = 5.0


@dataclasses.dataclass
class StateSeries:
    frame_index: np.ndarray        # (M,) frame of the sample each state was first paired with
    t: np.ndarray                  # (M,) seconds
    T: np.ndarray                  # (M, 4, 4) declared poses (relative poses need no anchor here)
    time_source: str               # robot_clock | main_timeline
    duplicates_removed: int


def state_series(sample: EefSample) -> StateSeries | None:
    """Unique recorded states with their best available time axis (None without a usable pose/time)."""
    idx = np.flatnonzero(sample.eef_mask)
    if len(idx) == 0:
        return None
    T = sample.T_declared[idx]
    ssi = sample.source_state_index[idx]
    keep = np.ones(len(idx), bool)
    if (ssi >= 0).all():
        keep[1:] = ssi[1:] != ssi[:-1]
    else:
        same = np.all(np.isclose(T[1:], T[:-1], rtol=0, atol=0), axis=(1, 2))
        keep[1:] = ~same
    idx, T = idx[keep], T[keep]
    removed = int((~keep).sum())
    robot = sample.clocks.get("robot_state")
    if robot is not None:
        tr = robot.seconds[idx]
        if np.isfinite(tr).all() and (np.diff(tr) > 0).all():
            return StateSeries(idx, tr, T, "robot_clock", removed)
    if sample.t is not None:
        tm = sample.t[idx]
        if np.isfinite(tm).all() and (np.diff(tm) > 0).all():
            return StateSeries(idx, tm, T, "main_timeline", removed)
    return None


def _lowpass(x: np.ndarray, fs: float, cutoff: float) -> np.ndarray:
    """Zero-phase 2nd-order Butterworth low-pass along axis 0 (identity when the cutoff >= Nyquist)."""
    if cutoff >= 0.5 * fs * 0.95 or len(x) < 16:
        return x.copy()
    b, a = signal.butter(2, cutoff / (0.5 * fs))
    return signal.filtfilt(b, a, x, axis=0, padlen=min(3 * max(len(a), len(b)), len(x) - 1))


def _rolling_rms(x: np.ndarray, width: int) -> np.ndarray:
    width = max(1, int(width))
    k = np.ones(width) / width
    return np.sqrt(np.convolve(np.nan_to_num(x) ** 2, k, mode="same"))


def l0_metrics(series: StateSeries, *, hf_cutoff_hz: float = HF_CUTOFF_HZ,
               rolling_window_s: float = ROLLING_WINDOW_S) -> dict:
    """Per-state signals and a summary for the recorded pose stream."""
    t, T = series.t, series.T
    M = len(t)
    dt = np.diff(t)
    fs = 1.0 / TL.median_step(t)
    p = T[:, :3, 3]
    R = T[:, :3, :3]
    v = np.diff(p, axis=0) / dt[:, None]
    speed = np.linalg.norm(v, axis=1)
    rel = np.einsum("nji,njk->nik", R[:-1], R[1:])               # R_t^T R_{t+1}
    ang_speed = np.degrees(np.linalg.norm(G.rotation_log(rel), axis=1)) / dt
    acc = np.diff(v, axis=0) / ((dt[1:] + dt[:-1]) / 2)[:, None] if M > 2 else np.zeros((0, 3))
    acc_mag = np.linalg.norm(acc, axis=1)
    med = float(np.median(acc_mag)) if len(acc_mag) else 0.0
    mad = float(np.median(np.abs(acc_mag - med))) * 1.4826 if len(acc_mag) else 0.0
    z = (acc_mag - med) / mad if mad > 0 else np.zeros_like(acc_mag)
    spike = (z > SPIKE_Z) & (acc_mag > SPIKE_FLOOR_M_S2)
    # high-frequency share on a uniform grid at the median state rate
    grid = TL.uniform_grid(t)
    pg = TL.interp_track(t, p, grid, gap_factor=4.0)
    ok = np.isfinite(pg).all(1)
    hf_pos = np.full(M, np.nan)
    hf_rot = np.full(M, np.nan)
    if ok.sum() >= 16:
        pg_ok = pg[ok]
        hp = pg_ok - _lowpass(pg_ok, fs, hf_cutoff_hz)
        hf_pos = np.linalg.norm(TL.interp_track(grid[ok], hp, t, gap_factor=4.0), axis=1)
        r_ref = R[0]
        rv = G.rotation_log(np.einsum("ji,njk->nik", r_ref, R))
        rv = _unwrap_rotvec(rv)
        rg = TL.interp_track(t, rv, grid, gap_factor=4.0)[ok]
        hr = rg - _lowpass(rg, fs, hf_cutoff_hz)
        hf_rot = np.degrees(np.linalg.norm(TL.interp_track(grid[ok], hr, t, gap_factor=4.0), axis=1))
    width = int(round(rolling_window_s * fs))
    hf_pos_rms = _rolling_rms(hf_pos, width)
    hf_rot_rms = _rolling_rms(hf_rot, width)
    pad = lambda x: np.concatenate([[np.nan], x])  # noqa: E731  (velocity i belongs to state i)
    per_state = {"t": t, "frame_index": series.frame_index, "speed_m_s": pad(speed),
                 "ang_speed_deg_s": pad(ang_speed), "hf_pos_m": hf_pos, "hf_rot_deg": hf_rot,
                 "hf_pos_rms_m": hf_pos_rms, "hf_rot_rms_deg": hf_rot_rms,
                 "spike": np.concatenate([[False], spike, [False]]) if M > 2 else np.zeros(M, bool)}
    finite = lambda x: x[np.isfinite(x)]  # noqa: E731
    q = lambda x, pct: float(np.percentile(finite(x), pct)) if finite(x).size else None  # noqa: E731
    summary = {
        "n_states": int(M), "duplicates_removed": series.duplicates_removed, "time_source": series.time_source,
        "duration_s": float(t[-1] - t[0]), "state_rate_hz": fs,
        "dt_cv": float(np.std(dt) / np.mean(dt)) if len(dt) else None, "nyquist_hz": fs / 2,
        "hf_cutoff_hz": hf_cutoff_hz, "resolvable_band_hz": [hf_cutoff_hz, fs / 2],
        "speed_p50_m_s": q(speed, 50), "speed_p95_m_s": q(speed, 95), "speed_max_m_s": q(speed, 100),
        "ang_speed_p95_deg_s": q(ang_speed, 95), "ang_speed_max_deg_s": q(ang_speed, 100),
        "hf_pos_std_mm": float(np.sqrt(np.nanmean(hf_pos ** 2)) * 1e3) if finite(hf_pos).size else None,
        "hf_pos_rms_max_mm": float(np.nanmax(hf_pos_rms) * 1e3) if finite(hf_pos_rms).size else None,
        "hf_rot_std_deg": float(np.sqrt(np.nanmean(hf_rot ** 2))) if finite(hf_rot).size else None,
        "hf_rot_rms_max_deg": float(np.nanmax(hf_rot_rms)) if finite(hf_rot_rms).size else None,
        "spike_count": int(per_state["spike"].sum()),
        "spike_frames": [int(f) for f in series.frame_index[per_state["spike"]]][:20],
    }
    return {"per_state": per_state, "summary": summary}


def _unwrap_rotvec(rv: np.ndarray) -> np.ndarray:
    """Keep consecutive rotation vectors on the same branch (q vs -q never reads as a jump)."""
    out = rv.copy()
    for i in range(1, len(out)):
        a = np.linalg.norm(out[i])
        if a > np.pi * 0.5:
            alt = out[i] - out[i] / a * 2 * np.pi
            if np.linalg.norm(alt - out[i - 1]) < np.linalg.norm(out[i] - out[i - 1]):
                out[i] = alt
    return out


# --- background / camera motion (design 12 §8.5) ------------------------------------------------

BG_SCALE = 0.5                # background features are tracked at half resolution; pixels are rescaled
BG_BORDER_PX = 12             # dataset1 mirror-pads shifted frames: ignore the outer ring


def background_motion(frames, n: int, *, exclude: dict[int, list] | None = None, exclude_radius_px: float = 140.0,
                      max_features: int = 400, min_inliers: int = 25) -> dict:
    """Frame-to-frame similarity of the static background, accumulated into an image trajectory.

    ``frames`` yields ``DecodedFrame``; ``exclude`` maps a media frame to pixel positions to mask out
    (the independently observed gripper - never the declared projection). Robust (RANSAC) fitting keeps
    the static majority; a frame whose background support is too thin gets NaN. Returns per media frame:
    ``dx, dy`` (media px, cumulative), ``rot_deg``, ``inliers`` and ``support`` (inlier ratio).
    """
    import cv2

    dx = np.full(n, np.nan)
    dy = np.full(n, np.nan)
    rot = np.full(n, np.nan)
    inliers = np.zeros(n, int)
    support = np.full(n, np.nan)
    prev = None
    prev_idx = None
    acc = np.eye(3)
    lk = dict(winSize=(21, 21), maxLevel=3, criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 0.01))
    for fr in frames:
        if fr.index >= n:
            break
        g = cv2.resize(fr.gray, None, fx=BG_SCALE, fy=BG_SCALE, interpolation=cv2.INTER_AREA)
        if prev is None:
            prev, prev_idx = g, fr.index
            dx[fr.index] = dy[fr.index] = rot[fr.index] = 0.0
            continue
        h, w = prev.shape
        mask = np.zeros_like(prev)
        b = int(BG_BORDER_PX * BG_SCALE) + 1
        mask[b:h - b, b:w - b] = 255
        for uv in (exclude or {}).get(prev_idx, []):
            cv2.circle(mask, (int(uv[0] * BG_SCALE), int(uv[1] * BG_SCALE)), int(exclude_radius_px * BG_SCALE), 0, -1)
        p0 = cv2.goodFeaturesToTrack(prev, max_features, 0.01, 8, mask=mask, blockSize=7)
        ok_frame = False
        if p0 is not None and len(p0) >= min_inliers:
            p1, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, p0, None, **lk)
            pr, st2, _ = cv2.calcOpticalFlowPyrLK(g, prev, p1, None, **lk)
            fb = np.linalg.norm((pr - p0).reshape(-1, 2), axis=1)
            good = (st.ravel() == 1) & (st2.ravel() == 1) & (fb < 0.5)
            if good.sum() >= min_inliers:
                M, inl = cv2.estimateAffinePartial2D(p0[good], p1[good], method=cv2.RANSAC,
                                                     ransacReprojThreshold=0.5)
                if M is not None and inl is not None and inl.sum() >= min_inliers:
                    ok_frame = True
                    step = np.eye(3)
                    step[:2] = M
                    step[:2, 2] /= BG_SCALE
                    acc = step @ acc
                    inliers[fr.index] = int(inl.sum())
                    support[fr.index] = float(inl.sum() / len(p0))
        if ok_frame:
            dx[fr.index], dy[fr.index] = acc[0, 2], acc[1, 2]
            rot[fr.index] = float(np.degrees(np.arctan2(acc[1, 0], acc[0, 0])))
        prev, prev_idx = g, fr.index
    return {"dx_px": dx, "dy_px": dy, "rot_deg": rot, "inliers": inliers, "support": support}


def background_hf(bg: dict, fps: float, *, cutoff_hz: float = 1.0, rolling_window_s: float = ROLLING_WINDOW_S) -> dict:
    """High-frequency part of the background trajectory (camera shake) and its rolling RMS."""
    n = len(bg["dx_px"])
    xy = np.stack([bg["dx_px"], bg["dy_px"]], 1)
    ok = np.isfinite(xy).all(1)
    hf = np.full(n, np.nan)
    if ok.sum() >= 16:
        idx = np.flatnonzero(ok)
        full = TL.interp_track(idx.astype(float), xy[ok], np.arange(n, dtype=float), gap_factor=6.0)
        good = np.isfinite(full).all(1)
        seg = full[good]
        hp = seg - _lowpass(seg, fps, cutoff_hz)
        hf_all = np.full(n, np.nan)
        hf_all[good] = np.linalg.norm(hp, axis=1)
        hf = np.where(ok, hf_all, np.nan)
    rms = _rolling_rms(hf, int(round(rolling_window_s * fps)))
    rms[~np.isfinite(hf)] = np.nan
    return {"hf_px": hf, "hf_rms_px": rms}
