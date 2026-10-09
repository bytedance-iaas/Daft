"""A wrist camera's own motion, from its pictures and from the recorded poses (design doc 22 §5.3, §7).

A camera on the gripper moves with it: the marks of its own hand stay put in the picture and cannot tell
whether the recorded trajectory is right. The camera's motion between two moments can be had twice: from
the recorded poses (its pose at both moments) and from the pictures (ORB matches between the two frames,
undistorted through the camera model, the essential matrix). The relative rotation and the direction of
travel must agree; a wrong body->optical rotation, a pose stream ahead of or behind the video, or a VIO
that jumped make them disagree. ``export-umi-mcap`` checks a few pairs (§7 row 2); F5.19 runs it over the
whole episode. Pure CPU; no model.
"""
from __future__ import annotations

import dataclasses

import numpy as np

ORB_FEATURES = 4000
RATIO = 0.75
#: fewer good matches, or fewer of them on the recovered motion, and the pair says nothing (a white wall, blur):
#: on the DAS recordings pairs under these gave the only large errors (up to 54 degrees), all others stayed under 5
MIN_MATCHES = 100
MIN_INLIERS = 40
#: pictures are matched at most this wide (DAS records 1600 x 1300)
MAX_SIDE = 960
#: the camera must have moved this far for the direction of travel to be compared, metres
MIN_TRAVEL_M = 0.01


def scene_mask(w: int, h: int, keep: float = 0.62) -> np.ndarray:
    """The scene, not the gripper: the upper ``keep`` of the picture, inside the fisheye circle."""
    import cv2

    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (w // 2, h // 2), int(0.49 * w), 255, -1)
    mask[int(keep * h):, :] = 0
    return mask


@dataclasses.dataclass
class Motion:
    """The camera from frame a to frame b, as ``cv2.recoverPose`` gives it: x_b = R x_a + t, |t| = 1."""

    R: np.ndarray
    t: np.ndarray
    inliers: int
    matches: int


def _normalized(pts: np.ndarray, K: np.ndarray, D: np.ndarray, model: str) -> np.ndarray:
    import cv2

    pts = pts.reshape(-1, 1, 2).astype(np.float64)
    if model == "opencv_fisheye":
        return cv2.fisheye.undistortPoints(pts, K, np.asarray(D, float).reshape(-1)[:4])
    if model == "opencv_brown":
        return cv2.undistortPoints(pts, K, np.asarray(D, float))
    return cv2.undistortPoints(pts, K, None)


def picture_motion(gray_a: np.ndarray, gray_b: np.ndarray, K, D, model: str, mask: np.ndarray | None = None) -> Motion | None:
    """The camera's motion between two pictures (grey, the size ``K`` is for); None when they do not match well
    enough to tell (a white wall, motion blur, too little overlap)."""
    import cv2

    h, w = gray_a.shape[:2]
    k = min(1.0, MAX_SIDE / max(w, h))
    K = np.asarray(K, float).copy()
    if k < 1.0:
        size = (int(round(w * k)), int(round(h * k)))
        gray_a, gray_b = cv2.resize(gray_a, size), cv2.resize(gray_b, size)
        mask = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST) if mask is not None else None
        K[:2] *= k
    orb = cv2.ORB_create(ORB_FEATURES)
    ka, da = orb.detectAndCompute(gray_a, mask)
    kb, db = orb.detectAndCompute(gray_b, mask)
    if da is None or db is None or len(ka) < MIN_INLIERS or len(kb) < MIN_INLIERS:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < RATIO * p[1].distance]
    if len(good) < MIN_MATCHES:
        return None
    pa = np.float64([ka[m.queryIdx].pt for m in good])
    pb = np.float64([kb[m.trainIdx].pt for m in good])
    na, nb = _normalized(pa, K, D, model), _normalized(pb, K, D, model)
    E, inl = cv2.findEssentialMat(na, nb, np.eye(3), method=cv2.RANSAC, prob=0.999, threshold=2e-3)
    if E is None or E.shape != (3, 3):
        return None
    n, R, t, _ = cv2.recoverPose(E, na, nb, np.eye(3), mask=inl)
    if n < MIN_INLIERS:
        return None
    return Motion(R, t.reshape(3), int(n), len(good))


def pose_motion(T_a: np.ndarray, T_b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The same motion from the camera's recorded poses (``T_reference_camera`` at both moments): (R, t) with
    x_b = R x_a + t, t in metres."""
    T = np.linalg.inv(T_b) @ T_a
    return T[:3, :3], T[:3, 3]


def disagreement(seen: Motion, T_a: np.ndarray, T_b: np.ndarray) -> tuple[float, float | None]:
    """(rotation difference, travel-direction difference) in degrees between the picture's motion and the
    poses'; the direction only when the camera travelled at least MIN_TRAVEL_M (it is a line: either sign)."""
    from scipy.spatial.transform import Rotation

    R, t = pose_motion(T_a, T_b)
    rot = float(np.degrees(np.linalg.norm(Rotation.from_matrix(seen.R.T @ R).as_rotvec())))
    if np.linalg.norm(t) < MIN_TRAVEL_M:
        return rot, None
    cos = abs(float(np.dot(t / np.linalg.norm(t), seen.t / np.linalg.norm(seen.t))))
    return rot, float(np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))))
