"""A small DAS-like handheld-gripper recording for the UMI mcap exporter (design doc 22 §5.2, F5.18).

Two hands in the DAS layout, protobuf with the recorder's ``header.timestamp``: per hand
``/robotN/vio/eef_pose`` (the VIO body: x forward, y left, z up), ``/robotN/sensor/magnetic_encoder``
(metres), ``/robotN/sensor/camera0/compressed`` (H.264 Annex B, one access unit a message) and
``/robotN/sensor/camera0/camera_info`` (equidistant at 320 x 240 for a 480 x 390 video, ``T_b_c``).

- robot0: header time = log time (as on the DAS recordings); its video starts in the middle of a GOP (the
  first ``FIRST_KEY`` messages cannot be decoded); its poses start ``POSE_FROM`` frames in (the checks'
  clock anchor); the poses of ``GAP0`` are missing (bridged by default); its pictures are a textured room
  seen through the true camera poses, so the camera's own motion can be read from them.
- robot1: no camera_info (the calibration's fallback); its device clock is not the host's; the poses of
  ``GAP1`` are missing (longer than the default bridges); its pictures are plain (nothing to match).
"""
from __future__ import annotations

import copy
import math
import os
from fractions import Fraction

import numpy as np

FPS = 30
STEP = 33_333_333
N = 90
L0 = 1_766_000_000_000_000_000
#: robot0's first encoded frames are not in the file; the GOP is KEYINT long
PRE, KEYINT = 5, 12
FIRST_KEY = KEYINT - PRE
POSE_FROM = 15
GAP0 = (30, 31)
GAP1 = tuple(range(40, 46))
#: robot1's device clock runs this far behind the host's
CLOCK1 = 3_000_000_000_000
#: log-time latencies after the frame's moment (ns): robot0 pose, opening, camera; robot1 pose, camera
LAT = {"pose0": 2_000_000, "open0": 3_000_000, "cam0": 12_000_000, "pose1": 4_000_000, "cam1": 19_000_000}
WH = (480, 390)
CAL_WH = (320, 240)
#: stretched onto the video (x 1.5, x 1.625) it is fx = fy = 300, the centre in the middle
K_CAL = [[200.0, 0.0, 160.0], [0.0, 300.0 * 240 / 390, 120.0], [0.0, 0.0, 1.0]]
D = [0.02, -0.01, 0.003, -0.001]
R_BO = [[0, 0, 1], [-1, 0, 0], [0, -1, 0]]
T_BC = [0.04, 0.0, 0.03]
T_CT = [[1, 0, 0, 0], [0, 1, 0, 0.086], [0, 0, 1, 0.17], [0, 0, 0, 1]]
#: the room robot0 films: (axis, value) of each wall, and boxes in front of the far one (min, max corners) -
#: a scene in depth, as a handheld recording sees (one plane leaves the camera's motion ambiguous)
PLANES = [(0, 1.6), (1, 1.2), (1, -1.2), (2, 1.0), (2, -0.7)]
BOXES = [((1.0, -0.5, -0.2), (1.25, -0.15, 0.35)), ((0.85, 0.2, 0.0), (1.05, 0.55, 0.6)),
         ((1.3, -0.05, 0.25), (1.45, 0.25, 0.7)), ((1.2, 0.55, -0.3), (1.4, 0.9, 0.2)),
         ((0.95, -0.95, 0.1), (1.15, -0.65, 0.5))]


def body_pose(hand: str, i: int) -> np.ndarray:
    """``T_world_body`` of a hand at frame i (its own VIO world)."""
    from scipy.spatial.transform import Rotation

    t = i / FPS
    s = lambda period, phase=0.0: math.sin(2 * math.pi * t / period + phase)  # noqa: E731
    if hand == "robot0":
        p = [0.1 * t, 0.15 * s(1.5), 0.06 * s(1.1)]
        e = [3 * s(1.7), 5 * s(1.3, 0.5), 8 * s(2.0)]
    else:
        p = [0.3 + 0.1 * s(1.9), -0.2 + 0.15 * t, 0.1]
        e = [3 * s(1.4), -5 * s(2.2), -10 * s(1.6, 1.0)]
    T = np.eye(4)
    T[:3, :3] = Rotation.from_euler("xyz", e, degrees=True).as_matrix()
    T[:3, 3] = p
    return T


