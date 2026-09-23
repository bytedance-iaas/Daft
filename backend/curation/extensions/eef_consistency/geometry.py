"""SE(3), tool points, camera models and the projection chain (design 12 §6.1, format §5).

Conventions: right-handed, column vectors, active rotations; ``T_A_B`` maps coordinates in B to A;
quaternions are stored ``(x, y, z, w)``. Pure numpy; every function is vectorised over frames.

    p_reference = T_reference_eef · p_eef
    p_camera    = inverse(T_reference_camera) · p_reference
    uv_calib    = project(K, model, coefficients, p_camera)
    uv_media    = normalize(H_media_from_calibration · [u, v, 1])
"""
from __future__ import annotations

import numpy as np

PINHOLE, BROWN, FISHEYE = "pinhole", "opencv_brown", "opencv_fisheye"


# --- rotations -------------------------------------------------------------------------------

def quat_xyzw_to_matrix(q: np.ndarray) -> np.ndarray:
    """(..., 4) unit quaternions (x, y, z, w) -> (..., 3, 3) rotation matrices."""
    q = np.asarray(q, dtype=float)
    q = q / np.linalg.norm(q, axis=-1, keepdims=True)
    x, y, z, w = np.moveaxis(q, -1, 0)
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], -2)


def rotation_log(R: np.ndarray) -> np.ndarray:
    """(..., 3, 3) rotations -> (..., 3) rotation vectors (axis * angle, radians)."""
    R = np.asarray(R, dtype=float)
    cos = np.clip((np.trace(R, axis1=-2, axis2=-1) - 1.0) / 2.0, -1.0, 1.0)
    angle = np.arccos(cos)
    vee = np.stack([R[..., 2, 1] - R[..., 1, 2], R[..., 0, 2] - R[..., 2, 0],
                    R[..., 1, 0] - R[..., 0, 1]], -1)
    sin = np.sin(angle)
    small = sin < 1e-6
    with np.errstate(invalid="ignore", divide="ignore"):
        out = vee * (angle / (2.0 * np.where(small, 1.0, sin)))[..., None]
    # near 0: first-order; near pi: axis from the symmetric part
    out = np.where(small[..., None] & (angle[..., None] < 1.0), vee / 2.0, out)
    near_pi = small & (angle >= 1.0)
    if np.any(near_pi):
        Rp = R[near_pi]
        B = (Rp + np.eye(3)) / 2.0
        idx = np.argmax(np.diagonal(B, axis1=-2, axis2=-1), axis=-1)
        axis = B[np.arange(len(Rp)), :, idx]
        axis /= np.linalg.norm(axis, axis=-1, keepdims=True)
        out[near_pi] = axis * np.pi
    return out


def rotation_angle_deg(R: np.ndarray) -> np.ndarray:
    return np.degrees(np.linalg.norm(rotation_log(R), axis=-1))


