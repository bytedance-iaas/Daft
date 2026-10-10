"""The EEF module's trajectory without the user giving one (design doc 24, 24-eef-trajectory-generated.md).

A user names a dataset and selects 「EEF–视频一致性」; the trajectory bundle the module reads
(``trajectory.json``, design doc 12) is found by the platform, never asked for. Where it comes from,
in order:

1. the ``trajectory_json`` parameter, when a caller still gives one (an upload in the console,
   a path on the command line) - kept for the tools and the old tasks;
2. generated from the dataset itself (:func:`generate`): a handheld-gripper LeRobot dataset (``robot_type``
   ``umi_*``) records every hand's absolute TCP pose and opening in ``observation.state``
   (``robotN_pos_x..z``, ``robotN_rot6d_0..5``, ``robotN_gripper_width``) and carries its cameras' calibration
   in ``meta/umi_calibration.json`` (intrinsics, distortion, ``T_camera_tcp``); a wrist camera's pose is the
   TCP pose through that fixed transform;
3. a ``trajectory.json`` at the dataset's root (what ``export-umi`` / ``export-umi-mcap`` write next to the
   data they produce, ``media_uri_base = lerobot_root``) when there is nothing to generate it from.

The file is copied into the run directory (``inputs/eef/trajectory.json``) when the module runs, so the
task keeps the exact bytes it judged, and the report's overlay (``daemon/viz/eef_overlay``) reads that copy.
A dataset with neither has no poses and camera calibration to project: the module is unsupported
for it, with that reason - nothing for the user to supply.
"""
from __future__ import annotations

import json
import os
import tempfile

import numpy as np

#: Where a dataset carries its bundle, relative to its root.
DATASET_KEY = "trajectory.json"
#: A handheld-gripper dataset's camera calibration.
UMI_CALIBRATION_KEY = "meta/umi_calibration.json"
HORIZON_S = 1.0
#: A raw UMI / TRUMI session (design doc 24 §6).
SESSION_PLAN = "dataset_plan.pkl"
#: The session files the trajectory is computed from (never the videos: only their headers are read).
SESSION_FILES = ("camera_trajectory.csv", "slam_stdout.txt", "tx_slam_tag.json", "gripper_range.json")
#: The copy a run judges, relative to its run directory.
RUN_COPY = os.path.join("inputs", "eef", "trajectory.json")


def dataset_key(listing) -> str | None:
    """The dataset's own bundle among ``listing`` (keys relative to the dataset), or None."""
    return DATASET_KEY if DATASET_KEY in listing else None


def source_of(listing) -> str | None:
    """How the trajectory is had: ``session`` (a raw UMI session read in place), ``generate``, ``dataset_file``
    or None (the module cannot run)."""
    if SESSION_PLAN in listing and any(k.startswith("demos/") for k in listing):
        return "session"
    if UMI_CALIBRATION_KEY in listing and "meta/info.json" in listing:
        return "generate"
    return "dataset_file" if DATASET_KEY in listing else None


#: how a task's trajectory is had (C2 preflight ``trajectory_source.kind``, design doc 25 §4.2)
UPLOAD, SESSION, GENERATE, MCAP_DERIVE, DATASET_FILE = "upload", "session", "generate", "mcap_derive", "dataset_file"
MISSING_DECLARATION, MISSING_POSE = "missing_declaration", "missing_pose"
#: the declaration was drafted here, never confirmed: what it would generate is not used (design doc 25 §3)
DECLARATION_UNCONFIRMED = "declaration_unconfirmed"


def plan_source(*, listing, upload: bool, handheld: bool, decl: dict | None, drafted: bool, cameras, kind: str) -> dict:
    """How the task's trajectory is had, in the order of design doc 25 §4.1: an upload; generated - a raw UMI session,
    a handheld gripper's LeRobot export (``meta/umi_calibration.json``) or raw mcap (``handheld``), a robot arm by its
    declaration (``decl``; ``drafted``: only a draft made here, which is never used); the dataset's own
    ``trajectory.json``; else what is missing. ``{"kind", "readiness"?, "missing"?}``."""
    from . import declared

    if upload:
        return {"kind": UPLOAD}
    how = source_of(listing)
    if how in ("session", "generate"):
        return {"kind": SESSION if how == "session" else GENERATE}
    if handheld:
        return {"kind": MCAP_DERIVE}
    ready = declared.readiness(decl, cameras, kind=kind) if decl is not None else None
    if ready is not None and ready["ready"] and not drafted:
        return {"kind": GENERATE, "readiness": ready}
    if how == "dataset_file":
        return {"kind": DATASET_FILE}
    if ready is not None and ready["pose"]:
        missing = list(ready["missing"])
        if ready["ready"] and drafted:
            missing = [{"field": "<declaration>", "code": DECLARATION_UNCONFIRMED}]
        return {"kind": MISSING_DECLARATION, "readiness": ready, "missing": missing}
    return {"kind": MISSING_POSE, **({"readiness": ready} if ready is not None else {})}


