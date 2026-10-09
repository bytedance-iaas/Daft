"""Handheld-gripper mcap (DAS / GenRobot) -> EEF trajectory bundle (design doc 22 §5.2, F5.18).

One ``.mcap`` per episode, numbered as the platform numbers them (``episode_<N>.mcap`` by name, else the
sorted files). Each hand ``robotN`` has a VIO pose stream (the body: x forward, y left, z up, at the IMU),
a gripper opening (magnetic encoder), a wrist fisheye camera (H.264 or JPEG) and, usually, its calibration
(``camera_info``: the equidistant model at 640 x 480 and the body->camera offset ``T_b_c``). What the
recordings do not say comes from the gripper's calibration file (``umi-calibration/2``, one per gripper
model): what the pose is, the body->optical rotation, the camera->TCP transform, how the opening reads, how
the intrinsics fit the video, the pairing tolerance - each with its assurance.

Per hand: ``T_world_cam = T_world_body · T(t_bc) · R(body_to_optical)`` and
``T_world_tcp = T_world_cam · T_camera_tcp``. The calibration stays as recorded and
``H_media_from_calibration`` stretches it onto the video. Each hand stays in its own VIO world
(``umi.world_frames: per_hand``, eef-video 1.1.0): only its own camera draws it.

The timeline is the first hand's camera frames from its first keyframe and from the checks' clock anchor on
(the first message of the anchor topic, ``/robot0/vio/eef_pose`` under the built-in UMI mapping);
``timestamp_s`` is the frame's log time from the anchor, so a finding lands where the player shows the
picture. Another camera takes its frame nearest in log time. A hand's pose and opening pair with its own
camera frame by header time (the device's clock) within the pairing tolerance; further is missing (null).
The bundle is never interpolated: readers bridge short gaps themselves (``umi.fill_gaps``).

Next to ``trajectory.json`` goes ``umi-export-report.json``: per camera the pairing rate, the missing and
the slow stretches, where the intrinsics came from, and the platform's checks of design doc 22 §7
(``calibration_suspect``, ``timing_suspect``), the camera's own motion against the poses among them.

The platform does the same itself when a task gives no trajectory.json (design doc 22 §5.4, F5.20): one episode
at a time (:func:`episode_bundle`), the file read in place or streamed from TOS, the gripper's calibration the
task's or the built-in DAS DEMO one (:data:`BUILTIN_CALIBRATION`: the assumptions of §7, no camera's intrinsics).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass, field

import numpy as np

from .. import contracts as C, geometry as G
from .lerobot_mapping import _points

CAL_VERSION = "umi-calibration/2"
#: the gripper calibration a task without one uses (design doc 22 §5.4): DAS, every value assumed
BUILTIN_CALIBRATION = pathlib.Path(__file__).resolve().parents[1] / "calibrations" / "das_gripper_demo.json"
REPORT_VERSION = "umi-export-report/1"
GENERATOR = "eef export-umi-mcap"
TOPICS = {"pose": "/{hand}/vio/eef_pose", "opening": "/{hand}/sensor/magnetic_encoder",
          "image": "/{hand}/sensor/camera0/compressed", "camera_info": "/{hand}/sensor/camera0/camera_info"}
#: the checks' clock starts at the first message of the built-in UMI mapping's first action source
ANCHOR_TOPIC = "/robot0/vio/eef_pose"
HAND_RE = re.compile(r"^/(robot\d+)/")
MODELS = {"equidistant": G.FISHEYE, "kannala_brandt": G.FISHEYE, "fisheye": G.FISHEYE,
          "plumb_bob": G.BROWN, "rational_polynomial": G.BROWN}
#: design doc 22 §7: rows 3, 7, 8 and 2
FXFY_TOLERANCE = 0.02
PAIRING_MIN = 0.9
OPENING_MAX_M = 0.2
EGO_ROTATION_DEG = 3.0
#: the rotation difference over the rotation the poses make: about 0.25-0.7 with the right convention on the
#: DAS recordings, 1.0-1.8 with a wrong axis assignment (0.5 s pairs turn only a few degrees)
EGO_RELATIVE = 1.0
EGO_PAIRS = 10
EGO_STEP_S = 0.5
#: a pose stream slower than this many camera intervals between two poses is a slow stretch
SLOW_STEPS = 1.5


class ExportError(ValueError):
    pass


# ---------------------------------------------------------------- the calibration file

def check_calibration(cfg, where: str = "calibration") -> dict:
    """A ``umi-calibration/2`` document checked against its Schema and for rigid, proper transforms (``where``
    names it in the errors)."""
    from ....contracts import schemas

    problems = schemas.errors("eef/umi_calibration.schema.json", cfg)
    if problems:
        raise ExportError(f"{where}: not a {CAL_VERSION} file: {problems[0]}")
    R = np.asarray(cfg["body_to_optical"], float)
    if not (np.allclose(R @ R.T, np.eye(3), atol=1e-6) and np.linalg.det(R) > 0.999):
        raise ExportError(f"{where}: body_to_optical is not a proper rotation")
    if not G.is_rigid(np.asarray(cfg["T_camera_tcp"], float)):
        raise ExportError(f"{where}: T_camera_tcp is not a rigid transform")
    for hand, intr in (cfg.get("intrinsics_fallback") or {}).items():
        K = np.asarray(intr["K"], float)
        if K[0, 0] <= 0 or K[1, 1] <= 0 or not np.allclose(K[2], [0, 0, 1]):
            raise ExportError(f"{where}: intrinsics_fallback.{hand}: invalid K")
    return cfg


def read_calibration(path) -> dict:
    """The ``umi-calibration/2`` file, checked (:func:`check_calibration`)."""
    try:
        cfg = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ExportError(f"{path}: not JSON: {exc}") from None
    return check_calibration(cfg, str(path))


def assurance(cfg: dict, item: str) -> str:
    """A field's assurance: its own in ``provenance.items``, else the file's."""
    own = ((cfg["provenance"].get("items") or {}).get(item) or {}).get("assurance")
    return own or cfg["provenance"]["assurance"]


def assumed(cfg: dict) -> list[str]:
    """The fields the results rest on that are assumptions (design doc 22 §7 rows 1, 2): 「按假设值」."""
    return [k for k in ("T_camera_tcp", "body_to_optical", "pose_frame") if assurance(cfg, k) != "declared"
            and assurance(cfg, k) != "independently_calibrated"]


# ---------------------------------------------------------------- reading a recording

def _get(node, dotted: str):
    from ....viz import mcap_messages as M

    for part in dotted.split("."):
        if node is None:
            return None
        node = M._get(node, part)
    return node


def _header_ns(decoded, log_ns: int) -> int:
    """The message's own time (``header.timestamp``, else ``timestamp``), else its log time."""
    from ....viz import mcap_messages as M

    for path in ("header.timestamp", "header.stamp", "timestamp"):
        ns = M._to_ns(_get(decoded, path))
        if ns:
            return ns
    return int(log_ns)


