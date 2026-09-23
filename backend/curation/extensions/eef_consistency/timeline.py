"""Timelines, pairing and interpolation (design 12 §6.2).

The main timeline is the sample's ``timestamp_s`` (video PTS local to the clip). Robot positions
interpolate linearly, rotations by shortest-arc SLERP, the gripper by zero-order hold; a gap longer
than ``gap_factor`` x the local median step splits the series instead of being bridged.
"""
from __future__ import annotations

import numpy as np

from . import geometry as G


def median_step(t: np.ndarray) -> float:
    d = np.diff(np.asarray(t, float))
    d = d[np.isfinite(d) & (d > 0)]
    return float(np.median(d)) if len(d) else float("nan")


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Half-open index runs ``[start, end)`` where ``mask`` is true."""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]])
    edges = np.flatnonzero(np.diff(m.astype(np.int8)))
    return [(int(a), int(b)) for a, b in zip(edges[0::2], edges[1::2])]


def interp_track(t_src: np.ndarray, x_src: np.ndarray, t_query: np.ndarray, *,
                 gap_factor: float = 2.0) -> np.ndarray:
    """Linear interpolation of an (N, D) series; NaN outside the support or across long gaps."""
    t_src = np.asarray(t_src, float)
    x_src = np.asarray(x_src, float)
    squeeze = x_src.ndim == 1
    if squeeze:
        x_src = x_src[:, None]
    t_query = np.asarray(t_query, float)
    ok = np.isfinite(t_src) & np.isfinite(x_src).all(-1)
    out = np.full((len(t_query), x_src.shape[1]), np.nan)
    if ok.sum() < 2:
        return out[:, 0] if squeeze else out
    ts, xs = t_src[ok], x_src[ok]
    max_gap = gap_factor * median_step(np.asarray(t_src, float)[np.isfinite(t_src)])
    j = np.searchsorted(ts, t_query, side="right")
    inside = (j > 0) & (j < len(ts)) & np.isfinite(t_query)
    exact = np.isin(t_query, ts)
    jj = np.clip(j, 1, len(ts) - 1)
    t0, t1 = ts[jj - 1], ts[jj]
    gap_ok = (t1 - t0) <= max_gap + 1e-9
    w = np.where(t1 > t0, (t_query - t0) / np.where(t1 > t0, t1 - t0, 1.0), 0.0)[:, None]
    val = (1 - w) * xs[jj - 1] + w * xs[jj]
    good = inside & gap_ok
    out[good] = val[good]
    if exact.any():
        k = np.searchsorted(ts, t_query[exact])
        out[np.flatnonzero(exact)] = xs[np.clip(k, 0, len(ts) - 1)]
    return out[:, 0] if squeeze else out


def shift_track(t: np.ndarray, x: np.ndarray, lag_s: float, *, gap_factor: float = 2.0) -> np.ndarray:
    """``x(t + lag)`` sampled on ``t`` (used to compensate a declared track by a lag, D-E8)."""
    return interp_track(t, x, np.asarray(t, float) + lag_s, gap_factor=gap_factor)


def slerp_rotations(t_src: np.ndarray, R_src: np.ndarray, t_query: np.ndarray) -> np.ndarray:
    """Shortest-arc SLERP of (N, 3, 3) rotations at ``t_query`` (NaN outside the support)."""
    t_src = np.asarray(t_src, float)
    out = np.full((len(t_query), 3, 3), np.nan)
    j = np.searchsorted(t_src, t_query, side="right")
    inside = (j > 0) & (j < len(t_src))
    exact = np.isin(t_query, t_src)
    for q in np.flatnonzero(inside | exact):
        if exact[q]:
            out[q] = R_src[int(np.searchsorted(t_src, t_query[q]))]
            continue
        a, b = j[q] - 1, j[q]
        w = (t_query[q] - t_src[a]) / (t_src[b] - t_src[a])
        rel = G.rotation_log(R_src[a].T @ R_src[b])
        out[q] = R_src[a] @ _exp(rel * w)
    return out


def _exp(rotvec: np.ndarray) -> np.ndarray:
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return np.eye(3)
    k = rotvec / theta
    Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(theta) * Kx + (1 - np.cos(theta)) * Kx @ Kx


def zero_order_hold(t_src: np.ndarray, x_src: np.ndarray, t_query: np.ndarray) -> np.ndarray:
    t_src = np.asarray(t_src, float)
    j = np.searchsorted(t_src, t_query, side="right") - 1
    out = np.full(len(t_query), np.nan)
    ok = j >= 0
    out[ok] = np.asarray(x_src, float)[j[ok]]
    return out


def uniform_grid(t: np.ndarray, step: float | None = None) -> np.ndarray:
    t = np.asarray(t, float)
    step = step or median_step(t)
    n = int(np.floor((t[-1] - t[0]) / step + 1e-9)) + 1
    return t[0] + step * np.arange(n)
