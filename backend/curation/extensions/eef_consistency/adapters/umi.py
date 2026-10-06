"""Explicitly calibrated UMI raw sessions -> LeRobot plus EEF trajectory bundle.

The plan supplies absolute TCP poses and raw video frame ranges; the CSV supplies
camera poses in SLAM coordinates. Neither the plan nor calibration is fitted to
make projections agree. The original session is never modified.
"""
from __future__ import annotations

import base64
import json
import pathlib
import pickle

import numpy as np

from .. import geometry as G
from .lerobot_mapping import VERSION, MappingError, _points


class _PlanReader(pickle.Unpickler):
    def find_class(self, module, name):
        # Old/new NumPy module paths are both common in published UMI sessions.
        from numpy.core.multiarray import _reconstruct

        allowed = {("numpy", "ndarray"): np.ndarray, ("numpy", "dtype"): np.dtype,
                   ("numpy.core.multiarray", "_reconstruct"): _reconstruct,
                   ("numpy._core.multiarray", "_reconstruct"): _reconstruct}
        if (module, name) not in allowed:
            raise MappingError(f"UMI plan: unsupported pickle global {module}.{name}")
        return allowed[module, name]


def read_plan(path):
    with open(path, "rb") as f:
        plans = _PlanReader(f).load()
    if not isinstance(plans, list) or not plans:
        raise MappingError("UMI plan must contain a nonempty episode list")
    return plans


def read_calibration(path):
    cfg = json.loads(pathlib.Path(path).read_text())
    if cfg.get("schema_version") != "umi-calibration/1":
        raise MappingError("calibration.schema_version must be umi-calibration/1")
    for key in ("dataset_id", "instruction", "provenance", "cameras", "T_world_slam"):
        if not cfg.get(key):
            raise MappingError(f"calibration.{key} is required")
    if not G.is_rigid(cfg["T_world_slam"]):
        raise MappingError("T_world_slam must be a rigid 4x4 transform")
    for name, cam in cfg["cameras"].items():
        K = np.asarray(cam.get("K"), float)
        if K.shape != (3, 3) or not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0:
            raise MappingError(f"{name}: explicit camera K is required")
        if cam.get("model") not in (G.PINHOLE, G.BROWN, G.FISHEYE):
            raise MappingError(f"{name}: explicit supported camera model is required")
        if "distortion_coefficients" not in cam or not cam.get("image_size_wh"):
            raise MappingError(f"{name}: distortion_coefficients and image_size_wh are required")
        if not G.is_rigid(cam.get("T_camera_tcp")):
            raise MappingError(f"{name}: explicit rigid T_camera_tcp is required")
    return cfg


def slam_to_tag(T_slam_object, *, T_tag_slam) -> np.ndarray:
    """Express an object's SLAM-world pose in the tag-aligned coordinate frame.

    T_A_B maps coordinates from B to A. T_slam_object is a (4, 4) pose or
    an (N, 4, 4) pose sequence. T_tag_slam is the fixed SLAM -> tag transform:
    inverse(tx_slam_tag) from the session's alignment, stored as T_world_slam
    in our calibration config. This applies the supplied alignment; it does
    not estimate it. TCP poses already expressed in the tag frame need no conversion.
    """
    return np.asarray(T_tag_slam) @ np.asarray(T_slam_object)


def _source(root, relative):
    path = pathlib.Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise MappingError(f"UMI video_path must be relative: {relative}")
    source = (root / "demos" / path).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise MappingError(f"UMI video is missing or outside the session: {relative}")
    return source