def body_to_camera(R_bo=R_BO, t_bc=T_BC) -> np.ndarray:
    B = np.eye(4)
    B[:3, :3] = np.asarray(R_bo, float)
    B[:3, 3] = t_bc
    return B


def camera_pose(hand: str, i: int) -> np.ndarray:
    return body_pose(hand, i) @ body_to_camera()


def opening(hand: str, i: int) -> float:
    return round(0.05 + 0.03 * math.sin(i / 9.0 + (0.0 if hand == "robot0" else 1.0)), 6)


def stretched_K(wh=WH) -> np.ndarray:
    """The calibration on a ``wh`` picture (the video's by default)."""
    K = np.asarray(K_CAL, float).copy()
    K[0] *= wh[0] / CAL_WH[0]
    K[1] *= wh[1] / CAL_WH[1]
    return K


# ---------------------------------------------------------------- pictures

def _hash(a: np.ndarray, b: np.ndarray, c: int) -> np.ndarray:
    with np.errstate(over="ignore"):
        h = (a * np.int64(73856093)) ^ (b * np.int64(19349663)) ^ np.int64(c * 83492791)
        h = (h ^ (h >> 13)) * np.int64(1274126177)
    return ((h ^ (h >> 16)) & 0xFFFF) / 65535.0


def _texture(u: np.ndarray, v: np.ndarray, plane: int) -> np.ndarray:
    out = np.zeros(len(u))
    for k, (size, weight) in enumerate(((0.03, 0.45), (0.08, 0.35), (0.2, 0.2))):
        out += weight * _hash(np.floor(u / size).astype(np.int64), np.floor(v / size).astype(np.int64), plane * 7 + k)
    return out


def _rays(wh=WH) -> np.ndarray:
    """Every pixel's ray in the camera (x, y, 1), through the stretched equidistant camera, for a ``wh`` picture."""
    import cv2

    W, H = wh
    u, v = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
    pts = np.stack([u.ravel(), v.ravel()], -1).reshape(-1, 1, 2)
    n = cv2.fisheye.undistortPoints(pts, stretched_K(wh), np.asarray(D, float)).reshape(-1, 2)
    return np.c_[n, np.ones(len(n))]


def render(T_wc: np.ndarray, rays: np.ndarray, wh=WH) -> np.ndarray:
    """The room seen from camera pose ``T_wc`` (grey, ``wh`` as the rays were made for)."""
    d = rays @ T_wc[:3, :3].T
    o = T_wc[:3, 3]
    best = np.full(len(d), np.inf)
    val = np.zeros(len(d))
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / d
    for pid, (axis, c) in enumerate(PLANES):
        s = (c - o[axis]) * inv[:, axis]
        hit = np.isfinite(s) & (s > 0) & (s < best)
        p = o + s[hit, None] * d[hit]
        a, b = [x for x in range(3) if x != axis]
        val[hit] = _texture(p[:, a], p[:, b], pid)
        best[hit] = s[hit]
    for bid, (lo, hi) in enumerate(BOXES):
        with np.errstate(invalid="ignore"):
            t1, t2 = (np.asarray(lo) - o) * inv, (np.asarray(hi) - o) * inv
        near = np.nan_to_num(np.minimum(t1, t2), nan=-np.inf)
        far = np.nan_to_num(np.maximum(t1, t2), nan=np.inf)
        s, face = near.max(axis=1), near.argmax(axis=1)
        hit = (s > 0) & (s <= far.min(axis=1)) & (s < best)
        p = o + s[hit, None] * d[hit]
        for axis in range(3):
            on = face[hit] == axis
            a, b = [x for x in range(3) if x != axis]
            idx = np.flatnonzero(hit)[on]
            val[idx] = _texture(p[on, a], p[on, b], 20 + 3 * bid + axis)
        best[hit] = s[hit]
    return (40 + 190 * val).astype(np.uint8).reshape(wh[1], wh[0])


