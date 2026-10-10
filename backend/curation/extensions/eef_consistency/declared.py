"""A robot arm's trajectory from its dataset's declaration (design doc 25 §4.1, D83).

The pose is in the data (a column of a LeRobot dataset, a topic of an mcap one); the declaration says what it
means (layout, units, the point it places) and how it becomes pixels (each camera's mount, intrinsics and
extrinsics, the tool on the arm). Together they are the ``eef-mapping/1.1`` the export has always read
(``adapters.lerobot_mapping``, design doc 12 §3.3), so nobody uploads a trajectory.json for it: the module generates
each episode's bundle when it comes to it - form B (pose + calibration; the platform projects) - into its output
directory (``trajectory/episode_<N>.json``), where the overlay reads it as it reads a handheld gripper's.

:func:`readiness` says, without reading any row, whether the declaration is enough: the pose, the point it
places, the tool, and per camera its mount, intrinsics and extrinsics (``missing_declaration`` names what is
not); no pose at all is ``missing_pose`` (upload a trajectory.json).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
import threading
from typing import Any, Iterable, Mapping

import numpy as np

from ...declaration import FIXED_EXTERNAL, MOVING, WRIST, short_name

GENERATE_VERSION = "declared-trajectory/1"
#: why a camera cannot be drawn, or the declaration is not enough (C2 preflight ``trajectory.missing[].code``)
POSE_MISSING, POSE_FRAME_UNKNOWN, TOOL_MISSING = "pose_missing", "pose_frame_unknown", "tool_missing"
MOUNT_UNKNOWN, MOVING_UNSUPPORTED = "mount_unknown", "moving_camera_unsupported"
INTRINSICS_MISSING, EXTRINSICS_MISSING, CAMERA_TCP_MISSING = "intrinsics_missing", "extrinsics_missing", "camera_tcp_missing"
NO_DRAWABLE_CAMERA, FORMAT_NOT_GENERATED = "no_drawable_camera", "format_not_generated"
#: why an episode has no generated trajectory (``details.trajectory_source.reason``)
EPISODE_MISSING, NOT_GENERATED, UNREADABLE, INVALID = ("episode_missing", "trajectory_not_generated",
                                                       "record_unreadable", "trajectory_invalid")


def camera_states(decl: Mapping | None, cameras: Iterable[str]) -> list[dict]:
    """Per camera of the dataset: its mount and owner as declared, whether its projection can be computed
    (``drawable``) and why not (``reason``)."""
    cal = ((decl or {}).get("calibration") or {}).get("cameras") or {}
    out = []
    for src in cameras:
        c = cal.get(src)
        state: dict[str, Any] = {"source": src, "camera_id": (c or {}).get("camera_id") or short_name(src),
                                 "mount": (c or {}).get("mount"), "owner": (c or {}).get("owner"),
                                 "drawable": False, "reason": None}
        if c is None:
            state["reason"] = MOUNT_UNKNOWN
        elif c["mount"] == MOVING:
            state["reason"] = MOVING_UNSUPPORTED
        elif not c.get("intrinsics"):
            state["reason"] = INTRINSICS_MISSING
        elif c["mount"] == FIXED_EXTERNAL and (c.get("extrinsics") or {}).get("mode") not in ("static", "column"):
            state["reason"] = EXTRINSICS_MISSING
        elif c["mount"] == WRIST and (c.get("extrinsics") or {}).get("mode") != "camera_tcp":
            state["reason"] = CAMERA_TCP_MISSING
        else:
            state["drawable"] = True
        out.append(state)
    return out


def readiness(decl: Mapping | None, cameras: Iterable[str], *, kind: str = "lerobot") -> dict:
    """``{"ready", "pose", "missing": [{field, code}], "cameras": [...]}``: whether the declaration lets the platform
    generate a robot arm's trajectory (``kind``: the dataset's format; a LeRobot dataset - local or on TOS - and an
    mcap one are generated)."""
    cams = camera_states(decl, cameras)
    sem = (decl or {}).get("semantics") or {}
    pose = sem.get("pose")
    missing: list[dict] = []
    if not isinstance(pose, dict):
        return {"ready": False, "pose": False, "missing": [{"field": "semantics.pose", "code": POSE_MISSING}],
                "cameras": cams}
    if not pose.get("frame_id"):
        missing.append({"field": "semantics.pose.frame_id", "code": POSE_FRAME_UNKNOWN})
    if not isinstance(((decl or {}).get("calibration") or {}).get("tool"), dict):
        missing.append({"field": "calibration.tool", "code": TOOL_MISSING})
    for c in cams:
        if not c["drawable"] and c["reason"] != MOVING_UNSUPPORTED:
            part = {MOUNT_UNKNOWN: "mount", INTRINSICS_MISSING: "intrinsics", EXTRINSICS_MISSING: "extrinsics",
                    CAMERA_TCP_MISSING: "extrinsics"}[c["reason"]]
            missing.append({"field": f"calibration.cameras.{c['source']}.{part}", "code": c["reason"]})
    if not any(c["drawable"] for c in cams):
        missing.append({"field": "calibration.cameras", "code": NO_DRAWABLE_CAMERA})
    if kind not in ("lerobot", "mcap"):
        missing.append({"field": "<dataset>", "code": FORMAT_NOT_GENERATED, "args": {"format": kind}})
    drawable = [c for c in cams if c["drawable"]]
    ready = bool(drawable) and not any(m["code"] in (POSE_FRAME_UNKNOWN, TOOL_MISSING, FORMAT_NOT_GENERATED)
                                       for m in missing)
    return {"ready": ready, "pose": True, "missing": missing, "cameras": cams}


def _K(intr: dict) -> list:
    if intr.get("K") is not None:
        return [list(map(float, r)) for r in intr["K"]]
    f = intr["fx_cx_fy_cy"]
    return [[float(f[0]), 0.0, float(f[1])], [0.0, float(f[2]), float(f[3])], [0.0, 0.0, 1.0]]


def eef_mapping(decl: Mapping, *, cameras: Iterable[str] | None = None, sizes: Mapping[str, Any] | None = None,
                dataset_id: str | None = None) -> dict:
    """The ``eef-mapping/1.1`` a declaration amounts to (the export's input and the record comparison's sources), with
    the drawable cameras (``cameras``: the dataset's camera sources; ``sizes``: their video size [w, h])."""
    sem = decl.get("semantics") or {}
    pose = sem["pose"]
    cal = decl.get("calibration") or {}
    eef: dict[str, Any] = {"pose_key": pose.get("key"), "layout": pose["layout"], "frame_id": pose["frame_id"],
                           "reference_frame": pose["reference_frame"],
                           "units": {"position": pose["units"]["position"], "angle": pose["units"].get("angle", "rad")},
                           "pose_type": "absolute"}
    for k in ("slice", "quaternion_key", "topic", "fields"):
        if pose.get(k) is not None:
            eef[k] = copy.deepcopy(pose[k])
    out: dict[str, Any] = {"schema_version": "eef-mapping/1.1", "eef": eef}
    if dataset_id:
        out["dataset_id"] = dataset_id
    grip = sem.get("gripper")
    if isinstance(grip, dict):
        out["gripper"] = {k: copy.deepcopy(grip[k]) for k in ("key", "topic", "fields", "index", "closed_fraction")
                          if grip.get(k) is not None}
    tool = cal.get("tool") or {}
    out["tool"] = {k: copy.deepcopy(tool[k]) for k in ("tcp_offset_m", "max_opening_m", "finger_axis", "axes")
                   if k in tool}
    out["tool"]["assurance"] = tool.get("assurance") or "model_assumed"
    states = camera_states(decl, cameras if cameras is not None else list((cal.get("cameras") or {})))
    cams: dict[str, dict] = {}
    names: dict[str, str] = {}
    for st in states:
        if not st["drawable"]:
            continue
        src = st["source"]
        c = cal["cameras"][src]
        intr = c["intrinsics"]
        key = short_name(src)
        names[src] = key
        calib: dict[str, Any] = {"K": _K(intr),
                                 "distortion": {"model": intr["model"], "image_space": intr.get("image_space", "rectified"),
                                                "coefficients": list(map(float, intr.get("coefficients") or []))}}
        ext = c["extrinsics"]
        if ext["mode"] == "column":
            calib["extrinsics"] = {"mode": "static", "cam2base_xyz_rpy_key": ext["key"]}
        elif ext["mode"] == "static" and ext.get("xyz_rpy") is not None:
            calib["extrinsics"] = {"mode": "static", "cam2base_xyz_rpy": list(map(float, ext["xyz_rpy"]))}
        elif ext["mode"] == "static":
            calib["extrinsics"] = {"mode": "static", "T_reference_camera": copy.deepcopy(ext["T_reference_camera"])}
        else:
            calib["extrinsics"] = {"mode": "camera_tcp", "T_camera_tcp": copy.deepcopy(ext["T_camera_tcp"])}
        size = (sizes or {}).get(src)
        if intr.get("image_size_wh"):
            calib["image_size_wh"] = list(intr["image_size_wh"])
        entry = {"video_key": src, "camera_id": st["camera_id"], "mount": c["mount"], "calibration": calib}
        if size:
            entry["image_size_wh"] = list(size)
        H = c.get("media_transform", "identity")
        if H == "identity" and intr.get("image_size_wh") and size and list(intr["image_size_wh"]) != list(size):
            cw, ch = intr["image_size_wh"]
            H = [[size[0] / cw, 0.0, 0.0], [0.0, size[1] / ch, 0.0], [0.0, 0.0, 1.0]]
        entry["media_transform"] = H
        if c.get("owner"):
            entry["owner"] = c["owner"]
        cams[key] = entry
    out["cameras"] = cams
    timing = decl.get("timing") or {}
    clocks = []
    for clock in timing.get("source_clocks") or []:
        ch = names.get(clock["channel"], clock["channel"])
        if clock["channel"] in (cal.get("cameras") or {}) and clock["channel"] not in names:
            continue                                     # a camera the trajectory leaves out
        clocks.append({**{k: v for k, v in clock.items() if k != "channel"}, "channel": ch})
    if clocks or timing.get("source_state_index_key"):
        out["timing"] = {"source_clocks": clocks,
                         **({"source_state_index_key": timing["source_state_index_key"]}
                            if timing.get("source_state_index_key") else {})}
    record = (record_block(decl) or {}).get("record") or {}
    out["record"] = record
    return out


def record_block(decl: Mapping) -> dict | None:
    """The ``eef-mapping/1.1`` of the declaration's records alone (the record comparison's sources: the pose and the
    joints, design doc 25 §6.1), or None when it declares neither."""
    sem = decl.get("semantics") or {}
    record: dict[str, Any] = {}
    pose = sem.get("pose")
    if isinstance(pose, dict):
        rp = {k: copy.deepcopy(pose[k]) for k in ("key", "topic", "fields", "slice", "layout", "quaternion_key",
                                                   "reference_frame") if pose.get(k) is not None}
        rp["units"] = {"position": pose["units"]["position"], "angle": pose["units"].get("angle", "rad")}
        rp["frame_id"] = pose.get("frame_id")
        rp["pose_type"] = "absolute"
        record["pose"] = rp
    joints = sem.get("joints")
    if isinstance(joints, dict):
        record["joints"] = {k: copy.deepcopy(joints[k]) for k in ("key", "topic", "fields", "slice", "units", "robot",
                                                                   "reference_frame") if joints.get(k) is not None}
    if sem.get("frames"):
        record["frames"] = copy.deepcopy(sem["frames"])
    if not record.get("pose") and not record.get("joints"):
        return None
    return {"schema_version": "eef-mapping/1.1", "record": record}


def sha256(decl: Mapping) -> str:
    return hashlib.sha256(json.dumps({"declaration": decl, "version": GENERATE_VERSION}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class _Staged:
    """A LeRobot dataset's ``meta/`` and, episode by episode, its data files, copied from a remote store into a
    scratch directory (videos never: the bundle only names them)."""

    def __init__(self, storage, listing: Iterable[str]):
        self.storage = storage
        self.dir = tempfile.mkdtemp(prefix="eef-declared-")
        self.keys = set(listing)
        self._lock = threading.Lock()
        for key in sorted(k for k in self.keys if k.startswith("meta/") and not k.endswith(".mp4")):
            self._fetch(key)

    def _fetch(self, key: str) -> None:
        dest = os.path.join(self.dir, *key.split("/"))
        if os.path.isfile(dest):
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.part-{threading.get_ident()}"
        with open(tmp, "wb") as fh:
            fh.write(self.storage.read_bytes(key))
        os.replace(tmp, dest)

    def data(self, key: str) -> None:
        with self._lock:
            self._fetch(key)

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


class Generated:
    """The task's trajectory generated from the declaration, episode by episode (each once, cached; thread-safe) -
    a LeRobot dataset, local or on a remote store (``storage.remote``: the meta and the episode's data file are
    copied to a scratch directory; videos are named, never read).

    ``out_dir``: the module's output directory; ``sha256`` names what the generation depends on besides the rows
    (the declaration and this code's version), for ``--resume``."""

    def __init__(self, decl: Mapping, *, storage, listing: Iterable[str] | None, out_dir: str, dataset_id: str,
                 cameras: Iterable[str], sizes: Mapping[str, Any] | None = None, version: int | None = None):
        from .adapters import lerobot_mapping as LM

        self.decl = copy.deepcopy(dict(decl))
        self.mapping = eef_mapping(self.decl, cameras=list(cameras), sizes=sizes, dataset_id=dataset_id)
        LM.check_mapping(self.mapping)
        self.sha256 = sha256(self.decl)
        self.version = version
        self.out_dir = out_dir
        self.staged = _Staged(storage, listing if listing is not None else storage.list()) \
            if getattr(storage, "remote", False) else None
        self.root = self.staged.dir if self.staged else str(storage.root)
        self.lr = LM.LeRobot(self.root)
        self.calibration = {"declaration_sha256": self.sha256, "version": version,
                            "cameras": sorted(c["camera_id"] for c in self.mapping["cameras"].values()),
                            "assumed": _assumed_fields(self.decl), "declared_fixed": declared_fixed(self.decl),
                            "camera_sources": camera_sources(self.mapping)}
        self._guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}
        self._done: dict[int, tuple] = {}

    @property
    def episodes(self) -> list[int]:
        return sorted(self.lr.episodes)

    def sample_id(self, ep: int) -> str:
        """The id the export gives the episode's sample (``<dataset>_<episode:06d>``)."""
        return f"{(self.mapping.get('dataset_id') or 'dataset').split('/')[-1]}_{int(ep):06d}"

    def describe(self) -> str:
        v = f"version {self.version}" if self.version is not None else f"sha256 {self.sha256[:12]}"
        return (f"trajectory generated from the dataset's declaration ({v}; cameras "
                f"{', '.join(self.calibration['cameras'])})")

    def close(self) -> None:
        if self.staged is not None:
            self.staged.close()

    def sample(self, ep: int):
        """(the episode's ``EefSample`` or None, its ``trajectory_source``)."""
        ep = int(ep)
        with self._guard:
            if ep in self._done:
                return self._done[ep]
            lock = self._locks.setdefault(ep, threading.Lock())
        with lock:
            with self._guard:
                if ep in self._done:
                    return self._done[ep]
            got = self._generate(ep)
            with self._guard:
                self._done[ep] = got
            return got

    def _source(self, **why) -> dict:
        fixed = self.calibration["declared_fixed"]
        return {"kind": "generated", "declaration": {"sha256": self.sha256, "version": self.version},
                "assumed": self.calibration["assumed"], "camera_sources": self.calibration["camera_sources"],
                **({"declared_fixed": fixed} if fixed else {}), **why}

    def _data_key(self, ep: int) -> str:
        info = self.lr.info
        if self.lr.v3:
            e = self.lr.episodes[ep]
            return info["data_path"].format(chunk_index=int(e["data/chunk_index"]), file_index=int(e["data/file_index"]))
        return info["data_path"].format(episode_chunk=ep // int(info.get("chunks_size", 1000)), episode_index=ep)

    def _generate(self, ep: int):
        from . import derive_mcap as DM
        from . import load
        from .adapters import lerobot_mapping as LM

        if ep not in self.lr.episodes:
            return None, self._source(status="unsupported", reason=EPISODE_MISSING)
        try:
            if self.staged is not None:
                self.staged.data(self._data_key(ep))
            bundle = LM.export(self.mapping, self.root, episodes=[ep], lr=self.lr)
        except LM.MappingError as exc:
            return None, self._source(status="unsupported", reason=NOT_GENERATED, message=str(exc)[:300])
        except (OSError, ValueError, KeyError) as exc:
            return None, self._source(status="unsupported", reason=UNREADABLE, message=f"{type(exc).__name__}: {exc}"[:300])
        bundle["dataset"]["generator"] = f"curation eef {GENERATE_VERSION} (dataset declaration)"
        data = json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()
        res = load.load_bundle(bundle, check_media=False, episodes=[ep], trusted=True)   # its own frames
        if not res.ok or ep not in res.samples:
            message = res.errors[0].message if res.errors else "no sample"
            return None, self._source(status="unsupported", reason=INVALID, message=message[:300])
        _write(DM.bundle_path(self.out_dir, ep), data)
        return res.samples[ep], self._source(status="generated")


#: a camera drafted as moving that a person declared fixed (design doc 25 §3.3): it takes part as a third-person one
#: and the report says 「按声明视为固定」; if it does move, the camera-motion sub-item reports it
MOUNT_DECLARED_FIXED = "mount_declared_fixed"


def declared_fixed(decl: Mapping | None) -> list[str]:
    """The camera ids the declaration takes as fixed by a person's say-so (assumption ``mount_declared_fixed``)."""
    cams = ((decl or {}).get("calibration") or {}).get("cameras") or {}
    return sorted(str(c.get("camera_id") or short_name(src)) for src, c in cams.items()
                  if isinstance(c, dict) and c.get("mount") == FIXED_EXTERNAL
                  and any(isinstance(a, dict) and a.get("code") == MOUNT_DECLARED_FIXED for a in c.get("assumptions") or []))


def camera_sources(mapping: Mapping) -> dict[str, str]:
    """``camera_id -> the declaration's source`` (a LeRobot video key, an mcap topic) of the drawable cameras: what
    ``calibration.cameras.<source>`` in the assumed fields is about (design doc 25 §7.4)."""
    out = {}
    for key, cam in (mapping.get("cameras") or {}).items():
        if isinstance(cam, Mapping) and cam.get("camera_id"):
            out[str(cam["camera_id"])] = str(cam.get("video_key") or cam.get("topic") or key)
    return out


def _assumed_fields(decl: Mapping) -> list[str]:
    from ...declaration import assumed

    return [a["field"] for a in assumed(decl)]


EPISODE_SUFFIX = re.compile(r"_(\d+)$")


def remap_seeds(seed_root: str, generated: "Generated", out_dir: str) -> str:
    """The observation seeds of a generated trajectory: a person's seed rows name their episode by their sample id's
    number (``<anything>_<episode:06d>``, the eef-video convention) and their camera by the declaration's
    ``camera_id``; the platform names the generated samples itself, so the rows are rewritten to its names, laid out
    ``<sample_id>/<camera_id>.jsonl`` under ``out_dir``. Rows whose sample id carries no episode number are left out."""
    root = pathlib.Path(seed_root)
    files = sorted(root.glob("*/*.jsonl")) if root.is_dir() else [root]
    rows: list[dict] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if text.lstrip().startswith("["):
            rows += [r for r in json.loads(text) if isinstance(r, dict)]
        else:
            rows += [json.loads(line) for line in text.splitlines() if line.strip()]
    out = pathlib.Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        m = EPISODE_SUFFIX.search(str(r.get("sample_id") or ""))
        if not m or not r.get("camera_id"):
            continue
        sid = generated.sample_id(int(m.group(1)))
        grouped.setdefault((sid, str(r["camera_id"])), []).append({**r, "sample_id": sid})
    for (sid, cid), got in grouped.items():
        d = out / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in got), encoding="utf-8")
    out.mkdir(parents=True, exist_ok=True)
    return str(out)


# ---------------------------------------------------------------- a robot arm's mcap (design doc 25 §4.1)

#: a pose pairs with a picture when their log times are at most this far apart (or half the pictures' interval)
PAIRING_TOLERANCE_S = 0.05


def _pose_vector(decoded, spec: Mapping) -> list[float] | None:
    """The pose's numbers from a decoded message: the numeric leaves under ``fields`` (or the whole message) in
    order, cut by ``slice``."""
    from ...viz import mcap_messages as M
    from .adapters.umi_mcap import _get

    parts = []
    for path in spec.get("fields") or [None]:
        node = decoded if path is None else _get(decoded, path)
        if node is None:
            return None
        parts += [v for _, v in M.leaves(node)]
    sl = spec.get("slice")
    if sl:
        parts = parts[int(sl[0]):int(sl[1])]
    return [float(v) for v in parts] if parts else None


class GeneratedMcap(Generated):
    """A robot arm's trajectory from an mcap dataset by its declaration: the pose topic and the camera topics of each
    episode file, read in one pass (local, or streamed from TOS); the first drawable camera's pictures are the frames,
    each paired with the nearest pose and every other camera's nearest picture."""

    def __init__(self, decl: Mapping, *, root: str, numbering: Mapping[int, str], out_dir: str, dataset_id: str,
                 cameras: Iterable[str], sizes: Mapping[str, Any] | None = None, version: int | None = None):
        from .adapters import lerobot_mapping as LM

        self.decl = copy.deepcopy(dict(decl))
        pose = (self.decl.get("semantics") or {}).get("pose") or {}
        if not pose.get("topic"):
            raise LM.MappingError("an mcap dataset's pose is a topic (semantics.pose.topic)")
        self.mapping = eef_mapping(self.decl, cameras=list(cameras), sizes=sizes, dataset_id=dataset_id)
        if not self.mapping["cameras"]:
            raise LM.MappingError("no camera can be drawn from the declaration")
        self.sha256 = sha256(self.decl)
        self.version = version
        self.out_dir = out_dir
        self.root, self.numbering = str(root), dict(numbering)
        self.staged = None
        self.calibration = {"declaration_sha256": self.sha256, "version": version,
                            "cameras": sorted(c["camera_id"] for c in self.mapping["cameras"].values()),
                            "assumed": _assumed_fields(self.decl), "declared_fixed": declared_fixed(self.decl),
                            "camera_sources": camera_sources(self.mapping)}
        self._guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}
        self._done: dict[int, tuple] = {}

    @property
    def episodes(self) -> list[int]:
        return sorted(self.numbering)

    def _generate(self, ep: int):
        from . import derive_mcap as DM
        from . import load
        from .adapters import lerobot_mapping as LM

        name = self.numbering.get(ep)
        if name is None:
            return None, self._source(status="unsupported", reason=EPISODE_MISSING)
        try:
            bundle = self._bundle(ep, DM._join(self.root, name), name)
        except LM.MappingError as exc:
            return None, self._source(status="unsupported", reason=NOT_GENERATED, message=str(exc)[:300])
        except (OSError, ValueError, KeyError) as exc:
            return None, self._source(status="unsupported", reason=UNREADABLE, message=f"{type(exc).__name__}: {exc}"[:300])
        bundle["dataset"]["generator"] = f"curation eef {GENERATE_VERSION} (dataset declaration, mcap)"
        data = json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()
        res = load.load_bundle(bundle, check_media=False, episodes=[ep], trusted=True)   # its own frames
        if not res.ok or ep not in res.samples:
            message = res.errors[0].message if res.errors else "no sample"
            return None, self._source(status="unsupported", reason=INVALID, message=message[:300])
        _write(DM.bundle_path(self.out_dir, ep), data)
        return res.samples[ep], self._source(status="generated")

    def _scan(self, path: str) -> tuple[dict, list[int], list[list[float]]]:
        from mcap.reader import make_reader

        from ...viz import mcap_messages as M
        from .adapters.umi_mcap import _open

        pose = self.mapping["record"]["pose"]
        cams = {c["video_key"]: {"log": [], "size": None} for c in self.mapping["cameras"].values()}
        plog: list[int] = []
        pvals: list[list[float]] = []
        dec = M.Decoder()
        with _open(path) as fh:
            for schema, channel, message in make_reader(fh).iter_messages(topics=sorted({pose["topic"], *cams}),
                                                                         log_time_order=True):
                if channel.topic in cams:
                    cam = cams[channel.topic]
                    cam["log"].append(int(message.log_time))
                    if cam["size"] is None:
                        decoded = dec.decode(channel, schema, message)
                        frame = M.as_frame(decoded) if decoded is not None else None
                        if frame is not None:
                            fmt, data = frame
                            codec = M.frame_codec(fmt, data)
                            if codec in ("jpeg", "png"):
                                w, h = M.picture_size(codec, data)
                                cam["size"] = [w, h] if w and h else None
                    continue
                decoded = dec.decode(channel, schema, message)
                vec = _pose_vector(decoded, pose) if decoded is not None else None
                if vec is not None:
                    plog.append(int(message.log_time))
                    pvals.append(vec)
        return cams, plog, pvals

    def _bundle(self, ep: int, path: str, uri: str) -> dict:
        from .adapters import lerobot_mapping as LM

        cams, plog, pvals = self._scan(path)
        eef = self.mapping["eef"]
        order = list(self.mapping["cameras"].values())
        ref = order[0]["video_key"]
        frames_ns = np.asarray(cams[ref]["log"], np.int64)
        if not len(frames_ns):
            raise LM.MappingError(f"{ref}: no picture in {uri}")
        if not plog:
            raise LM.MappingError(f"{eef.get('topic')}: no pose in {uri}")
        P = np.asarray(pvals, float)
        need = {"xyz_rpy_xyz_extrinsic": 6, "xyz_quat_xyzw": 7, "xyz_quat_wxyz": 7, "xyz_rotmat": 12, "xyz_rot6d": 9}[eef["layout"]]
        if P.shape[1] != need:
            raise LM.MappingError(f"{eef.get('topic')}: {P.shape[1]} numbers per pose, {eef['layout']} takes {need}")
        scale = 1e-3 if (eef.get("units") or {}).get("position") == "mm" else 1.0
        R = LM._rotation(eef["layout"], P[:, 3:], (eef.get("units") or {}).get("angle", "rad"))
        quat = R.as_quat()
        quat /= np.linalg.norm(quat, axis=1, keepdims=True)
        T_ref_eef = np.tile(np.eye(4), (len(P), 1, 1))
        T_ref_eef[:, :3, :3] = R.as_matrix()
        T_ref_eef[:, :3, 3] = P[:, :3] * scale
        tcp = (self.mapping.get("tool") or {}).get("tcp_offset_m")
        T_eef_tcp = np.eye(4)
        if tcp is not None:
            T_eef_tcp[:3, 3] = np.asarray(tcp, float)

        def nearest(log: np.ndarray, t: np.ndarray, tol_ns: float) -> np.ndarray:
            idx = np.clip(np.searchsorted(log, t), 1, len(log) - 1) if len(log) > 1 else np.zeros(len(t), int)
            if len(log) > 1:
                left = idx - 1
                idx = np.where(np.abs(log[left] - t) <= np.abs(log[idx] - t), left, idx)
            ok = np.abs(log[idx] - t) <= tol_ns
            return np.where(ok, idx, -1)

        def interval(log: np.ndarray) -> float:
            return float(np.median(np.diff(log))) if len(log) > 1 else 1e8

        plog_a = np.asarray(plog, np.int64)
        pose_of = nearest(plog_a, frames_ns, max(PAIRING_TOLERANCE_S * 1e9, interval(frames_ns) / 2))
        n = len(frames_ns)
        t = (frames_ns - frames_ns[0]) / 1e9
        sid = self.sample_id(ep)
        points, axes = LM._points(self.mapping.get("tool") or {}, eef["frame_id"], "dataset declaration tool model")
        views, calibs, rows = [], {}, []
        for c in order:
            topic, cid = c["video_key"], c["camera_id"]
            log = np.asarray(cams[topic]["log"], np.int64)
            if not len(log):
                continue
            fps = 1e9 / interval(log)
            wh = c.get("image_size_wh") or cams[topic]["size"]
            if not wh:
                raise LM.MappingError(f"{topic}: the picture size is not known")
            cal = c["calibration"]
            ext = cal["extrinsics"]
            per_frame, T = None, None
            if ext.get("T_camera_tcp") is not None:
                per_frame = T_ref_eef @ T_eef_tcp @ np.linalg.inv(np.asarray(ext["T_camera_tcp"], float))
            elif ext.get("cam2base_xyz_rpy") is not None:
                T = LM._pose6_to_T(np.asarray(ext["cam2base_xyz_rpy"], float))
            else:
                T = np.asarray(ext["T_reference_camera"], float).tolist()
            dist = cal.get("distortion") or {}
            cal_id = f"{cid}_declared"
            calibs[cal_id] = {"camera_id": cid, "reference_frame": eef["reference_frame"],
                              "image_size_wh": list(cal.get("image_size_wh") or wh),
                              "image_space": dist.get("image_space", "rectified"), "model": dist.get("model", "pinhole"),
                              "K": np.asarray(cal["K"], float).tolist(),
                              "distortion_coefficients": list(dist.get("coefficients") or []),
                              "extrinsics_mode": "static" if per_frame is None else "per_frame", "T_reference_camera": T,
                              "provenance": {"source": f"dataset declaration {topic}", "method": "copy",
                                             "assurance": "declared"}}
            H = c.get("media_transform", "identity")
            H = LM.IDENTITY3 if H == "identity" else H
            views.append({"view_id": cid, "kind": "camera", "camera_id": cid, "mount": c["mount"],
                          "media": {"kind": "video", "uri": uri, "topic": topic, "image_size_wh": list(wh),
                                    "frame_count": int(len(log)), "fps": round(fps, 6), "clip_start_s": 0.0,
                                    "clip_end_s": None}})
            index = np.arange(n) if topic == ref else nearest(log, frames_ns, max(PAIRING_TOLERANCE_S * 1e9, interval(log) / 2))
            rows.append((cid, cal_id, list(wh), H, per_frame, index, fps))
        frames = []
        for i in range(n):
            k = int(pose_of[i])
            cams_i = {}
            for cid, cal_id, wh, H, per_frame, index, fps in rows:
                j = int(index[i])
                cams_i[cid] = {"video_frame_index": j if j >= 0 else -1, "video_timestamp_s": (j / fps) if j >= 0 else None,
                               "image_size_wh": wh, "calibration_id": cal_id,
                               "T_reference_camera": None if per_frame is None or k < 0 else per_frame[k].tolist(),
                               "H_media_from_calibration": H, "projection": None}
            frames.append({"schema_version": LM.VERSION, "sample_id": sid, "frame_index": i, "timestamp_s": float(t[i]),
                           "source_state_index": None, "source_timing": [],
                           "eef": None if k < 0 else {
                               "pose_type": "absolute", "frame_id": eef["frame_id"], "reference_frame": eef["reference_frame"],
                               "position_m": T_ref_eef[k, :3, 3].tolist(), "quaternion_xyzw": quat[k].tolist(),
                               "relative_to": None,
                               "provenance": {"source": eef.get("topic"), "method": f"{eef['layout']}; nearest by log time",
                                              "assurance": "declared"}},
                           "gripper": None, "cameras": cams_i})
        sample = {"schema_version": LM.VERSION, "sample_id": sid,
                  "source": {"dataset": self.mapping.get("dataset_id") or self.root, "episode_id": str(ep), "instruction": None},
                  "frame_count": n, "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": "#calibration",
                  "eef_frame": eef["frame_id"], "reference_frame": eef["reference_frame"], "views": views,
                  "point_definitions": points, "axis_definitions": axes, "raw_pose_sequence": None,
                  "notes": [f"generated from the mcap topics {eef.get('topic')} and {', '.join(c['video_key'] for c in order)} "
                            f"by the dataset declaration; projection left to the platform (form B)"]}
        return {"schema_version": LM.VERSION, "container": "trajectory-bundle/1.0",
                "dataset": {"id": self.mapping.get("dataset_id") or self.root, "lerobot_codebase_version": "mcap",
                            "fps": round(1e9 / interval(frames_ns), 6), "episode_count": len(self.numbering)},
                "media_uri_base": "lerobot_root",
                "samples": [{"episode_index": int(ep), "sample": sample,
                             "calibration": {"schema_version": LM.VERSION, "calibrations": calibs}, "frames": frames}]}