def _camera_rows(csv, times):
    import pandas as pd

    df = pd.read_csv(csv)
    ts = df["timestamp"].to_numpy(float)
    if not np.isfinite(ts).all() or np.any(np.diff(ts) <= 0):
        raise MappingError(f"{csv}: camera timestamps must increase")
    hi = np.searchsorted(ts, times).clip(0, len(ts) - 1)
    lo = (hi - 1).clip(0)
    idx = np.where(abs(ts[lo] - times) < abs(ts[hi] - times), lo, hi)
    # CSV timestamps are printed at microsecond precision, not a license to pair another frame.
    if np.max(abs(ts[idx] - times)) > 2e-5 or len(np.unique(idx)) != len(idx):
        raise MappingError(f"{csv}: camera trajectory does not match raw video frame times")
    selected = df.iloc[idx]
    if selected["is_lost"].astype(str).str.lower().isin(["true", "1"]).any():
        raise MappingError(f"{csv}: selected frames contain lost SLAM poses")
    q = selected[["q_x", "q_y", "q_z", "q_w"]].to_numpy(float)
    p = selected[["x", "y", "z"]].to_numpy(float)
    if not np.isfinite(q).all() or not np.isfinite(p).all() or np.any(abs(np.linalg.norm(q, axis=1) - 1) > 1e-5):
        raise MappingError(f"{csv}: invalid camera pose")
    return G.pose_matrix(p, q)