def _h264(frames: list[np.ndarray]) -> list[bytes]:
    import av

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = WH[0], WH[1], "yuv420p"
    enc.time_base = Fraction(1, FPS)
    enc.options = {"crf": "12", "bf": "0", "g": str(KEYINT),
                   "x264-params": f"keyint={KEYINT}:min-keyint={KEYINT}:scenecut=0"}
    out: list[bytes] = []
    for i, g in enumerate(frames):
        f = av.VideoFrame.from_ndarray(g, format="gray").reformat(format="yuv420p")
        f.pts = i
        out += [bytes(p) for p in enc.encode(f)]
    out += [bytes(p) for p in enc.encode(None)]
    assert len(out) == len(frames)
    return out


# ---------------------------------------------------------------- messages

def _types():
    """DAS-like protobuf messages: the recorder's ``header`` (``timestamp`` in ns) on each."""
    from google.protobuf import descriptor_pb2 as D_, descriptor_pool, message_factory

    F = D_.FieldDescriptorProto
    fdp = D_.FileDescriptorProto(name="das_test.proto", package="foxglove", syntax="proto3")

    def msg(name, fields):
        m = fdp.message_type.add(name=name)
        for k, (fname, ftype, rep, tname) in enumerate(fields, 1):
            f = m.field.add(name=fname, number=k, type=ftype,
                            label=F.LABEL_REPEATED if rep else F.LABEL_OPTIONAL)
            if tname:
                f.type_name = f".foxglove.{tname}"

    dbl, u32, u64, s, b, m = F.TYPE_DOUBLE, F.TYPE_UINT32, F.TYPE_UINT64, F.TYPE_STRING, F.TYPE_BYTES, F.TYPE_MESSAGE
    msg("Header", [("module_name", s, False, None), ("sequence_num", u32, False, None), ("timestamp", u64, False, None)])
    msg("Vector3", [("x", dbl, False, None), ("y", dbl, False, None), ("z", dbl, False, None)])
    msg("Quaternion", [("x", dbl, False, None), ("y", dbl, False, None), ("z", dbl, False, None), ("w", dbl, False, None)])
    msg("Pose", [("position", m, False, "Vector3"), ("orientation", m, False, "Quaternion")])
    msg("PoseInFrame", [("frame_id", s, False, None), ("pose", m, False, "Pose"), ("header", m, False, "Header")])
    msg("CompressedImage", [("frame_id", s, False, None), ("data", b, False, None), ("format", s, False, None),
                            ("header", m, False, "Header")])
    msg("MagneticEncoderMeasurement", [("header", m, False, "Header"), ("frame_id", s, False, None),
                                       ("value", dbl, False, None)])
    msg("CameraCalibration", [("frame_id", s, False, None), ("width", u32, False, None), ("height", u32, False, None),
                              ("distortion_model", s, False, None), ("D", dbl, True, None), ("K", dbl, True, None),
                              ("T_b_c", dbl, True, None), ("header", m, False, "Header")])
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    get = lambda n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"foxglove.{n}"))  # noqa: E731
    return {n: get(n) for n in ("PoseInFrame", "CompressedImage", "MagneticEncoderMeasurement", "CameraCalibration")}


def frame_ns(i: int) -> int:
    return L0 + i * STEP