@dataclass
class Camera:
    topic: str
    codec: str | None = None
    log: list[int] = field(default_factory=list)
    head: list[int] = field(default_factory=list)
    first_key: int | None = None
    size: tuple[int, int] | None = None
    sets: dict = field(default_factory=dict)
    sizer: object = None


@dataclass
class Series:
    topic: str
    log: list[int] = field(default_factory=list)
    head: list[int] = field(default_factory=list)
    values: list = field(default_factory=list)


@dataclass
class Recording:
    path: str
    hands: list[str]
    cameras: dict[str, Camera]
    poses: dict[str, Series]
    openings: dict[str, Series]
    infos: dict[str, dict]
    anchor_log: int | None
    task: str | None


def _open(path: str):
    """The episode file: a local path, or the object's URI in a streamed TOS dataset (ranged reads)."""
    from ..mcap_media import open_episode

    return open_episode(path)


def _topics_of(path: str) -> set[str]:
    from mcap.reader import make_reader

    with _open(path) as fh:
        summary = make_reader(fh).get_summary()
    return {c.topic for c in summary.channels.values()} if summary else set()


def scan(path: str, cfg: dict, anchor_topic: str = ANCHOR_TOPIC) -> Recording:
    """One pass over the file: every hand's camera messages (times, keyframes, picture size), poses,
    openings and camera_info, and the anchor topic's first message."""
    from mcap.reader import make_reader

    from ....viz import annexb as AB
    from ....viz import mcap_messages as M

    names = {**TOPICS, **(cfg.get("topics") or {})}
    topics = _topics_of(path)
    hands = sorted({m.group(1) for t in topics if (m := HAND_RE.match(t))
                    and names["pose"].format(hand=m.group(1)) in topics and names["image"].format(hand=m.group(1)) in topics})
    if not hands:
        raise ExportError(f"{os.path.basename(path)}: no hand has both {names['pose']} and {names['image']}")
    want: dict[str, tuple[str, str]] = {}
    for h in hands:
        for kind in ("pose", "opening", "image", "camera_info"):
            want[names[kind].format(hand=h)] = (kind, h)
    cameras = {h: Camera(names["image"].format(hand=h)) for h in hands}
    poses = {h: Series(names["pose"].format(hand=h)) for h in hands}
    openings = {h: Series(names["opening"].format(hand=h)) for h in hands}
    infos: dict[str, dict] = {}
    anchor_log = None
    dec = M.Decoder()
    with _open(path) as fh:
        reader = make_reader(fh)
        for schema, channel, message in reader.iter_messages(topics=sorted(set(want) | {anchor_topic}), log_time_order=True):
            if channel.topic == anchor_topic and anchor_log is None:
                anchor_log = int(message.log_time)
            if channel.topic not in want:
                continue
            kind, h = want[channel.topic]
            decoded = dec.decode(channel, schema, message)
            if decoded is None:
                continue
            head = _header_ns(decoded, message.log_time)
            if kind == "image":
                frame = M.as_frame(decoded)
                if frame is None:
                    continue
                cam = cameras[h]
                fmt, data = frame
                cam.codec = cam.codec or M.frame_codec(fmt, data)
                k = len(cam.log)
                cam.log.append(int(message.log_time))
                cam.head.append(head)
                if cam.codec in ("h264", "h265"):
                    cam.sets.update(AB.parameter_sets(cam.codec, data))
                    if cam.first_key is None and AB.is_keyframe(cam.codec, data) and AB.has_all(cam.codec, cam.sets):
                        cam.first_key = k
                    if cam.size is None:
                        cam.sizer = cam.sizer or M.VideoSizer(cam.codec)
                        cam.size = cam.sizer.feed(data)
                elif cam.codec in ("jpeg", "png"):
                    if cam.first_key is None:
                        cam.first_key = k
                    if cam.size is None:
                        w, hh = M.picture_size(cam.codec, data)
                        cam.size = (w, hh) if w and hh else None
            elif kind == "pose":
                p, q = _get(decoded, "pose.position"), _get(decoded, "pose.orientation")
                xyz = [_get(p, a) for a in "xyz"] if p is not None else [None] * 3
                quat = [_get(q, a) for a in "xyzw"] if q is not None else [None] * 4
                if any(v is None for v in xyz + quat):
                    continue
                poses[h].log.append(int(message.log_time))
                poses[h].head.append(head)
                poses[h].values.append([float(v) for v in xyz + quat])
            elif kind == "opening":
                v = _get(decoded, "value")
                if v is None:
                    continue
                openings[h].log.append(int(message.log_time))
                openings[h].head.append(head)
                openings[h].values.append(float(v))
            elif kind == "camera_info" and h not in infos:
                infos[h] = _info(decoded)
        task = None
        try:
            for rec in reader.iter_metadata():
                for key in ("task_name", "task", "instruction"):
                    if (rec.metadata or {}).get(key):
                        task = str(rec.metadata[key]).strip()
                        break
                if task:
                    break
        except Exception:  # noqa: BLE001 - no metadata records
            task = None
    for cam in cameras.values():
        cam.sizer = None
    return Recording(path, hands, cameras, poses, openings, infos, anchor_log, task)


