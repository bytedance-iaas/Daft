"""Past tool positions expressed in the current camera, shared by EEF and UMI opinions."""
from __future__ import annotations

import numpy as np

from . import contracts as C, geometry as G

HORIZON_S = 1.0
COLOR = (255, 220, 0)  # BGR: cyan, distinct from the current red/orange geometry


def indices(t, frame: int, horizon_s: float = HORIZON_S) -> np.ndarray:
    """Contiguous history in [t - horizon_s, t], ending at frame; never select future samples."""
    if t is None or not np.isfinite(t[frame]):
        return np.array([], dtype=int)
    past = t[:frame + 1]
    # Include the exact one-second boundary despite a one-ULP subtraction roundoff.
    cutoff = np.nextafter(float(t[frame] - horizon_s), -np.inf)
    ids = np.flatnonzero(np.isfinite(past) & (past >= cutoff) & (past <= t[frame]))
    dt = np.diff(t)
    positive = dt[np.isfinite(dt) & (dt > 0)]
    step = float(np.median(positive)) if len(positive) else np.inf
    gaps = np.flatnonzero((np.diff(ids) != 1) | (np.diff(t[ids]) <= 0) | (np.diff(t[ids]) > 1.5 * step))
    return ids[gaps[-1] + 1:] if len(gaps) else ids


def times(sample, camera_id: str):
    """Prefer the sample clock; index-only inputs can use their paired video clock."""
    if sample.t is not None:
        return sample.t
    cam = sample.cameras[camera_id]
    if np.isfinite(cam.video_timestamp_s).any():
        return cam.video_timestamp_s
    fps = float(cam.media.get("fps") or 0)
    if np.isfinite(fps) and fps > 0:
        return np.where(cam.video_frame_index >= 0, cam.video_frame_index / fps, np.nan)
    return None


def project_eef(sample, camera_id: str, frame: int, point_id: str, current_uv) -> np.ndarray:
    """Project P's history through this frame's camera without changing the supplied current P.

    An empty trail means missing time/geometry or a conflict with the declared projection.
    NaN points remain in the result so drawing cannot bridge a missing pose or negative depth.
    """
    empty = np.empty((0, 2))
    ids = indices(times(sample, camera_id), frame)
    cam = sample.cameras[camera_id]
    cal, definition = cam.calibration(sample), sample.points.get(point_id)
    if not len(ids) or cal is None or definition is None or current_uv is None:
        return empty
    offsets = G.point_offsets(definition, sample.gripper[ids])
    if offsets is None:
        return empty
    uv, _ = G.project_chain(sample.T_reference_eef[ids], offsets, cam.T_reference_camera[frame],
                            cal["K"], cal["model"], cal["distortion_coefficients"], cam.H[frame])
    if (not np.isfinite([uv[-1], current_uv]).all()
            or np.linalg.norm(uv[-1] - current_uv) >= C.REPROJECTION_TOLERANCE_PX):
        return empty
    return uv


def draw(img, uv: np.ndarray, scale: float):
    """Draw history underneath the current markers, with clipping and gaps preserved."""
    import cv2

    out = img.copy()
    h, w = out.shape[:2]
    for a, b in zip(uv[:-1], uv[1:]):
        if not np.isfinite([a, b]).all():
            continue
        p, q = (tuple(np.clip(np.rint(x * scale), -1000000, 1000000).astype(int)) for x in (a, b))
        visible, p, q = cv2.clipLine((0, 0, w, h), p, q)
        if visible:
            cv2.line(out, p, q, COLOR, 2, cv2.LINE_AA)
    return out


def prompt(horizon_s: float = HORIZON_S, point: str = "P") -> str:
    """The same temporal interpretation for both kinds of marked video."""
    return (
        f"Each colored trail is up to {horizon_s:g} seconds of PAST recorded {point} positions, ending at "
        f"the CURRENT frame's {point} circle. All historical positions are reprojected into the CURRENT frame's camera. "
        "It is the recent path already traveled, not a future plan or a predicted object path. "
        "Only the trail's current endpoint represents the current gripper. The past points need not coincide with "
        "their screen positions in earlier video frames because the camera may have moved. Missing/behind-camera "
        "points are not drawn, and trails do not connect across missing samples or timestamp gaps. "
        "A missing trail alone is not evidence of a motion error."
    )