def make_das(root, *, name: str = "episode_0.mcap", camera_info1: bool = False) -> str:
    """Write the recording (see the module doc); returns the file's path."""
    from mcap_protobuf.writer import Writer
    from scipy.spatial.transform import Rotation

    T = _types()
    os.makedirs(root, exist_ok=True)
    rays = _rays()
    room = _h264([render(camera_pose("robot0", i), rays) for i in range(N)])[PRE:]
    plain = _h264([np.full((WH[1], WH[0]), 90 + (i % 2), np.uint8) for i in range(N)])
    msgs: list[tuple[int, str, object]] = []

    def head(m, ns):
        m.header.timestamp = ns
        return m

    for hand in ("robot0", "robot1"):
        own = 0 if hand == "robot0" else CLOCK1                 # header = log - own
        if hand == "robot0" or camera_info1:
            cal = T["CameraCalibration"](frame_id="das_center_optical_frame", width=CAL_WH[0], height=CAL_WH[1],
                                         distortion_model="equidistant")
            cal.D.extend(D)
            cal.K.extend(np.asarray(K_CAL, float).ravel().tolist())
            cal.T_b_c.extend(T_BC + [1.0, 0.0, 0.0, 0.0])
            msgs.append((frame_ns(0), f"/{hand}/sensor/camera0/camera_info", head(cal, frame_ns(0) - own)))
        pictures = room if hand == "robot0" else plain
        first = PRE if hand == "robot0" else 0
        for k, data in enumerate(pictures):
            i = first + k
            ns = frame_ns(i) + LAT["cam0" if hand == "robot0" else "cam1"]
            img = T["CompressedImage"](frame_id="das_center_optical_frame", data=data, format="h264")
            msgs.append((ns, f"/{hand}/sensor/camera0/compressed", head(img, frame_ns(i) - own)))
        for i in range(N):
            if hand == "robot0" and (i < POSE_FROM or i in GAP0):
                continue
            if hand == "robot1" and i in GAP1:
                continue
            Tb = body_pose(hand, i)
            q = Rotation.from_matrix(Tb[:3, :3]).as_quat()
            p = T["PoseInFrame"](frame_id="world")
            p.pose.position.x, p.pose.position.y, p.pose.position.z = map(float, Tb[:3, 3])
            p.pose.orientation.x, p.pose.orientation.y, p.pose.orientation.z, p.pose.orientation.w = map(float, q)
            lat = LAT["pose0" if hand == "robot0" else "pose1"]
            msgs.append((frame_ns(i) + lat, f"/{hand}/vio/eef_pose", head(p, frame_ns(i) + 1_000_000 - own)))
        for i in range(POSE_FROM if hand == "robot0" else 0, N):
            e = T["MagneticEncoderMeasurement"](frame_id="encoder", value=opening(hand, i))
            msgs.append((frame_ns(i) + LAT["open0"], f"/{hand}/sensor/magnetic_encoder", head(e, frame_ns(i) - 2_000_000 - own)))
    path = os.path.join(str(root), name)
    with open(path, "wb") as fh:
        w = Writer(fh, chunk_size=1 << 20)
        w._writer.add_metadata("episode", {"task_name": "put the cup on the shelf"})
        for ns, topic, m in sorted(msgs, key=lambda x: x[0]):
            w.write_message(topic, m, log_time=ns, publish_time=ns)
        w.finish()
    return path


def calibration(**changes) -> dict:
    """The gripper's ``umi-calibration/2`` for this recording; ``changes`` replace top-level fields."""
    fallback = {"model": "opencv_fisheye", "K": K_CAL, "D": D, "image_size_wh": list(CAL_WH), "t_bc": T_BC}
    cfg = {"schema_version": "umi-calibration/2", "gripper": "das_test", "pose_frame": "vio_body_flu",
           "body_to_optical": R_BO, "T_camera_tcp": T_CT, "finger_axis": "camera_x",
           "opening": {"unit": "m", "scale": 1.0}, "intrinsics_fallback": {"robot1": fallback},
           "intrinsics_scaling": "stretch_to_video", "pairing_tolerance_s": 0.02,
           "provenance": {"source": "test", "method": "synthetic", "assurance": "declared",
                          "items": {"T_camera_tcp": {"assurance": "model_assumed", "method": "a guess"}}}}
    out = copy.deepcopy(cfg)
    out.update(copy.deepcopy(changes))
    return out