def _info(decoded) -> dict | None:
    """``camera_info`` as an intrinsics entry (``model``, ``K``, ``D``, ``image_size_wh``, ``t_bc``)."""
    K = _get(decoded, "K")
    w, h = _get(decoded, "width"), _get(decoded, "height")
    if K is None or len(list(K)) != 9 or not w or not h:
        return None
    model = MODELS.get(str(_get(decoded, "distortion_model") or "").lower(), G.PINHOLE)
    D = [float(x) for x in (_get(decoded, "D") or [])]
    if model == G.FISHEYE:
        D = (D + [0.0] * 4)[:4]
    elif model == G.BROWN:
        D = (D + [0.0] * 5)[:5]
    tbc = _get(decoded, "T_b_c")
    tbc = [float(x) for x in list(tbc)[:3]] if tbc is not None and len(list(tbc)) >= 3 else None
    return {"model": model, "K": np.asarray(list(K), float).reshape(3, 3).tolist(), "D": D,
            "image_size_wh": [int(w), int(h)], "t_bc": tbc}


# ---------------------------------------------------------------- pairing

def _nearest(times: np.ndarray, at: np.ndarray, tol_ns: float) -> np.ndarray:
    """Per ``at``, the index of the nearest of ``times`` (sorted) within ``tol_ns``, else -1."""
    if not len(times):
        return np.full(len(at), -1)
    i = np.clip(np.searchsorted(times, at), 1, max(1, len(times) - 1)) if len(times) > 1 else np.zeros(len(at), int)
    if len(times) > 1:
        left = i - 1
        i = np.where(np.abs(times[left] - at) <= np.abs(times[i] - at), left, i)
    return np.where(np.abs(times[i] - at) <= tol_ns, i, -1)


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """(first, last) of every run of True."""
    out, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        if not f and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _pose_matrix(v) -> np.ndarray | None:
    q = np.asarray(v[3:7], float)
    n = np.linalg.norm(q)
    if not np.isfinite(n) or n < 1e-6:
        return None
    return G.pose_matrix(np.asarray(v[:3], float), q / n)


# ---------------------------------------------------------------- one episode

@dataclass
class Episode:
    entry: dict | None
    report: dict


