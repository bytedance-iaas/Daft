"""Synthetic EEF-video scenes for the tests: a bundle whose provided projections come from OpenCV
(an implementation independent of ``geometry.py``) and, when asked, a rendered video in which a
textured gripper follows the true geometry over a textured static background.
"""
from __future__ import annotations

import copy
import json
import pathlib

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

FPS = 15.0
W, H = 320, 240
K = np.array([[300.0, 0.0, 161.0], [0.0, 300.0, 119.0], [0.0, 0.0, 1.0]])
TCP = np.array([0.0, 0.0, 0.16])
HALF_OPEN = 0.0425


def camera_pose() -> np.ndarray:
    """T_base_camera: camera 1.2 m in front of the workspace, looking back at it."""
    c = np.array([1.5, 0.05, 0.62])
    z = np.array([0.55, 0.0, 0.30]) - c
    z /= np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    T = np.eye(4)
    T[:3, :3] = np.stack([x, np.cross(z, x), z], 1)
    T[:3, 3] = c
    return T


def eef_trajectory(n: int, *, jitter_m: float = 0.0, seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Positions (n,3), quaternions xyzw (n,4) and closed fraction (n,) of a smooth reach-and-grasp."""
    t = np.arange(n) / FPS
    pos = np.stack([0.55 + 0.12 * np.sin(0.5 * t), 0.10 * np.sin(0.8 * t + 0.3), 0.30 + 0.06 * np.cos(0.6 * t)], 1)
    if jitter_m:
        rng = np.random.default_rng(seed)
        pos = pos + jitter_m * np.sin(2 * np.pi * 5.0 * t)[:, None] * np.array([1.0, 0.7, 0.5]) \
            + 0.3 * jitter_m * rng.standard_normal(pos.shape)
    eul = np.stack([np.pi + 0.15 * np.sin(0.4 * t), 0.1 * np.cos(0.3 * t), 0.4 * np.sin(0.25 * t)], 1)
    quat = Rotation.from_euler("xyz", eul).as_quat()
    grip = np.clip(0.5 - 0.5 * np.cos(0.4 * t), 0, 1)
    return pos, quat, grip


def cv_project(points_base: np.ndarray, T_base_cam: np.ndarray, *, K_=K, dist=None, fisheye=False):
    T_cam_base = np.linalg.inv(T_base_cam)
    rvec, _ = cv2.Rodrigues(T_cam_base[:3, :3])
    tvec = T_cam_base[:3, 3]
    pts = np.asarray(points_base, float).reshape(-1, 1, 3)
    if fisheye:
        uv, _ = cv2.fisheye.projectPoints(pts, rvec.reshape(1, 1, 3), tvec.reshape(1, 1, 3), K_,
                                          np.asarray(dist, float).reshape(4, 1))
    else:
        uv, _ = cv2.projectPoints(pts, rvec, tvec, K_, None if dist is None else np.asarray(dist, float))
    depth = (np.c_[points_base, np.ones(len(points_base))] @ T_cam_base.T)[:, 2]
    return uv.reshape(-1, 2), depth


POINTS = {
    "eef_origin": {"meaning": "flange origin", "frame_id": "panda_link8", "model": "fixed",
                   "position_eef_m": [0, 0, 0], "position_open_eef_m": None, "position_closed_eef_m": None,
                   "provenance": {"source": "synthetic", "method": "tool geometry", "assurance": "synthetic"}},
    "tcp": {"meaning": "finger centre", "frame_id": "panda_link8", "model": "fixed",
            "position_eef_m": TCP.tolist(), "position_open_eef_m": None, "position_closed_eef_m": None,
            "provenance": {"source": "synthetic", "method": "tool geometry", "assurance": "synthetic"}},
    "tcp_z": {"meaning": "approach axis end", "frame_id": "panda_link8", "model": "fixed",
              "position_eef_m": [0, 0, 0.22], "position_open_eef_m": None, "position_closed_eef_m": None,
              "provenance": {"source": "synthetic", "method": "tool geometry", "assurance": "synthetic"}},
    "finger_plus_y": {"meaning": "finger tip +y", "frame_id": "panda_link8", "model": "linear_gripper",
                      "position_eef_m": None, "position_open_eef_m": [0, HALF_OPEN, 0.16],
                      "position_closed_eef_m": [0, 0, 0.16],
                      "provenance": {"source": "synthetic", "method": "tool geometry", "assurance": "synthetic"}},
    "finger_minus_y": {"meaning": "finger tip -y", "frame_id": "panda_link8", "model": "linear_gripper",
                       "position_eef_m": None, "position_open_eef_m": [0, -HALF_OPEN, 0.16],
                       "position_closed_eef_m": [0, 0, 0.16],
                       "provenance": {"source": "synthetic", "method": "tool geometry", "assurance": "synthetic"}},
}
AXES = {
    "z": {"start_point_id": "tcp", "end_point_id": "tcp_z", "physical_meaning": "local_z", "directed": True,
          "length_m": 0.06},
    "finger_line": {"start_point_id": "finger_minus_y", "end_point_id": "finger_plus_y",
                    "physical_meaning": "finger line", "directed": False, "length_m": None},
}


def point_offset(pid: str, g: float) -> np.ndarray:
    d = POINTS[pid]
    if d["model"] == "fixed":
        return np.asarray(d["position_eef_m"], float)
    return (1 - g) * np.asarray(d["position_open_eef_m"]) + g * np.asarray(d["position_closed_eef_m"])


def truth_pixels(pos, quat, grip, T_base_cam, *, dist=None, fisheye=False) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per point: ((n,2) media pixels, (n,) depth) via OpenCV."""
    R = Rotation.from_quat(quat).as_matrix()
    out = {}
    for pid in POINTS:
        pb = np.stack([R[i] @ point_offset(pid, grip[i]) + pos[i] for i in range(len(pos))])
        out[pid] = cv_project(pb, T_base_cam, dist=dist, fisheye=fisheye)
    return out


def make_entry(ep: int = 0, n: int = 60, *, pos=None, quat=None, grip=None, declared_T=None,
               video_uri: str = "videos/cam/episode_000000.mp4", dataset: str = "synthetic") -> dict:
    """One bundle entry (sample, calibration, frames) with provided projections from OpenCV."""
    if pos is None:
        pos, quat, grip = eef_trajectory(n)
    T_bc = camera_pose() if declared_T is None else declared_T
    px = truth_pixels(pos, quat, grip, T_bc)
    sid = f"{dataset}_{ep:06d}"
    cam = "cam0"
    sample = {
        "schema_version": "eef-video/1.0.0", "sample_id": sid,
        "source": {"dataset": dataset, "episode_id": str(ep), "instruction": None},
        "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": "#calibration",
        "eef_frame": "panda_link8", "reference_frame": "robot_base",
        "views": [{"view_id": cam, "kind": "camera", "camera_id": cam, "mount": "fixed_external",
                   "media": {"kind": "video", "uri": video_uri, "image_size_wh": [W, H], "frame_count": n,
                             "fps": FPS, "clip_start_s": 0.0, "clip_end_s": n / FPS}}],
        "point_definitions": copy.deepcopy(POINTS), "axis_definitions": copy.deepcopy(AXES),
        "raw_pose_sequence": None, "notes": ["synthetic test scene"]}
    calibration = {"schema_version": "eef-video/1.0.0", "calibrations": {f"{cam}_declared": {
        "camera_id": cam, "reference_frame": "robot_base", "image_size_wh": [W, H], "image_space": "rectified",
        "model": "pinhole", "K": K.tolist(), "distortion_coefficients": [], "extrinsics_mode": "static",
        "T_reference_camera": T_bc.tolist(),
        "provenance": {"source": "synthetic", "method": "copy", "assurance": "declared"}}}}
    frames = []
    for i in range(n):
        pts = {}
        for pid, (uv, depth) in px.items():
            u, v = map(float, uv[i])
            inside = 0 <= u < W and 0 <= v < H
            pts[pid] = {"uv_px": [u, v], "depth_m": float(depth[i]), "status": "valid" if inside else "out_of_frame",
                        "in_frame": inside}
        frames.append({
            "schema_version": "eef-video/1.0.0", "sample_id": sid, "frame_index": i, "timestamp_s": i / FPS,
            "source_state_index": i,
            "source_timing": [{"channel": "robot_state", "timestamp": str(1704137533000 + int(i * 1000 / FPS) + 3),
                               "unit": "ms", "clock_id": "synthetic_ms", "semantics": "robot read_start"}],
            "eef": {"pose_type": "absolute", "frame_id": "panda_link8", "reference_frame": "robot_base",
                    "position_m": pos[i].tolist(), "quaternion_xyzw": quat[i].tolist(), "relative_to": None,
                    "provenance": {"source": "synthetic", "method": "copy", "assurance": "declared"}},
            "gripper": {"closed_fraction": float(grip[i]), "opening_m": None,
                        "provenance": {"source": "synthetic", "method": "copy", "assurance": "declared"}},
            "cameras": {cam: {"video_frame_index": i, "video_timestamp_s": i / FPS, "image_size_wh": [W, H],
                              "calibration_id": f"{cam}_declared", "T_reference_camera": None,
                              "H_media_from_calibration": np.eye(3).tolist(),
                              "projection": {"source": "provided", "pixel_space": "media", "points": pts}}}})
    return {"episode_index": ep, "sample": sample, "calibration": calibration, "frames": frames}


def make_bundle(entries: list[dict], *, dataset: str = "synthetic") -> dict:
    return {"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
            "dataset": {"id": dataset, "lerobot_codebase_version": "v2.1", "fps": FPS,
                        "episode_count": max(e["episode_index"] for e in entries) + 1},
            "media_uri_base": "lerobot_root", "samples": entries}


def write_bundle(path: pathlib.Path, bundle: dict) -> pathlib.Path:
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path
