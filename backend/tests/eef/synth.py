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


# --- rendered video ----------------------------------------------------------------------------

GRIPPER_POINTS = {"finger_plus_y": (-15.0, 22.0), "finger_minus_y": (15.0, 22.0), "tcp": (0.0, 22.0)}


def _texture(h, w, seed):
    rng = np.random.default_rng(seed)
    tex = cv2.resize(rng.integers(0, 256, (h // 6 + 1, w // 6 + 1), dtype=np.uint8), (w, h),
                     interpolation=cv2.INTER_NEAREST)
    return cv2.GaussianBlur(tex, (3, 3), 0)


def gripper_motion(n: int):
    t = np.arange(n) / FPS
    centre = np.stack([160 + 55 * np.sin(0.9 * t), 115 + 30 * np.sin(1.3 * t + 0.4)], 1)
    angle = np.radians(12.0 * np.sin(0.8 * t))
    return centre, angle


def gripper_truth(n: int, *, shake: np.ndarray | None = None) -> dict[str, np.ndarray]:
    centre, angle = gripper_motion(n)
    out = {}
    for pid, (x, y) in GRIPPER_POINTS.items():
        c, s = np.cos(angle), np.sin(angle)
        out[pid] = np.stack([centre[:, 0] + c * x - s * y, centre[:, 1] + s * x + c * y], 1)
        if shake is not None:
            out[pid] = out[pid] + shake
    return out


def render_gripper_video(path: pathlib.Path, n: int = 60, *, occlude: tuple[int, int] | None = None,
                         shake: np.ndarray | None = None, lead_frames: int = 0) -> dict[str, np.ndarray]:
    """Encode an H.264 clip of a textured rigid 'gripper' over a textured static background.

    ``occlude=(f0, f1)`` hides the gripper behind a static board for frames [f0, f1);
    ``shake`` (n, 2) moves the whole frame (camera shake); ``lead_frames`` prepends frames of another
    'episode' so the clip starts at ``lead_frames / FPS`` in the file (LeRobot v3 concatenation).
    Returns the true pixels of GRIPPER_POINTS on the clip's frames.
    """
    import av

    bg = cv2.cvtColor(_texture(H, W, 1), cv2.COLOR_GRAY2BGR)
    tex = cv2.cvtColor(_texture(70, 50, 2), cv2.COLOR_GRAY2BGR)
    tex = cv2.rectangle(tex, (0, 0), (49, 69), (255, 255, 255), 2)
    centre, angle = gripper_motion(n)
    board = cv2.cvtColor(_texture(H, 120, 3), cv2.COLOR_GRAY2BGR)
    container = av.open(str(path), "w")
    stream = container.add_stream("libx264", rate=int(FPS))
    stream.width, stream.height, stream.pix_fmt = W, H, "yuv420p"
    stream.options = {"crf": "12", "g": "10"}

    def emit(img):
        for pkt in stream.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
            container.mux(pkt)

    for i in range(lead_frames):
        emit(np.full((H, W, 3), (i * 7) % 255, np.uint8))
    for i in range(n):
        img = bg.copy()
        c, s = np.cos(angle[i]), np.sin(angle[i])
        M = np.array([[c, -s, 0], [s, c, 0]], float)
        M[:, 2] = centre[i] - M[:, :2] @ np.array([25.0, 35.0])
        warped = cv2.warpAffine(tex, M, (W, H))
        mask = cv2.warpAffine(np.full((70, 50), 255, np.uint8), M, (W, H))
        img[mask > 127] = warped[mask > 127]
        if occlude and occlude[0] <= i < occlude[1]:
            img[:, 100:220] = board
        if shake is not None:
            img = cv2.warpAffine(img, np.array([[1, 0, shake[i, 0]], [0, 1, shake[i, 1]]], float), (W, H),
                                 borderMode=cv2.BORDER_REFLECT)
        emit(img)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()
    return gripper_truth(n, shake=shake)


def write_seeds(path: pathlib.Path, truth: dict[str, np.ndarray], *, sample_id: str, camera_id: str,
                every: int = 15, occluded: tuple[int, int] | None = None, hashes: dict[int, str] | None = None):
    """Seed rows (observation schema, synthetic_fixture) at every ``every`` frames from the truth."""
    n = len(next(iter(truth.values())))
    rows = []
    for i in range(0, n, every):
        hidden = occluded is not None and occluded[0] <= i < occluded[1]
        pts = {pid: ({"uv_px": None, "visibility": "occluded", "confidence": 0.0, "uncertainty_px": None} if hidden
                     else {"uv_px": [round(float(uv[i, 0]), 2), round(float(uv[i, 1]), 2)], "visibility": "visible",
                           "confidence": 1.0, "uncertainty_px": None})
               for pid, uv in truth.items()}
        rows.append({"schema_version": "eef-video/1.0.0", "sample_id": sample_id, "frame_index": i,
                     "camera_id": camera_id, "video_frame_index": i, "pixel_space": "media",
                     "method": "synthetic_fixture", "model_version": "synthetic-test/1.0",
                     "input_image_sha256": (hashes or {}).get(i, "0" * 64),
                     "projection_visible_to_localizer": False, "points": pts})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def make_entry_2d(truth: dict[str, np.ndarray], *, ep: int = 0, shift_frames: int = 0,
                  offset_px: tuple[float, float] = (0.0, 0.0), video_uri: str = "videos/cam/episode_000000.mp4",
                  dataset: str = "synthetic") -> dict:
    """Form A entry (2D projections only, no pose / calibration) declaring the rendered gripper points.

    ``shift_frames=k`` declares frame i with the truth of frame i-k (the record lags the video by k frames,
    lag = +k/FPS under D-E8); ``offset_px`` adds a constant image offset.
    """
    n = len(next(iter(truth.values())))
    sid = f"{dataset}_{ep:06d}"
    cam = "cam0"
    prov = {"source": "synthetic", "method": "rendered", "assurance": "synthetic"}
    points = {pid: {"meaning": f"rendered {pid}", "frame_id": None, "model": "external_2d", "position_eef_m": None,
                    "position_open_eef_m": None, "position_closed_eef_m": None, "provenance": prov}
              for pid in GRIPPER_POINTS}
    sample = {
        "schema_version": "eef-video/1.0.0", "sample_id": sid,
        "source": {"dataset": dataset, "episode_id": str(ep), "instruction": None},
        "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": None,
        "eef_frame": None, "reference_frame": None,
        "views": [{"view_id": cam, "kind": "camera", "camera_id": cam, "mount": "fixed_external",
                   "media": {"kind": "video", "uri": video_uri, "image_size_wh": [W, H], "frame_count": n,
                             "fps": FPS, "clip_start_s": 0.0, "clip_end_s": n / FPS}}],
        "point_definitions": points,
        "axis_definitions": {"finger_line": {"start_point_id": "finger_minus_y", "end_point_id": "finger_plus_y",
                                             "physical_meaning": "finger line", "directed": False, "length_m": None}},
        "raw_pose_sequence": None, "notes": ["synthetic 2D-only scene"]}
    frames = []
    for i in range(n):
        src = min(max(i - shift_frames, 0), n - 1)
        pts = {}
        for pid, uv in truth.items():
            u, v = float(uv[src, 0] + offset_px[0]), float(uv[src, 1] + offset_px[1])
            inside = 0 <= u < W and 0 <= v < H
            pts[pid] = {"uv_px": [u, v], "depth_m": None, "status": "valid" if inside else "out_of_frame",
                        "in_frame": inside}
        frames.append({"schema_version": "eef-video/1.0.0", "sample_id": sid, "frame_index": i, "timestamp_s": i / FPS,
                       "source_state_index": None, "source_timing": [], "eef": None, "gripper": None,
                       "cameras": {cam: {"video_frame_index": i, "video_timestamp_s": i / FPS, "image_size_wh": [W, H],
                                         "calibration_id": None, "T_reference_camera": None,
                                         "H_media_from_calibration": np.eye(3).tolist(),
                                         "projection": {"source": "provided", "pixel_space": "media",
                                                        "points": pts}}}})
    return {"episode_index": ep, "sample": sample, "calibration": None, "frames": frames}