def export_episode(path: str, ep: int, cfg: dict, *, uri: str, dataset_id: str, horizon_s: float = 1.0,
                   anchor_topic: str = ANCHOR_TOPIC, ego_check: bool = True) -> Episode:
    from scipy.spatial.transform import Rotation

    rec = scan(path, cfg, anchor_topic)
    tol = float(cfg["pairing_tolerance_s"]) * 1e9
    R_bo = np.asarray(cfg["body_to_optical"], float)
    T_ct = np.asarray(cfg["T_camera_tcp"], float)
    scale = float(cfg["opening"]["scale"]) * (1e-3 if cfg["opening"]["unit"] == "mm" else 1.0)
    report: dict = {"episode_index": int(ep), "file": uri, "cameras": {}, "hands": {}, "suspects": []}
    # the cameras that can be drawn: intrinsics from camera_info, else the calibration's fallback
    usable: dict[str, dict] = {}
    for h in rec.hands:
        cam = rec.cameras[h]
        intr, source = rec.infos.get(h), "camera_info"
        if intr is None and (cfg.get("intrinsics_fallback") or {}).get(h):
            intr, source = dict(cfg["intrinsics_fallback"][h]), "intrinsics_fallback"
            intr.setdefault("t_bc", None)
        info = {"hand": h, "topic": cam.topic, "messages": len(cam.log), "first_decodable": cam.first_key,
                "video_wh": list(cam.size) if cam.size else None, "intrinsics": source if intr else None,
                "status": "ok", "reason": None}
        report["cameras"][h] = info
        if intr is None:
            info.update(status="unsupported", reason="这一路没有 camera_info，标定文件里也没有它的 intrinsics_fallback")
        elif cam.codec not in ("h264", "jpeg", "png") or not cam.size or cam.first_key is None:
            info.update(status="unsupported", reason=f"画面读不出来（编码 {cam.codec}、尺寸 {cam.size}）")
        else:
            usable[h] = intr
    if not usable:
        report["status"] = "unsupported"
        return Episode(None, report)
    hands = sorted(usable)
    primary = hands[0]
    pcam = rec.cameras[primary]
    plog = np.asarray(pcam.log, np.int64)
    anchor = rec.anchor_log if rec.anchor_log is not None else int(plog[pcam.first_key])
    rows = [k for k in range(pcam.first_key, len(plog)) if plog[k] >= anchor]
    rows = [k for i, k in enumerate(rows) if i == 0 or plog[k] > plog[rows[i - 1]]]
    report.update(anchor_topic=anchor_topic if rec.anchor_log is not None else pcam.topic,
                  dropped_before_anchor=int(sum(1 for k in range(pcam.first_key, len(plog)) if plog[k] < anchor)),
                  rows=len(rows))
    if len(rows) < 2:
        report["status"] = "unsupported"
        report["reason"] = "锚点之后不到两帧画面"
        return Episode(None, report)
    row_log = plog[rows]
    n = len(rows)
    t = np.round((row_log - anchor) / 1e9, 6)
    frames_of: dict[str, np.ndarray] = {primary: np.asarray(rows)}
    for h in hands[1:]:
        cam = rec.cameras[h]
        lg = np.asarray(cam.log, np.int64)
        step = float(np.median(np.diff(lg))) if len(lg) > 1 else 1e9 / 30
        k = _nearest(lg, row_log, step)
        frames_of[h] = np.where(k >= cam.first_key, k, -1)
    cams_doc, frames_doc = {}, [None] * n
    views, calibrations, umi_ranges = [], {}, {}
    T_cam: dict[str, list] = {}
    T_tcp: dict[str, list] = {}
    opening: dict[str, list] = {}
    for h in hands:
        cam, intr = rec.cameras[h], usable[h]
        lg, hd = np.asarray(cam.log, np.int64), np.asarray(cam.head, np.int64)
        k = frames_of[h]
        # the hand's own clock at each row: its camera's header time, shifted by how far the row is in log time
        j = np.clip(np.searchsorted(lg, row_log), 0, len(lg) - 1)
        ref = hd[j] + (row_log - lg[j])
        ref = np.where(k >= 0, hd[np.maximum(k, 0)], ref)
        ps = rec.poses[h]
        order = np.argsort(np.asarray(ps.head, np.int64)) if ps.head else np.array([], int)
        pose_head = np.asarray(ps.head, np.int64)[order] if ps.head else np.array([], np.int64)
        pi = _nearest(pose_head, ref, tol)
        os_ = rec.openings[h]
        oord = np.argsort(np.asarray(os_.head, np.int64)) if os_.head else np.array([], int)
        open_head = np.asarray(os_.head, np.int64)[oord] if os_.head else np.array([], np.int64)
        oi = _nearest(open_head, ref, tol)
        t_bc = np.asarray(intr.get("t_bc") or [0.0, 0.0, 0.0], float)
        B = np.eye(4)
        B[:3, :3] = R_bo
        B[:3, 3] = t_bc
        tcams, ttcps, opens = [], [], []
        for r in range(n):
            T = _pose_matrix(ps.values[order[pi[r]]]) if pi[r] >= 0 else None
            if T is None:
                tcams.append(None)
                ttcps.append(None)
            else:
                if cfg["pose_frame"] == "vio_body_flu":
                    Tc = T @ B
                elif cfg["pose_frame"] == "camera_optical":
                    Tc = T
                else:                                    # the pose is the tool centre already
                    Tc = T @ np.linalg.inv(T_ct)
                tcams.append(Tc)
                ttcps.append(Tc @ T_ct)
            v = os_.values[oord[oi[r]]] * scale if oi[r] >= 0 else None
            opens.append(v if v is not None and v >= 0 else None)
        T_cam[h], T_tcp[h], opening[h] = tcams, ttcps, opens
        # what the report says about this camera and hand
        drawn = k >= 0
        paired = np.array([tcams[r] is not None for r in range(n)])
        info = report["cameras"][h]
        rate = float(paired[drawn].mean()) if drawn.any() else 0.0
        missing = [[int(a), int(b), float(t[a]), float(t[b])] for a, b in _runs(drawn & ~paired)]
        pose_head_all = np.sort(np.asarray(ps.head, np.int64))
        gaps = np.diff(pose_head_all) / 1e9 if len(pose_head_all) > 1 else np.array([])
        cam_step = float(np.median(np.diff(lg))) / 1e9 if len(lg) > 1 else 1 / 30
        slow = _runs(gaps > SLOW_STEPS * cam_step)
        # the hand's own clock onto the episode's (log time from the anchor), by its camera's messages
        shift = int(np.median(lg - hd))
        at = lambda ns: round((int(ns) + shift - anchor) / 1e9, 3)  # noqa: E731
        info.update(rows_with_frame=int(drawn.sum()), paired=int((drawn & paired).sum()), pairing_rate=round(rate, 4),
                    missing_segments=missing,
                    slow_pose_segments=[[at(pose_head_all[a]), at(pose_head_all[b + 1])] for a, b in slow],
                    pose_rate_hz=round(len(ps.head) / max(1e-9, (pose_head_all[-1] - pose_head_all[0]) / 1e9), 2)
                    if len(ps.head) > 1 else None)
        raw = [os_.values[oord[oi[r]]] * scale for r in range(n) if oi[r] >= 0]
        report["hands"][h] = {"opening_m": [round(min(raw), 4), round(max(raw), 4)] if raw else None,
                              "negative_openings": int(sum(1 for v in raw if v < 0))}
        # the media and the calibration
        W, H = cam.size
        cw, ch = intr["image_size_wh"]
        Hm = np.diag([W / cw, H / ch, 1.0]) if cfg["intrinsics_scaling"] == "stretch_to_video" else np.eye(3)
        K = np.asarray(intr["K"], float)
        fxfy = float(K[0, 0] * Hm[0, 0] / (K[1, 1] * Hm[1, 1]))
        info["fx_fy"] = round(fxfy, 4)
        if abs(fxfy - 1) > FXFY_TOLERANCE:
            report["suspects"].append({"code": "calibration_suspect", "row": 3, "camera": h,
                                       "detail": f"内参换算到画面后 fx / fy = {fxfy:.3f}，偏离 1 超过 {FXFY_TOLERANCE:.0%}"})
        if drawn.any() and rate < PAIRING_MIN:
            report["suspects"].append({"code": "timing_suspect", "row": 7, "camera": h,
                                       "detail": f"有画面的帧里只有 {rate:.0%} 配上了位姿（容差 {cfg['pairing_tolerance_s'] * 1e3:.0f} ms）"})
        if raw and (min(raw) < 0 or max(raw) > OPENING_MAX_M):
            report["suspects"].append({"code": "calibration_suspect", "row": 8, "hand": h,
                                       "detail": f"开口 {min(raw):.3f}–{max(raw):.3f} m，超出 0–{OPENING_MAX_M} m"})
        cid = f"{h}_camera0"
        fps = round(1.0 / cam_step, 3) if cam_step > 0 else 30.0
        views.append({"view_id": cid, "kind": "camera", "camera_id": cid, "mount": "wrist",
                      "media": {"kind": "video", "uri": uri, "topic": cam.topic, "image_size_wh": [int(W), int(H)],
                                "frame_count": len(lg), "fps": fps, "clip_start_s": 0.0, "clip_end_s": None}})
        calibrations[cid] = {"camera_id": cid, "reference_frame": f"{h}_vio_world", "image_size_wh": [int(cw), int(ch)],
                             "image_space": "distorted" if intr["model"] != G.PINHOLE else "rectified",
                             "model": intr["model"], "K": K.tolist(), "distortion_coefficients": list(map(float, intr["D"])),
                             "extrinsics_mode": "per_frame", "T_reference_camera": None,
                             "provenance": {"source": f"{cam.topic.rsplit('/', 1)[0]}/camera_info" if info["intrinsics"] == "camera_info"
                                            else "umi-calibration intrinsics_fallback",
                                            "method": "copy; stretched onto the video by H_media_from_calibration"
                                            if cfg["intrinsics_scaling"] == "stretch_to_video" else "copy",
                                            "assurance": "declared" if info["intrinsics"] == "camera_info"
                                            else assurance(cfg, "intrinsics_fallback")}}
        cams_doc[h] = (cid, Hm, lg)
        if (cfg.get("gripper_range") or {}).get(h):
            umi_ranges[h] = cfg["gripper_range"][h]
    # the camera's own motion against the poses on a few pairs (design doc 22 §7 row 2)
    if ego_check:
        report["ego_motion"] = {}
        for h in hands:
            res = _ego_check(rec, h, usable[h], frames_of[h], T_cam[h], t, cams_doc[h][1])
            report["ego_motion"][h] = res
            if res.get("rotation_median_deg") is not None and (res["rotation_median_deg"] > EGO_ROTATION_DEG
                                                                or res["relative_median"] > EGO_RELATIVE):
                report["suspects"].append({"code": "calibration_suspect", "row": 2, "camera": h,
                                           "detail": f"画面估计的相机转动与位姿推出的相差中位 {res['rotation_median_deg']:.1f}°，"
                                                     f"是位姿本身转动的 {res['relative_median']:.0%}（界限 {EGO_ROTATION_DEG:.0f}°、"
                                                     f"{EGO_RELATIVE:.0%}）：机体→光学的旋转或位姿的定义可能不对"})
    # the bundle entry
    sid = f"umi_mcap_{ep:06d}"
    prov = cfg["provenance"]
    tcp_ass = assurance(cfg, "T_camera_tcp")
    pose_prov = {"source": "VIO body pose (mcap)", "method": "T_world_body · T(t_bc) · R(body_to_optical) · T_camera_tcp",
                 "assurance": "declared" if tcp_ass == "declared" and assurance(cfg, "body_to_optical") == "declared" else "model_assumed"}
    points, axes = _points({"tcp_offset_m": [0, 0, 0], "axes": {"from": "tcp", "length_m": 0.06},
                            "assurance": tcp_ass}, "tcp", f"{cfg['gripper']} T_camera_tcp")
    umi = {"camera_hands": {cams_doc[h][0]: h for h in hands}, "horizon_s": float(horizon_s),
           "provenance": {"source": prov["source"], "method": prov["method"], "assurance": prov["assurance"]},
           "world_frames": "per_hand"}
    if umi_ranges:
        umi["gripper_range"] = umi_ranges
    notes = [f"{cfg['gripper']} handheld-gripper mcap; each hand in its own VIO world (per_hand), drawn only by its own wrist camera.",
             "Poses are paired with camera frames by header time; frames without a pose within the tolerance are null, never interpolated."]
    if assumed(cfg):
        notes.append("按假设值：" + "、".join(assumed(cfg)) + " 是假设，不是夹爪厂商的声明（设计 22 §7）。")
    sample = {"schema_version": C.SCHEMA_VERSION_1_1, "sample_id": sid,
              "source": {"dataset": dataset_id, "episode_id": str(ep), "instruction": rec.task or cfg.get("instruction")},
              "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": "#calibration",
              "eef_frame": "tcp", "reference_frame": "per_hand", "views": views, "point_definitions": points,
              "axis_definitions": axes, "raw_pose_sequence": None, "notes": notes, "umi": umi}
    first_log = {h: cams_doc[h][2][0] for h in hands}
    for r in range(n):
        hand_rows, cam_rows = {}, {}
        for h in hands:
            Tt = T_tcp[h][r]
            if Tt is None:
                hand_rows[h] = None
            else:
                hand_rows[h] = {"pose": {"pose_type": "absolute", "frame_id": h, "reference_frame": f"{h}_vio_world",
                                         "position_m": [round(float(x), 6) for x in Tt[:3, 3]],
                                         "quaternion_xyzw": [float(x) for x in Rotation.from_matrix(Tt[:3, :3]).as_quat()],
                                         "relative_to": None, "provenance": pose_prov},
                                "opening_m": opening[h][r]}
            cid, Hm, lg = cams_doc[h]
            k = int(frames_of[h][r])
            Tc = T_cam[h][r]
            cam_rows[cid] = {"video_frame_index": k if k >= 0 else None,
                             "video_timestamp_s": round((lg[k] - first_log[h]) / 1e9, 6) if k >= 0 else None,
                             "image_size_wh": list(rec.cameras[h].size), "calibration_id": cid if Tc is not None else None,
                             "T_reference_camera": Tc.tolist() if Tc is not None else None,
                             "H_media_from_calibration": Hm.tolist(), "projection": None}
        frames_doc[r] = {"schema_version": C.SCHEMA_VERSION_1_1, "sample_id": sid, "frame_index": r,
                         "timestamp_s": float(t[r]), "source_state_index": None, "source_timing": [], "eef": None,
                         "gripper": None, "hands": hand_rows, "cameras": cam_rows}
    entry = {"episode_index": int(ep), "sample": sample,
             "calibration": {"schema_version": C.SCHEMA_VERSION_1_1, "calibrations": calibrations}, "frames": frames_doc}
    report["status"] = "ok"
    report["duration_s"] = float(t[-1])
    return Episode(entry, report)