def export(root, calibration, out, *, horizon_s=1.0, max_side=960):
    """Create a new dataset directory, using raw video pixels (scale only, no inferred crop)."""
    import av
    import cv2
    import pyarrow as pa
    import pyarrow.parquet as pq
    from scipy.spatial.transform import Rotation

    from ....adapters.video_input import encode_rendered_video
    from ..load import load_bundle

    root, out = pathlib.Path(root), pathlib.Path(out)
    cfg = read_calibration(calibration)
    plans = read_plan(root / "dataset_plan.pkl")
    if not np.isfinite(horizon_s) or not 0 <= horizon_s <= 5 or max_side < 32:
        raise MappingError("horizon_s must be 0..5 and max_side at least 32")
    if out.exists():
        raise MappingError(f"output already exists: {out}")
    out.mkdir(parents=True)
    samples, episodes, features, fps_all = [], [], {}, []
    prov = cfg["provenance"]
    for ep, plan in enumerate(plans):
        t = np.asarray(plan["episode_timestamps"], float)
        if t.ndim != 1 or len(t) < 2 or not np.isfinite(t).all() or np.any(np.diff(t) <= 0):
            raise MappingError(f"episode {ep}: invalid timestamps")
        t = t - t[0]
        n = len(t)
        cams, grippers = plan["cameras"], plan["grippers"]
        if len(cams) != len(grippers) or len(cams) not in (1, 2):
            raise MappingError("UMI export requires one wrist camera per hand (one or two hands)")
        if set(cfg["cameras"]) != {f"camera{i}" for i in range(len(cams))}:
            raise MappingError("calibration cameras must match dataset plan order")
        views, calibrations, camera_poses, media_wh, transforms = [], {}, {}, {}, {}
        hand_poses, quats, state = {}, {}, []
        for j, grip in enumerate(grippers):
            hand = f"robot{j}"
            pose = np.asarray(grip["tcp_pose"], float)
            widths = np.asarray(grip["gripper_width"], float).reshape(-1)
            if (pose.shape != (n, 6) or widths.shape != (n,) or not np.isfinite(pose).all()
                    or not np.isfinite(widths).all() or np.any(widths < 0)):
                raise MappingError(f"episode {ep}/{hand}: invalid absolute TCP poses or opening widths")
            rot = Rotation.from_rotvec(pose[:, 3:])
            quats[hand] = rot.as_quat()
            hand_poses[hand] = G.se3(rot.as_matrix(), pose[:, :3])
            state.append(np.column_stack([pose[:, :3], rot.as_matrix()[:, :, :2].transpose(0, 2, 1).reshape(n, 6), widths]))
        for j, camera in enumerate(cams):
            cid = f"camera{j}"
            cal = cfg["cameras"][cid]
            video = _source(root, camera["video_path"])
            first, end = camera["video_start_end"]
            if not isinstance(first, int) or not isinstance(end, int) or first < 0 or end <= first or (end-first) % n:
                raise MappingError(f"{cid}: invalid raw video frame range")
            stride = (end - first) // n
            with av.open(str(video)) as inp:
                stream = inp.streams.video[0]
                rate = float(stream.average_rate)
                source_wh = [stream.width, stream.height]
                if source_wh != cal["image_size_wh"] or end > stream.frames:
                    raise MappingError(f"{cid}: calibration image size or frame range does not match raw video")
            fps = rate / stride
            if abs(np.median(np.diff(t)) * fps - 1) > 1e-4:
                raise MappingError(f"{cid}: plan timestamps and video sampling rates differ")
            fps_all.append(fps)
            raw_indices = np.arange(first, end, stride)
            T_slam_camera = _camera_rows(video.parent / "camera_trajectory.csv", raw_indices / rate)
            T_tag_camera = slam_to_tag(T_slam_camera, T_tag_slam=cfg["T_world_slam"])
            camera_poses[cid] = T_tag_camera
            scale = min(1., max_side / max(source_wh))
            wh = [max(2, int(v * scale) // 2 * 2) for v in source_wh]
            media_wh[cid] = wh
            H = np.diag([wh[0] / source_wh[0], wh[1] / source_wh[1], 1.])
            transforms[cid] = H.tolist()
            vkey = f"observation.images.{cid}"
            uri = f"videos/chunk-000/{vkey}/episode_{ep:06d}.mp4"

            def rendered():
                count = 0
                with av.open(str(video)) as inp:
                    for index, fr in enumerate(inp.decode(video=0)):
                        if index >= end:
                            break
                        if index >= first and (index-first) % stride == 0:
                            if fr.pts is None or abs(float(fr.pts * fr.time_base) - index / rate) > 1e-4:
                                raise MappingError(f"{cid}: video is not on its declared constant frame grid")
                            yield float(t[count]), cv2.resize(fr.to_ndarray(format="rgb24"), tuple(wh))
                            count += 1
                if count != n:
                    raise MappingError(f"{cid}: expected {n} frames, decoded {count}")

            clip = encode_rendered_video(cid, rendered(), fps=fps, end_s=n / fps, max_bytes=128 * 1024 * 1024)
            dest = out / uri
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(clip.url.split(",", 1)[1]))
            views.append({"view_id": cid, "kind": "camera", "camera_id": cid, "mount": "wrist",
                          "media": {"kind": "video", "uri": uri, "image_size_wh": wh, "frame_count": n,
                                    "fps": fps, "clip_start_s": 0., "clip_end_s": n / fps}})
            calibrations[cid] = {"camera_id": cid, "reference_frame": "umi_world", "image_size_wh": source_wh,
                                 "image_space": "distorted" if cal["model"] != G.PINHOLE else "rectified",
                                 "model": cal["model"], "K": cal["K"],
                                 "distortion_coefficients": cal["distortion_coefficients"],
                                 "extrinsics_mode": "per_frame", "T_reference_camera": None, "provenance": prov}
            features[vkey] = {"dtype": "video", "shape": [wh[1], wh[0], 3], "names": ["height", "width", "channels"],
                               "video_info": {"video.fps": fps, "video.codec": "h264", "video.pix_fmt": "yuv420p"}}
        sid = f"umi_{ep:06d}"
        points, axes = _points({"tcp_offset_m": [0, 0, 0], "axes": {"from": "tcp", "length_m": .06}}, "robot0", "UMI TCP")
        sample = {"schema_version": VERSION, "sample_id": sid,
                  "source": {"dataset": cfg["dataset_id"], "episode_id": str(ep), "instruction": cfg["instruction"]},
                  "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": "#calibration",
                  "eef_frame": "robot0", "reference_frame": "umi_world", "views": views,
                  "point_definitions": points, "axis_definitions": axes, "raw_pose_sequence": None,
                  "notes": ["UMI absolute recorded TCP trajectories; action/state are the same observation, not independent commands.",
                            "Raw source images, resized only; Camera->TCP geometry is declared, not independently calibrated."],
                  "umi": {"camera_hands": {f"camera{j}": f"robot{j}" for j in range(len(cams))},
                          "horizon_s": horizon_s, "provenance": prov}}
        frames = []
        for i in range(n):
            hands = {}
            for j, grip in enumerate(grippers):
                hand = f"robot{j}"
                hands[hand] = {"pose": {"pose_type": "absolute", "frame_id": hand, "reference_frame": "umi_world",
                                        "position_m": hand_poses[hand][i, :3, 3].tolist(), "quaternion_xyzw": quats[hand][i].tolist(),
                                        "relative_to": None, "provenance": prov},
                               "opening_m": float(np.asarray(grip["gripper_width"]).reshape(-1)[i])}
            frames.append({"schema_version": VERSION, "sample_id": sid, "frame_index": i, "timestamp_s": float(t[i]),
                           "source_state_index": i, "source_timing": [], "eef": hands["robot0"]["pose"], "gripper": None,
                           "hands": hands, "cameras": {cid: {"video_frame_index": i, "video_timestamp_s": float(t[i]),
                            "image_size_wh": media_wh[cid], "calibration_id": cid, "T_reference_camera": Tc[i].tolist(),
                            "H_media_from_calibration": transforms[cid], "projection": None} for cid, Tc in camera_poses.items()}})
        samples.append({"episode_index": ep, "sample": sample,
                        "calibration": {"schema_version": VERSION, "calibrations": calibrations}, "frames": frames})
        values = np.concatenate(state, axis=1).astype(np.float32)
        path = out / f"data/chunk-000/episode_{ep:06d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"episode_index": [ep]*n, "frame_index": list(range(n)), "timestamp": t,
                                 "task_index": [0]*n, "index": list(range(sum(e['length'] for e in episodes), sum(e['length'] for e in episodes)+n)),
                                 "observation.state": values.tolist(), "action": values.tolist()}), path)
        episodes.append({"episode_index": ep, "tasks": [cfg["instruction"]], "length": n})
    if not np.allclose(fps_all, fps_all[0]):
        raise MappingError("UMI export requires the same sampling rate in every episode")
    names = [name for j in range(len(plans[0]["grippers"])) for name in
             [*[f"robot{j}_pos_{a}" for a in "xyz"], *[f"robot{j}_rot6d_{k}" for k in range(6)], f"robot{j}_gripper_width"]]
    features.update({key: {"dtype": "float32", "shape": [len(names)], "names": names} for key in ("action", "observation.state")})
    for key in ("episode_index", "frame_index", "task_index", "index", "timestamp"):
        features[key] = {"dtype": "float32" if key == "timestamp" else "int64", "shape": [1], "names": None}
    meta = out / "meta"
    meta.mkdir()
    info = {"codebase_version": "v2.1", "robot_type": "umi_dual_handheld_gripper" if len(names) == 20 else "umi_handheld_gripper",
            "fps": fps_all[0], "total_episodes": len(episodes), "total_frames": sum(e["length"] for e in episodes),
            "total_tasks": 1, "total_videos": len(fps_all), "total_chunks": 1, "chunks_size": max(1000, len(episodes)),
            "splits": {"train": f"0:{len(episodes)}"}, "features": features,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"}
    (meta / "info.json").write_text(json.dumps(info, indent=2))
    (meta / "episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in episodes))
    (meta / "tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": cfg["instruction"]}) + "\n")
    (meta / "umi_calibration.json").write_text(json.dumps(cfg, indent=2))
    bundle = {"schema_version": VERSION, "container": "trajectory-bundle/1.0", "media_uri_base": "lerobot_root",
              "dataset": {"id": cfg["dataset_id"], "lerobot_codebase_version": "v2.1", "fps": fps_all[0],
                          "episode_count": len(samples), "generator": "eef export-umi"}, "samples": samples}
    trajectory = out / "trajectory.json"
    trajectory.write_text(json.dumps(bundle, allow_nan=False))
    result = load_bundle(trajectory, lerobot_root=out)
    if not result.ok:
        raise MappingError("UMI export validation: " + "; ".join(e.message for e in result.errors[:3]))
    return {"dataset": str(out), "trajectory": str(trajectory), "episodes": len(samples), "frames": info["total_frames"]}
