"""LeRobot columns -> ``trajectory.json`` by an explicit mapping (design 12 §3.3, ``eef-mapping/1.0``).

A convenience for customers and for us, not a platform entry: the platform reads the uploaded
trajectory.json only. The mapping says which column holds the EEF pose and how it is laid out, the
gripper, the tool model, the cameras (video key, id, mount, calibration inline or from a per-frame
column) and the source clocks; nothing is guessed from a column's width. The bundle is form B (pose +
calibration, ``projection: null``): the module recomputes the projection, so there is nothing to keep
consistent by hand. LeRobot v2.1 (one file per episode) and v3.0 (concatenated files, clips located by
the episodes table's ``from_timestamp``) are both read.

    python -m curation.extensions.eef_consistency export --mapping mapping.yaml --lerobot-root DIR --out trajectory.json

Mapping (YAML or JSON)::

    schema_version: eef-mapping/1.0
    dataset_id: galbot/dataset2                        # optional, goes to the bundle
    eef:
      pose_key: observation.state.cartesian_position   # a column; `slice: [a, b]` takes part of it
      layout: xyz_rpy_xyz_extrinsic                    # | xyz_quat_xyzw | xyz_quat_wxyz | xyz_rotmat
      quaternion_key: null                             # quaternion from another column (xyz_quat_*)
      frame_id: panda_link8
      reference_frame: robot_base
      units: {position: m, angle: rad}                 # position m | mm, angle rad | deg
      pose_type: absolute
    gripper:                                           # optional
      key: observation.state.gripper_position          # `index: k` for one element of a wider column
      closed_fraction: identity                        # | one_minus | {min: .., max: ..}
    tool:                                              # the points and axes compared on screen
      tcp_offset_m: [0, 0, 0.16]                       # null: no TCP point
      max_opening_m: 0.085                             # with finger_axis: two finger points (linear gripper)
      finger_axis: local_y
      axes: {from: tcp, length_m: 0.06}                # local x / y / z endpoints from tcp or eef_origin
      assurance: model_assumed                         # declared | model_assumed
    cameras:
      exterior_1_left:
        video_key: observation.images.exterior_1_left
        camera_id: 27432424_left
        mount: fixed_external                          # | wrist
        image_size_wh: [1280, 720]                     # optional: else the video feature's shape
        calibration:
          intrinsics_fx_cx_fy_cy: [..]                 # or K: 3x3
          distortion: {model: pinhole, image_space: rectified, coefficients: []}
          extrinsics: {mode: static, cam2base_xyz_rpy_key: camera_extrinsics.exterior_1_left}
                                                       # or cam2base_xyz_rpy: [6] or T_reference_camera: 4x4
        media_transform: identity                      # or a 3x3 H: calibration image -> media pixels
    timing:
      source_state_index_key: null                     # optional
      source_clocks:
        - {channel: robot_state, key: timestamp_robot_ms, unit: ms, clock_id: droid_recorded_ms, semantics: robot read_start}
        - {channel: exterior_1_left, key: ..., unit: ms, ...}   # a camera key names that camera's clock
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import numpy as np

VERSION = "eef-video/1.0.0"
MAPPING_VERSION = "eef-mapping/1.0"
LAYOUTS = ("xyz_rpy_xyz_extrinsic", "xyz_quat_xyzw", "xyz_quat_wxyz", "xyz_rotmat")
IDENTITY3 = np.eye(3).tolist()


class MappingError(ValueError):
    """The mapping is incomplete or does not fit the dataset; the message names the key."""


def load_mapping(path: str | os.PathLike) -> dict:
    text = pathlib.Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".json"):
        doc = json.loads(text)
    else:
        import yaml

        doc = yaml.safe_load(text)
    check_mapping(doc)
    return doc


def _need(doc: dict, dotted: str):
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or cur.get(part) in (None, ""):
            raise MappingError(f"mapping: {dotted} is required")
        cur = cur[part]
    return cur


def check_mapping(doc: dict) -> None:
    if not isinstance(doc, dict) or doc.get("schema_version") != MAPPING_VERSION:
        raise MappingError(f"mapping: schema_version must be {MAPPING_VERSION}")
    _need(doc, "eef.pose_key")
    if _need(doc, "eef.layout") not in LAYOUTS:
        raise MappingError(f"mapping: eef.layout must be one of {', '.join(LAYOUTS)} (never guessed from widths)")
    for k in ("eef.frame_id", "eef.reference_frame"):
        _need(doc, k)
    units = (doc["eef"].get("units") or {})
    if units.get("position", "m") not in ("m", "mm") or units.get("angle", "rad") not in ("rad", "deg"):
        raise MappingError("mapping: eef.units position m|mm, angle rad|deg")
    if (doc["eef"].get("pose_type") or "absolute") != "absolute":
        raise MappingError("mapping: only absolute poses are exported (a relative pose needs an anchor)")
    cams = doc.get("cameras")
    if not isinstance(cams, dict) or not cams:
        raise MappingError("mapping: cameras is required (one entry per video key)")
    for key, c in cams.items():
        for k in ("video_key", "camera_id", "mount"):
            if not isinstance(c, dict) or not c.get(k):
                raise MappingError(f"mapping: cameras.{key}.{k} is required")
        cal = c.get("calibration") or {}
        if not (cal.get("intrinsics_fx_cx_fy_cy") or cal.get("K")):
            raise MappingError(f"mapping: cameras.{key}.calibration needs intrinsics_fx_cx_fy_cy or K")
        ext = cal.get("extrinsics") or {}
        if not any(ext.get(k) for k in ("cam2base_xyz_rpy_key", "cam2base_xyz_rpy", "T_reference_camera")):
            raise MappingError(f"mapping: cameras.{key}.calibration.extrinsics needs cam2base_xyz_rpy_key, "
                               "cam2base_xyz_rpy or T_reference_camera")


# ----------------------------------------------------------------------------------- LeRobot


class LeRobot:
    """What the export reads from a LeRobot v2.1 / v3.0 dataset: info, episodes, tasks, per-episode rows."""

    def __init__(self, root: str | os.PathLike):
        self.root = pathlib.Path(root)
        self.info = json.loads((self.root / "meta" / "info.json").read_text())
        self.version = str(self.info.get("codebase_version", ""))
        self.fps = float(self.info["fps"])
        self.v3 = self.version.startswith("v3")
        if self.v3:
            import pyarrow.parquet as pq

            files = sorted((self.root / "meta" / "episodes").glob("chunk-*/file-*.parquet"))
            self.episodes = {int(r["episode_index"]): r for f in files for r in pq.read_table(f).to_pylist()}
            tasks = pq.read_table(self.root / "meta" / "tasks.parquet").to_pandas()
            self.tasks = {int(v): str(k) for k, v in tasks["task_index"].items()}
        else:
            rows = [json.loads(x) for x in (self.root / "meta" / "episodes.jsonl").read_text().splitlines() if x.strip()]
            self.episodes = {int(r["episode_index"]): r for r in rows}
            tp = self.root / "meta" / "tasks.jsonl"
            self.tasks = ({int(r["task_index"]): str(r["task"]) for r in
                           (json.loads(x) for x in tp.read_text().splitlines() if x.strip())} if tp.is_file() else {})
        self._frames: dict = {}

    def rows(self, ep: int, columns: list[str]):
        import pyarrow.parquet as pq

        want = list(dict.fromkeys(["episode_index", "frame_index", "timestamp", "task_index", *columns]))
        if self.v3:
            e = self.episodes[ep]
            path = self.root / self.info["data_path"].format(chunk_index=int(e["data/chunk_index"]),
                                                            file_index=int(e["data/file_index"]))
        else:
            chunk = ep // int(self.info.get("chunks_size", 1000))
            path = self.root / self.info["data_path"].format(episode_chunk=chunk, episode_index=ep)
        have = set(pq.read_schema(path).names)
        missing = [c for c in want if c not in have and c != "task_index"]
        if missing:
            raise MappingError(f"columns not in the dataset: {', '.join(missing)}")
        df = pq.read_table(path, columns=[c for c in want if c in have]).to_pandas()
        return df[df["episode_index"] == ep].sort_values("frame_index").reset_index(drop=True)

    def video(self, ep: int, vkey: str, n: int) -> dict:
        e = self.episodes[ep]
        if self.v3:
            uri = self.info["video_path"].format(video_key=vkey, chunk_index=int(e[f"videos/{vkey}/chunk_index"]),
                                                 file_index=int(e[f"videos/{vkey}/file_index"]))
            start, end = float(e[f"videos/{vkey}/from_timestamp"]), float(e[f"videos/{vkey}/to_timestamp"])
        else:
            chunk = ep // int(self.info.get("chunks_size", 1000))
            uri = self.info["video_path"].format(episode_chunk=chunk, video_key=vkey, episode_index=ep)
            start, end = 0.0, n / self.fps
        feat = self.info["features"].get(vkey) or {}
        shape = feat.get("shape") or []
        names = feat.get("names") or []
        wh = None
        if len(shape) == 3 and names and names[0] in ("height", "h"):
            wh = [int(shape[1]), int(shape[0])]
        return {"uri": uri, "clip_start_s": start, "clip_end_s": end, "wh": wh}


# ----------------------------------------------------------------------------------- building


def _column(df, key: str, sl=None, index=None) -> np.ndarray:
    col = np.stack([np.atleast_1d(np.asarray(v, dtype=float)) for v in df[key]])
    if index is not None:
        return col[:, int(index)]
    if sl is not None:
        return col[:, int(sl[0]):int(sl[1])]
    return col


def _rotation(layout: str, rot: np.ndarray, angle_unit: str):
    from scipy.spatial.transform import Rotation

    if layout == "xyz_rpy_xyz_extrinsic":
        return Rotation.from_euler("xyz", rot, degrees=angle_unit == "deg")
    if layout == "xyz_quat_xyzw":
        return Rotation.from_quat(rot)
    if layout == "xyz_quat_wxyz":
        return Rotation.from_quat(rot[:, [1, 2, 3, 0]])
    return Rotation.from_matrix(rot.reshape(-1, 3, 3))


def _pose6_to_T(p) -> list:
    from scipy.spatial.transform import Rotation

    T = np.eye(4)
    T[:3, :3] = Rotation.from_euler("xyz", p[3:6]).as_matrix()
    T[:3, 3] = p[:3]
    return T.tolist()


def _points(tool: dict, frame_id: str, source: str) -> tuple[dict, dict]:
    assurance = tool.get("assurance") or "model_assumed"
    prov = lambda how: {"source": source, "method": how, "assurance": assurance}  # noqa: E731

    def fixed(meaning, offset, how="tool geometry", ass=None):
        p = prov(how)
        if ass:
            p["assurance"] = ass
        return {"meaning": meaning, "frame_id": frame_id, "model": "fixed", "position_eef_m": list(map(float, offset)),
                "position_open_eef_m": None, "position_closed_eef_m": None, "provenance": p}

    points = {"eef_origin": fixed(f"Recorded EEF origin ({frame_id})", [0, 0, 0], "copy", "declared")}
    axes: dict = {}
    tcp = tool.get("tcp_offset_m")
    if tcp is not None:
        points["tcp"] = fixed("Model assumed finger center", tcp)
    ax = tool.get("axes") or {}
    if ax:
        origin = ax.get("from", "tcp" if tcp is not None else "eef_origin")
        if origin == "tcp" and tcp is None:
            raise MappingError("mapping: tool.axes.from is tcp but tool.tcp_offset_m is null")
        base = np.asarray(tcp if origin == "tcp" else [0, 0, 0], float)
        length = float(ax.get("length_m") or 0.06)
        for j, a in enumerate("xyz"):
            end = base.copy()
            end[j] += length
            pid = f"tcp_{a}" if origin == "tcp" else f"{a}_endpoint"
            points[pid] = fixed(f"Local {a} axis endpoint from {origin}", end)
            axes[a] = {"start_point_id": origin, "end_point_id": pid, "physical_meaning": f"local_{a}",
                       "directed": True, "length_m": length}
    opening = tool.get("max_opening_m")
    if opening and tool.get("finger_axis"):
        j = "xyz".index(str(tool["finger_axis"])[-1])
        center = np.asarray(tcp if tcp is not None else [0, 0, 0], float)
        for name, sign in (("finger_plus_" + "xyz"[j], 1), ("finger_minus_" + "xyz"[j], -1)):
            open_p = center.copy()
            open_p[j] += sign * float(opening) / 2
            points[name] = {"meaning": f"Model assumed finger tip ({'+' if sign > 0 else '-'}local {'xyz'[j]})",
                            "frame_id": frame_id, "model": "linear_gripper", "position_eef_m": None,
                            "position_open_eef_m": open_p.tolist(), "position_closed_eef_m": center.tolist(),
                            "provenance": prov("tool geometry")}
        axes["finger_line"] = {"start_point_id": f"finger_minus_{'xyz'[j]}", "end_point_id": f"finger_plus_{'xyz'[j]}",
                               "physical_meaning": "model_assumed_finger_line", "directed": False, "length_m": None}
    return points, axes


def _closed_fraction(v: np.ndarray, how) -> np.ndarray:
    if how in (None, "identity"):
        return v
    if how == "one_minus":
        return 1.0 - v
    if isinstance(how, dict):
        lo, hi = float(how["min"]), float(how["max"])
        return np.clip((v - lo) / (hi - lo), 0.0, 1.0)
    raise MappingError(f"mapping: gripper.closed_fraction {how!r} (identity | one_minus | {{min, max}})")


def export(mapping: dict, lerobot_root: str | os.PathLike, *, episodes: list[int] | None = None) -> dict:
    """The ``trajectory-bundle/1.0`` of the dataset's episodes (form B: pose + calibration)."""
    check_mapping(mapping)
    lr = LeRobot(lerobot_root)
    eef, cams = mapping["eef"], mapping["cameras"]
    grip = mapping.get("gripper") or {}
    timing = mapping.get("timing") or {}
    units = eef.get("units") or {}
    scale = 1e-3 if units.get("position") == "mm" else 1.0
    points, axes = _points(mapping.get("tool") or {}, eef["frame_id"], "mapping.yaml tool model")
    clocks = timing.get("source_clocks") or []
    cols = [eef["pose_key"], *([eef["quaternion_key"]] if eef.get("quaternion_key") else []),
            *([grip["key"]] if grip.get("key") else []), *[c["key"] for c in clocks],
            *([timing["source_state_index_key"]] if timing.get("source_state_index_key") else []),
            *[c["calibration"]["extrinsics"]["cam2base_xyz_rpy_key"] for c in cams.values()
              if c["calibration"]["extrinsics"].get("cam2base_xyz_rpy_key")]]
    samples = []
    for ep in sorted(lr.episodes) if episodes is None else episodes:
        df = lr.rows(ep, cols)
        n = len(df)
        pose = _column(df, eef["pose_key"], eef.get("slice"))
        pos = pose[:, :3] * scale
        if eef.get("quaternion_key"):
            rot = _column(df, eef["quaternion_key"])
        else:
            rot = pose[:, 3:]
        R = _rotation(eef["layout"], rot, units.get("angle", "rad"))
        quat = R.as_quat()
        quat /= np.linalg.norm(quat, axis=1, keepdims=True)
        closed = None
        if grip.get("key"):
            closed = _closed_fraction(_column(df, grip["key"], index=grip.get("index", 0)), grip.get("closed_fraction"))
        t = df["timestamp"].to_numpy(float)
        task = lr.tasks.get(int(df["task_index"].iloc[0])) if "task_index" in df and n else None
        sid = f"{(mapping.get('dataset_id') or 'dataset').split('/')[-1]}_{ep:06d}"
        views, calibs, cam_rows = [], {}, {}
        for key, c in cams.items():
            cid = c["camera_id"]
            vid = lr.video(ep, c["video_key"], n)
            wh = c.get("image_size_wh") or vid["wh"]
            if not wh:
                raise MappingError(f"mapping: cameras.{key}.image_size_wh (the video feature does not say)")
            cal = c["calibration"]
            K = cal.get("K") or (lambda f: [[f[0], 0, f[1]], [0, f[2], f[3]], [0, 0, 1]])(cal["intrinsics_fx_cx_fy_cy"])
            ext = cal["extrinsics"]
            if ext.get("cam2base_xyz_rpy_key"):
                col = _column(df, ext["cam2base_xyz_rpy_key"])
                if not np.allclose(col, col[0], atol=1e-6):
                    raise MappingError(f"cameras.{key}: {ext['cam2base_xyz_rpy_key']} changes within episode {ep}; "
                                       "only static extrinsics are exported")
                T = _pose6_to_T(col[0].astype(float))
            elif ext.get("cam2base_xyz_rpy"):
                T = _pose6_to_T(np.asarray(ext["cam2base_xyz_rpy"], float))
            else:
                T = np.asarray(ext["T_reference_camera"], float).tolist()
            dist = cal.get("distortion") or {}
            cal_id = f"{cid}_declared"
            calibs[cal_id] = {"camera_id": cid, "reference_frame": eef["reference_frame"], "image_size_wh": list(wh),
                              "image_space": dist.get("image_space", "rectified"), "model": dist.get("model", "pinhole"),
                              "K": np.asarray(K, float).tolist(),
                              "distortion_coefficients": list(dist.get("coefficients") or []),
                              "extrinsics_mode": "static", "T_reference_camera": T,
                              "provenance": {"source": f"mapping.yaml cameras.{key}", "method": "copy",
                                             "assurance": "declared"}}
            H = IDENTITY3 if c.get("media_transform", "identity") == "identity" else c["media_transform"]
            views.append({"view_id": cid, "kind": "camera", "camera_id": cid, "mount": c["mount"],
                          "media": {"kind": "video", "uri": vid["uri"], "image_size_wh": list(wh), "frame_count": n,
                                    "fps": lr.fps, "clip_start_s": vid["clip_start_s"], "clip_end_s": vid["clip_end_s"]}})
            cam_rows[key] = (cid, cal_id, list(wh), H)
        sample = {"schema_version": VERSION, "sample_id": sid,
                  "source": {"dataset": mapping.get("dataset_id") or str(lerobot_root), "episode_id": str(ep),
                             "instruction": task or None},
                  "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": "#calibration",
                  "eef_frame": eef["frame_id"], "reference_frame": eef["reference_frame"], "views": views,
                  "point_definitions": points, "axis_definitions": axes, "raw_pose_sequence": None,
                  "notes": [f"exported from LeRobot {lr.version} columns by eef-mapping/1.0; projection left to the "
                            f"platform (form B)"]}
        frames = []
        state_idx = (df[timing["source_state_index_key"]].to_numpy(int) if timing.get("source_state_index_key") else None)
        for i in range(n):
            timing_rows = []
            for c in clocks:
                ch = cams[c["channel"]]["camera_id"] if c["channel"] in cams else c["channel"]
                timing_rows.append({"channel": ch, "timestamp": str(int(df[c["key"]].iloc[i])), "unit": c.get("unit", "ms"),
                                    "clock_id": c.get("clock_id", "recorded"), "semantics": c.get("semantics", "")})
            frames.append({
                "schema_version": VERSION, "sample_id": sid, "frame_index": int(df["frame_index"].iloc[i]),
                "timestamp_s": float(t[i]), "source_state_index": None if state_idx is None else int(state_idx[i]),
                "source_timing": timing_rows,
                "eef": {"pose_type": "absolute", "frame_id": eef["frame_id"], "reference_frame": eef["reference_frame"],
                        "position_m": pos[i].tolist(), "quaternion_xyzw": quat[i].tolist(), "relative_to": None,
                        "provenance": {"source": eef["pose_key"], "method": f"{eef['layout']}; quaternion normalized",
                                       "assurance": "declared"}},
                "gripper": None if closed is None else {
                    "closed_fraction": float(closed[i]), "opening_m": None,
                    "provenance": {"source": grip["key"], "method": str(grip.get("closed_fraction") or "identity"),
                                   "assurance": "declared"}},
                "cameras": {cid: {"video_frame_index": int(df["frame_index"].iloc[i]), "video_timestamp_s": float(t[i]),
                                  "image_size_wh": wh, "calibration_id": cal_id, "T_reference_camera": None,
                                  "H_media_from_calibration": H, "projection": None}
                            for cid, cal_id, wh, H in cam_rows.values()}})
        samples.append({"episode_index": ep, "sample": sample,
                        "calibration": {"schema_version": VERSION, "calibrations": calibs}, "frames": frames})
    return {"schema_version": VERSION, "container": "trajectory-bundle/1.0",
            "dataset": {"id": mapping.get("dataset_id") or str(lerobot_root), "lerobot_codebase_version": lr.version,
                        "fps": lr.fps, "episode_count": len(lr.episodes),
                        "generator": "curation.extensions.eef_consistency export (eef-mapping/1.0)"},
            "media_uri_base": "lerobot_root", "samples": samples}