def _ego_check(rec: Recording, h: str, intr: dict, frames: np.ndarray, T_cam: list, t: np.ndarray, Hm: np.ndarray) -> dict:
    """A few pairs EGO_STEP_S apart, spread over the episode: the camera's motion in its pictures against
    its recorded poses (design doc 22 §7 row 2). Medians of the rotation and travel-direction differences."""
    from .. import egomotion as EM

    from scipy.spatial.transform import Rotation

    cam = rec.cameras[h]
    step_s = float(np.median(np.diff(t))) if len(t) > 1 else 1 / 30
    gap = max(1, int(round(EGO_STEP_S / step_s)))
    ok = [r for r in range(len(t) - gap) if frames[r] >= 0 and frames[r + gap] >= 0
          and T_cam[r] is not None and T_cam[r + gap] is not None]
    if not ok:
        return {"pairs": 0, "rotation_median_deg": None, "direction_median_deg": None, "note": "没有两端都有画面和位姿的帧对"}
    # the pairs that turn the most (a wrong convention shows in proportion to the turn), at least a pair apart
    turn = {r: float(np.degrees(np.linalg.norm(Rotation.from_matrix(EM.pose_motion(T_cam[r], T_cam[r + gap])[0]).as_rotvec())))
            for r in ok}
    pick: list[int] = []
    for r in sorted(ok, key=lambda x: -turn[x]):
        if all(abs(r - q) >= 2 * gap for q in pick):
            pick.append(r)
        if len(pick) == EGO_PAIRS:
            break
    wanted = {int(frames[r]) for r in pick} | {int(frames[r + gap]) for r in pick}
    pictures = _decode(rec.path, cam, wanted)
    K = np.asarray(intr["K"], float).copy()
    K[0] *= Hm[0, 0]
    K[1] *= Hm[1, 1]
    rot, rel, turned, direction = [], [], [], []
    mask = None
    for r in pick:
        a, b = pictures.get(int(frames[r])), pictures.get(int(frames[r + gap]))
        if a is None or b is None:
            continue
        if mask is None:
            mask = EM.scene_mask(a.shape[1], a.shape[0])
        seen = EM.picture_motion(a, b, K, intr["D"], intr["model"], mask)
        if seen is None:
            continue
        r_deg, d_deg = EM.disagreement(seen, T_cam[r], T_cam[r + gap])
        rot.append(r_deg)
        turned.append(turn[r])
        rel.append(r_deg / max(turn[r], 1.0))
        if d_deg is not None:
            direction.append(d_deg)
    med = lambda v, d: round(float(np.median(v)), d) if v else None  # noqa: E731
    return {"pairs": len(rot), "asked": len(pick), "step_frames": gap,
            "rotation_median_deg": med(rot, 2), "rotation_max_deg": round(float(np.max(rot)), 2) if rot else None,
            "pose_rotation_median_deg": med(turned, 2), "relative_median": med(rel, 2),
            "direction_median_deg": med(direction, 1)}