def _rot6d(v: np.ndarray) -> np.ndarray:
    """Rotation matrices from the first two columns (rot6d as ``export-umi`` writes it: column 0, then column 1)."""
    a, b = v[:, 0:3], v[:, 3:6]
    x = a / np.linalg.norm(a, axis=1, keepdims=True)
    b = b - (x * b).sum(1, keepdims=True) * x
    y = b / np.linalg.norm(b, axis=1, keepdims=True)
    return np.stack([x, y, np.cross(x, y)], axis=2)


def generate(storage, listing) -> dict:
    """The trajectory bundle of a handheld-gripper LeRobot dataset, from its own state and calibration."""
    import io

    import pyarrow.parquet as pq
    from scipy.spatial.transform import Rotation

    from . import geometry as G
    from .adapters.lerobot_mapping import VERSION, MappingError, _points

    info = json.loads(_read(storage, "meta/info.json"))
    cfg = json.loads(_read(storage, UMI_CALIBRATION_KEY))
    names = list((info.get("features") or {}).get("observation.state", {}).get("names") or [])
    hands = sorted({n.split("_", 1)[0] for n in names if n.startswith("robot") and n.endswith("_pos_x")})
    cams = sorted(cfg.get("cameras") or {})
    if not hands or len(hands) != len(cams):
        raise MappingError("observation.state must hold robotN_pos / rot6d / gripper_width for one hand per camera")
    fps = float(info["fps"])
    prov = cfg.get("provenance") or {"source": "dataset", "method": "generated", "assurance": "declared"}
    episodes = [json.loads(line) for line in _read(storage, "meta/episodes.jsonl").decode().splitlines() if line.strip()]
    chunks = int(info.get("chunks_size") or 1000)
    samples = []
    for row in episodes:
        ep = int(row["episode_index"])
        key = info["data_path"].format(episode_chunk=ep // chunks, episode_index=ep)
        table = pq.read_table(io.BytesIO(_read(storage, key)), columns=["timestamp", "observation.state"])
        t = np.asarray(table.column("timestamp").to_pylist(), float)
        state = np.asarray(table.column("observation.state").to_pylist(), float)
        n = len(t)
        t = t - t[0]
        hand_T, quats, widths = {}, {}, {}
        for hand in hands:
            col = {k: names.index(f"{hand}_{k}") for k in ("pos_x", "rot6d_0", "gripper_width")}
            R = _rot6d(state[:, col["rot6d_0"]:col["rot6d_0"] + 6])
            hand_T[hand] = G.se3(R, state[:, col["pos_x"]:col["pos_x"] + 3])
            quats[hand] = Rotation.from_matrix(R).as_quat()
            widths[hand] = state[:, col["gripper_width"]]
        views, calibrations, camera_poses, media_wh, transforms = [], {}, {}, {}, {}
        for j, cid in enumerate(cams):
            cal = cfg["cameras"][cid]
            vkey = f"observation.images.{cid}"
            shape = info["features"][vkey]["shape"]                       # [h, w, 3]
            wh, source_wh = [int(shape[1]), int(shape[0])], list(cal["image_size_wh"])
            media_wh[cid] = wh
            transforms[cid] = np.diag([wh[0] / source_wh[0], wh[1] / source_wh[1], 1.]).tolist()
            camera_poses[cid] = hand_T[hands[j]] @ np.linalg.inv(np.asarray(cal["T_camera_tcp"], float))
            uri = info["video_path"].format(episode_chunk=ep // chunks, video_key=vkey, episode_index=ep)
            views.append({"view_id": cid, "kind": "camera", "camera_id": cid, "mount": "wrist",
                          "media": {"kind": "video", "uri": uri, "image_size_wh": wh, "frame_count": n,
                                    "fps": fps, "clip_start_s": 0., "clip_end_s": n / fps}})
            calibrations[cid] = {"camera_id": cid, "reference_frame": "umi_world", "image_size_wh": source_wh,
                                 "image_space": "distorted" if cal["model"] != G.PINHOLE else "rectified",
                                 "model": cal["model"], "K": cal["K"],
                                 "distortion_coefficients": cal["distortion_coefficients"],
                                 "extrinsics_mode": "per_frame", "T_reference_camera": None, "provenance": prov}
        sid = f"umi_{ep:06d}"
        points, axes = _points({"tcp_offset_m": [0, 0, 0], "axes": {"from": "tcp", "length_m": .06}}, hands[0], "UMI TCP")
        tasks = row.get("tasks") or [cfg.get("instruction") or ""]
        sample = {"schema_version": VERSION, "sample_id": sid,
                  "source": {"dataset": cfg.get("dataset_id") or str(storage.uri), "episode_id": str(ep),
                             "instruction": tasks[0] if tasks else None},
                  "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": "#calibration",
                  "eef_frame": hands[0], "reference_frame": "umi_world", "views": views,
                  "point_definitions": points, "axis_definitions": axes, "raw_pose_sequence": None,
                  "notes": ["Generated by the platform from the dataset's observation.state and meta/umi_calibration.json; "
                            "a wrist camera's pose is its hand's TCP pose through the declared T_camera_tcp."],
                  "umi": {"camera_hands": {cid: hands[j] for j, cid in enumerate(cams)},
                          "horizon_s": HORIZON_S, "provenance": prov}}
        frames = []
        for i in range(n):
            hp = {h: {"pose": {"pose_type": "absolute", "frame_id": h, "reference_frame": "umi_world",
                               "position_m": hand_T[h][i, :3, 3].tolist(), "quaternion_xyzw": quats[h][i].tolist(),
                               "relative_to": None, "provenance": prov},
                      "opening_m": float(widths[h][i])} for h in hands}
            frames.append({"schema_version": VERSION, "sample_id": sid, "frame_index": i, "timestamp_s": float(t[i]),
                           "source_state_index": i, "source_timing": [], "eef": hp[hands[0]]["pose"], "gripper": None,
                           "hands": hp, "cameras": {cid: {"video_frame_index": i, "video_timestamp_s": float(t[i]),
                            "image_size_wh": media_wh[cid], "calibration_id": cid, "T_reference_camera": Tc[i].tolist(),
                            "H_media_from_calibration": transforms[cid], "projection": None}
                            for cid, Tc in camera_poses.items()}})
        samples.append({"episode_index": ep, "sample": sample,
                        "calibration": {"schema_version": VERSION, "calibrations": calibrations}, "frames": frames})
    return {"schema_version": VERSION, "container": "trajectory-bundle/1.0", "media_uri_base": "lerobot_root",
            "dataset": {"id": cfg.get("dataset_id") or str(storage.uri),
                        "lerobot_codebase_version": str(info.get("codebase_version") or "v2.1"), "fps": fps,
                        "episode_count": len(samples), "generator": "curation eef derive"},
            "samples": samples}


def session(storage, listing) -> dict:
    """The trajectory bundle of a raw UMI session, read in place (``adapters.umi.session_bundle``). A remote
    session's small files (plan, CSVs, SLAM logs, tag and gripper calibrations, intrinsics) are copied into a
    temporary directory; the videos are not - only their headers are read, by ranged GETs."""
    import shutil

    from .adapters import umi

    if not getattr(storage, "remote", False):
        return umi.session_bundle(storage.root, dataset_id=os.path.basename(str(storage.root).rstrip("/")))
    import av

    from ...streams.rangefile import RangeFile

    tmp = tempfile.mkdtemp(prefix="umi-session-")
    try:
        for key in listing:
            name = key.rsplit("/", 1)[-1]
            if key == SESSION_PLAN or name in SESSION_FILES or (name.endswith(".json") and "intrinsics" in name):
                dest = os.path.join(tmp, *key.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as fh:
                    fh.write(storage.read_bytes(key))
        base = str(storage.uri).rstrip("/")

        def video_info(path: str):
            """The video's rate and size from its header, by ranged reads with the dataset's own key."""
            key = f"demos/{path}"
            with RangeFile(lambda start, n: storage.read_range(key, start, n), int(listing[key].size), name=key) as fh:
                with av.open(fh) as inp:
                    stream = inp.streams.video[0]
                    return float(stream.average_rate), [stream.width, stream.height]

        return umi.session_bundle(tmp, video_info=video_info, dataset_id=os.path.basename(base))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bytes(storage, listing) -> bytes:
    """The trajectory bundle of the dataset, generated or its own file."""
    how = source_of(listing)
    if how == "session":
        return json.dumps(session(storage, listing), allow_nan=False).encode()
    if how == "generate":
        return json.dumps(generate(storage, listing), allow_nan=False).encode()
    if how == "dataset_file":
        return _read(storage, DATASET_KEY)
    raise FileNotFoundError("the dataset has neither a handheld-gripper calibration nor a trajectory.json")


def _read(storage, key: str) -> bytes:
    if getattr(storage, "remote", False):
        return storage.read_bytes(key)
    with open(os.path.join(storage.root, key), "rb") as fh:
        return fh.read()


def to_run(storage, listing, run_dir: str) -> str:
    """The dataset's bundle in ``run_dir`` (written once; a resume keeps the bundle it judged)."""
    dest = os.path.join(run_dir, RUN_COPY)
    if not os.path.isfile(dest):
        data = _bytes(storage, listing)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    return dest


def to_temp(storage, listing) -> str:
    """The dataset's bundle in a temporary file (preflight checks it and removes it)."""
    data = _bytes(storage, listing)
    fd, path = tempfile.mkstemp(prefix="eef-trajectory-", suffix=".json")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path