def se3(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    R = np.asarray(R, dtype=float)
    t = np.asarray(t, dtype=float)
    T = np.zeros(R.shape[:-2] + (4, 4))
    T[..., :3, :3] = R
    T[..., :3, 3] = t
    T[..., 3, 3] = 1.0
    return T


def pose_matrix(position_m, quaternion_xyzw) -> np.ndarray:
    return se3(quat_xyzw_to_matrix(quaternion_xyzw), position_m)


def se3_inverse(T: np.ndarray) -> np.ndarray:
    T = np.asarray(T, dtype=float)
    R = np.swapaxes(T[..., :3, :3], -1, -2)
    return se3(R, -np.einsum("...ij,...j->...i", R, T[..., :3, 3]))


def is_rigid(T, atol: float = 1e-6) -> bool:
    T = np.asarray(T, dtype=float)
    if T.shape != (4, 4) or not np.all(np.isfinite(T)):
        return False
    if not np.allclose(T[3], [0, 0, 0, 1], atol=1e-8):
        return False
    R = T[:3, :3]
    return bool(np.allclose(R.T @ R, np.eye(3), atol=atol) and abs(np.linalg.det(R) - 1) < atol)


# --- tool points -----------------------------------------------------------------------------

def point_offsets(definition: dict, closed_fraction: np.ndarray) -> np.ndarray | None:
    """(N, 3) point positions in the EEF frame for every frame, NaN where not computable.

    ``fixed`` -> constant; ``linear_gripper`` -> ``(1-g) p_open + g p_closed`` with the frame's
    ``closed_fraction`` (unknown g -> NaN); ``external_2d`` -> None (no 3D model).
    """
    n = len(closed_fraction)
    model = definition["model"]
    if model == "fixed":
        return np.repeat(np.asarray(definition["position_eef_m"], float)[None], n, axis=0)
    if model == "linear_gripper":
        g = np.asarray(closed_fraction, float)[:, None]
        p_open = np.asarray(definition["position_open_eef_m"], float)[None]
        p_closed = np.asarray(definition["position_closed_eef_m"], float)[None]
        return (1.0 - g) * p_open + g * p_closed
    return None


# --- camera models ---------------------------------------------------------------------------

def _distort_brown(x: np.ndarray, y: np.ndarray, k: np.ndarray):
    k1, k2, p1, p2, k3 = k
    r2 = x * x + y * y
    radial = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    return xd, yd


def _distort_fisheye(x: np.ndarray, y: np.ndarray, k: np.ndarray):
    k1, k2, k3, k4 = k
    r = np.sqrt(x * x + y * y)
    theta = np.arctan(r)
    t2 = theta * theta
    theta_d = theta * (1 + k1 * t2 + k2 * t2 * t2 + k3 * t2 ** 3 + k4 * t2 ** 4)
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(r > 1e-12, theta_d / np.where(r > 1e-12, r, 1.0), 1.0)
    return x * scale, y * scale


def project_camera_points(p_cam: np.ndarray, K, model: str = PINHOLE,
                          coefficients=()) -> tuple[np.ndarray, np.ndarray]:
    """(..., 3) camera-frame points -> ((..., 2) calibration pixels, (...,) depth).

    Points with depth <= 0 or non-finite coordinates get NaN pixels (format §4.4 behind_camera).
    """
    p = np.asarray(p_cam, dtype=float)
    K = np.asarray(K, dtype=float)
    z = p[..., 2]
    ok = np.isfinite(p).all(-1) & (z > 0)
    zs = np.where(ok, z, 1.0)
    x, y = p[..., 0] / zs, p[..., 1] / zs
    k = np.asarray(coefficients, dtype=float)
    if model == BROWN:
        x, y = _distort_brown(x, y, k)
    elif model == FISHEYE:
        x, y = _distort_fisheye(x, y, k)
    elif model != PINHOLE:
        raise ValueError(f"unknown camera model {model!r}")
    u = K[0, 0] * x + K[0, 1] * y + K[0, 2]
    v = K[1, 1] * y + K[1, 2]
    uv = np.stack([u, v], -1)
    uv[~ok] = np.nan
    return uv, np.where(np.isfinite(z), z, np.nan)


def apply_homography(H, uv: np.ndarray) -> np.ndarray:
    """(3, 3) or (N, 3, 3) pixel transforms applied to (N, 2) pixels; NaN stays NaN."""
    uv = np.asarray(uv, dtype=float)
    H = np.asarray(H, dtype=float)
    hom = np.concatenate([uv, np.ones(uv.shape[:-1] + (1,))], -1)
    out = np.einsum("...ij,...j->...i", H, hom) if H.ndim == 3 else hom @ H.T
    with np.errstate(invalid="ignore", divide="ignore"):
        return out[..., :2] / out[..., 2:3]


def project_chain(T_reference_eef: np.ndarray, offsets_eef: np.ndarray, T_reference_camera: np.ndarray,
                  K, model: str, coefficients, H) -> tuple[np.ndarray, np.ndarray]:
    """Full chain for N frames: EEF poses (N,4,4), point offsets (N,3), camera poses (4,4) or (N,4,4).

    Returns media pixels (N, 2) and camera depth (N,). NaN poses/offsets propagate as NaN.
    """
    p_eef = np.concatenate([offsets_eef, np.ones((len(offsets_eef), 1))], -1)
    p_ref = np.einsum("nij,nj->ni", T_reference_eef, p_eef)
    T_cam_ref = se3_inverse(T_reference_camera)
    if T_cam_ref.ndim == 2:
        p_cam = p_ref @ T_cam_ref.T
    else:
        p_cam = np.einsum("nij,nj->ni", T_cam_ref, p_ref)
    uv, depth = project_camera_points(p_cam[:, :3], K, model, coefficients)
    return apply_homography(H, uv), depth


def reference_points(T_reference_eef: np.ndarray, offsets_eef: np.ndarray) -> np.ndarray:
    """(N, 3) points in the reference frame (for PnP and depth-scaled units)."""
    p_eef = np.concatenate([offsets_eef, np.ones((len(offsets_eef), 1))], -1)
    return np.einsum("nij,nj->ni", T_reference_eef, p_eef)[:, :3]