def _decode(path: str, cam: Camera, wanted: set[int]) -> dict[int, np.ndarray]:
    """Grey pictures of the camera's messages ``wanted`` (message order): an H.264 stream is decoded from the
    start, one access unit per message; JPEG / PNG only where wanted."""
    import cv2
    from mcap.reader import make_reader

    from ....viz import mcap_messages as M

    out: dict[int, np.ndarray] = {}
    last = max(wanted) if wanted else -1
    ctx = None
    if cam.codec in ("h264", "h265"):
        import av

        ctx = av.CodecContext.create("hevc" if cam.codec == "h265" else "h264", "r")
        ctx.thread_type = "SLICE"                         # no frame threading: frame k comes out of message k
    dec = M.Decoder()
    with _open(path) as fh:
        for k, (schema, channel, message) in enumerate(make_reader(fh).iter_messages(topics=[cam.topic], log_time_order=True)):
            if k > last:
                break
            frame = M.as_frame(dec.decode(channel, schema, message))
            if frame is None:
                continue
            data = frame[1]
            if ctx is not None:
                try:
                    pics = ctx.decode(av.Packet(data))
                except Exception:  # noqa: BLE001 - before the first keyframe nothing decodes
                    pics = []
                if k in wanted and pics:
                    out[k] = pics[-1].to_ndarray(format="gray")
            elif k in wanted:
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    out[k] = img
    return out


# ---------------------------------------------------------------- a dataset

def _bundle(samples: list[dict], dataset_id: str, fps: float) -> dict:
    """The ``trajectory-bundle/1.0`` around exported samples."""
    return {"schema_version": C.SCHEMA_VERSION_1_1, "container": C.CONTAINER, "media_uri_base": "lerobot_root",
            "dataset": {"id": dataset_id, "lerobot_codebase_version": "mcap", "fps": float(fps),
                        "episode_count": max(s["episode_index"] for s in samples) + 1, "generator": GENERATOR},
            "samples": samples}


def episode_bundle(path: str, ep: int, cfg: dict, *, uri: str, dataset_id: str, horizon_s: float = 1.0,
                   anchor_topic: str = ANCHOR_TOPIC) -> tuple[dict | None, dict]:
    """One episode as a bundle of its own, for the platform deriving it (design doc 22 §5.4): (bundle or None
    when the episode cannot be exported, its report). The pairs the report checks the camera's motion on are
    left out: the opinion checks the whole episode (§5.3)."""
    res = export_episode(path, ep, cfg, uri=uri, dataset_id=dataset_id, horizon_s=horizon_s, anchor_topic=anchor_topic,
                         ego_check=False)
    if res.entry is None:
        return None, res.report
    fps = float(np.median([v["media"]["fps"] for v in res.entry["sample"]["views"]]))
    return _bundle([res.entry], dataset_id, fps), res.report


def export(mcap_root, calibration, out, *, episodes: list[int] | None = None, dataset_id: str | None = None,
           horizon_s: float = 1.0, anchor_topic: str = ANCHOR_TOPIC, ego_check: bool = True) -> dict:
    """Every episode of a directory of mcap files into ``out`` (trajectory.json) and the report beside it.
    The bundle is checked as the platform checks an upload before it is written."""
    from ....cli import containers
    from .. import load

    root = pathlib.Path(mcap_root)
    cfg = read_calibration(calibration)
    numbered = containers.mcap_episodes(sorted(p.name for p in root.iterdir() if p.suffix == ".mcap"))
    if not numbered:
        raise ExportError(f"{root}: no .mcap files")
    chosen = sorted(numbered) if episodes is None else [e for e in sorted(numbered) if e in set(episodes)]
    out = pathlib.Path(out)
    report = {"schema_version": REPORT_VERSION, "generator": GENERATOR, "mcap_root": str(root),
              "calibration": {"path": str(calibration), "gripper": cfg["gripper"],
                              "sha256": hashlib.sha256(pathlib.Path(calibration).read_bytes()).hexdigest(),
                              "assurance": {k: assurance(cfg, k) for k in ("T_camera_tcp", "body_to_optical", "pose_frame",
                                                                          "intrinsics_scaling", "opening")},
                              "assumed": assumed(cfg)},
              "episodes": []}
    samples, fps_all = [], []
    for ep in chosen:
        name = numbered[ep]
        res = export_episode(str(root / name), ep, cfg, uri=name, dataset_id=dataset_id or root.name,
                             horizon_s=horizon_s, anchor_topic=anchor_topic, ego_check=ego_check)
        report["episodes"].append(res.report)
        if res.entry is not None:
            samples.append(res.entry)
            fps_all += [v["media"]["fps"] for v in res.entry["sample"]["views"]]
    if not samples:
        raise ExportError("no episode could be exported: " + json.dumps(report["episodes"], ensure_ascii=False)[:400])
    bundle = _bundle(samples, dataset_id or root.name, float(np.median(fps_all)))
    data = json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()
    checked = load.load_bundle(data, lerobot_root=str(root))
    if not checked.ok:                                       # never write a file the platform would refuse
        raise ExportError(f"the bundle does not validate: {checked.errors[0].message}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    report["trajectory"] = {"path": str(out), "sha256": hashlib.sha256(data).hexdigest(), "samples": len(samples)}
    report["suspects"] = sum(len(e["suspects"]) for e in report["episodes"])
    (out.parent / "umi-export-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"trajectory": str(out), "report": str(out.parent / "umi-export-report.json"), "samples": len(samples),
            "episodes": [{k: e.get(k) for k in ("episode_index", "status", "rows")} for e in report["episodes"]],
            "suspects": report["suspects"], "assumed": report["calibration"]["assumed"]}
