"""The uploaded trajectory against the dataset's own record (design 12 §8.7, D-E16).

Answers one question: does the uploaded 3D end-effector trajectory agree with what the dataset itself
recorded? Two sources, each compared on its own: the dataset's pose columns (or mcap pose topic), and
its joint angles through the forward kinematics of a built-in robot model. Where they are and how to
read them is written out by the user in the record mapping (``eef-mapping/1.1`` ``record``; a 1.0
mapping's ``eef`` block is the pose source) - never guessed. No video is decoded (except the optional
overlay evidence), no gripper reference and no model are needed, and the result is reported only: it is
never part of the verdict.

Per source: pair the upload's frames with the record (``source_state_index`` > frame index when the
counts match > timestamps, positions linear and rotations SLERP); take the expected relation between
the two frames from the mapping (same ``frame_id`` -> identity, otherwise a path through ``record.frames``
and the robot's named frames); the primary numbers are the raw residuals under that relation (mm, deg);
a constant fitted on the tool side is reported and, when the relation is declared, compared with it
(``constant_mismatch``); what remains after the constant is segmented (``record_deviation``); the time
offset between the two tracks is estimated and reported.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
from typing import Any, Iterable

import numpy as np

from . import contracts as C
from . import geometry as G
from . import metrics as MX
from . import robots as RB
from . import segments as SG
from . import timeline as TL

MAPPING_VERSIONS = ("eef-mapping/1.0", "eef-mapping/1.1")
SOURCES = ("pose", "joints")
LAYOUTS = ("xyz_rpy_xyz_extrinsic", "xyz_quat_xyzw", "xyz_quat_wxyz", "xyz_rotmat")
#: demo thresholds, used when a profile has no ``record`` section (profiles/demo.yaml has them)
DEFAULTS: dict[str, float] = {
    "min_frames": 30, "min_fraction": 0.3, "rolling_median_frames": 5,
    "pos_on_mm": 5.0, "pos_off_mm": 3.0, "rot_on_deg": 2.0, "rot_off_deg": 1.5,
    "min_duration_s": 0.5, "max_gap_s": 0.4,
    "constant_mm": 5.0, "constant_deg": 2.0,
    "lag_min_frames": 1.0, "lag_min_improvement": 1.5,
}
CURVE_POINTS = 300
_RANK = {C.SUSPECT: 4, C.ERROR: 3, C.UNKNOWN: 2, C.OK: 1, C.UNSUPPORTED: 0}


class RecordMappingError(ValueError):
    """The record mapping is incomplete or inconsistent; the message names the key."""


class RobotModelUnknown(RecordMappingError):
    """The mapping names a robot model that is not built in (``robot_model_unknown``)."""


class RecordDataError(RuntimeError):
    """The dataset's data does not fit the mapping (a column / topic missing, a width that does not fit)."""


# ------------------------------------------------------------------------------------ the mapping


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    kind: str                           # pose | joints
    key: str | None                     # LeRobot column
    topic: str | None                   # mcap topic
    fields: str | None                  # mcap field path (dotted), None: the message's numeric shape
    slice: tuple[int, int] | None
    layout: str | None                  # pose only
    quaternion_key: str | None          # pose only, LeRobot: the quaternion in another column
    position_scale: float               # metres per unit
    angle_unit: str                     # rad | deg
    frame_id: str                       # pose: declared; joints: the robot's tip frame
    reference_frame: str
    robot: str | None                   # joints only

    def as_dict(self) -> dict:
        return {k: v for k, v in {"kind": self.kind, "key": self.key, "topic": self.topic, "fields": self.fields,
                                  "slice": list(self.slice) if self.slice else None, "layout": self.layout,
                                  "robot": self.robot, "frame_id": self.frame_id,
                                  "reference_frame": self.reference_frame}.items() if v is not None}


@dataclasses.dataclass(frozen=True)
class RecordMapping:
    sources: dict[str, SourceSpec]
    frames: dict[str, tuple[str, np.ndarray]]     # declared fixed frames: name -> (parent, T_parent_name)
    sha256: str

    def transform(self, a: str, b: str, robot: str | None = None) -> np.ndarray | None:
        """``T_a_b`` (frame ``b`` in frame ``a``) through the declared frames and the robot's named frames;
        None when nothing connects them."""
        if a == b:
            return np.eye(4)
        edges: list[tuple[str, str, np.ndarray]] = [(p, n, T) for n, (p, T) in self.frames.items()]
        if robot is not None:
            edges += [(p, n, T) for n, (p, T) in RB.get(robot).frames.items()]
        adj: dict[str, list[tuple[str, np.ndarray]]] = {}
        for p, n, T in edges:
            adj.setdefault(p, []).append((n, T))
            adj.setdefault(n, []).append((p, G.se3_inverse(T)))
        seen = {a: np.eye(4)}
        todo = [a]
        while todo:
            cur = todo.pop(0)
            for nxt, T in adj.get(cur, []):
                if nxt not in seen:
                    seen[nxt] = seen[cur] @ T
                    if nxt == b:
                        return seen[nxt]
                    todo.append(nxt)
        return None


def load_mapping(path: str | os.PathLike) -> RecordMapping:
    raw = pathlib.Path(path).read_bytes()
    text = raw.decode("utf-8")
    if str(path).endswith(".json"):
        doc = json.loads(text)
    else:
        import yaml

        doc = yaml.safe_load(text)
    return parse_mapping(doc, sha256=hashlib.sha256(raw).hexdigest())


def parse_mapping(doc: Any, *, sha256: str | None = None) -> RecordMapping:
    if not isinstance(doc, dict) or doc.get("schema_version") not in MAPPING_VERSIONS:
        raise RecordMappingError(f"record mapping: schema_version must be one of {', '.join(MAPPING_VERSIONS)}")
    rec = doc.get("record")
    sources: dict[str, SourceSpec] = {}
    frames: dict[str, tuple[str, np.ndarray]] = {}
    if rec is not None:
        if not isinstance(rec, dict):
            raise RecordMappingError("record mapping: record must be an object")
        for kind in SOURCES:
            if rec.get(kind) is not None:
                sources[kind] = _source(kind, rec[kind], f"record.{kind}")
        frames = _frames(rec.get("frames") or {})
        if not sources:
            raise RecordMappingError("record mapping: record needs pose, joints or both")
    elif isinstance(doc.get("eef"), dict):                  # an exporter mapping: its pose column is the record
        e = doc["eef"]
        sources["pose"] = _source("pose", {"key": e.get("pose_key"), "slice": e.get("slice"),
                                           "quaternion_key": e.get("quaternion_key"), "layout": e.get("layout"),
                                           "units": e.get("units"), "frame_id": e.get("frame_id"),
                                           "reference_frame": e.get("reference_frame"),
                                           "pose_type": e.get("pose_type")}, "eef")
    else:
        raise RecordMappingError("record mapping: record (or an exporter's eef block) is required")
    digest = sha256 or hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    return RecordMapping(sources, frames, digest)


def _source(kind: str, d: Any, where: str) -> SourceSpec:
    if not isinstance(d, dict):
        raise RecordMappingError(f"record mapping: {where} must be an object")
    key, topic = d.get("key") or None, d.get("topic") or None
    if bool(key) == bool(topic):
        raise RecordMappingError(f"record mapping: {where} needs key (a LeRobot column) or topic (an mcap topic), "
                                 "exactly one")
    sl = d.get("slice")
    if sl is not None and not (isinstance(sl, (list, tuple)) and len(sl) == 2 and all(isinstance(x, int) for x in sl)
                               and 0 <= sl[0] < sl[1]):
        raise RecordMappingError(f"record mapping: {where}.slice must be [start, end) with 0 <= start < end")
    ref = d.get("reference_frame")
    if not ref:
        raise RecordMappingError(f"record mapping: {where}.reference_frame is required")
    common = dict(kind=kind, key=key, topic=topic, fields=d.get("fields") or None,
                  slice=tuple(sl) if sl else None, reference_frame=str(ref))
    if kind == "pose":
        layout = d.get("layout")
        if layout not in LAYOUTS:
            raise RecordMappingError(f"record mapping: {where}.layout must be one of {', '.join(LAYOUTS)} "
                                     "(never guessed from widths)")
        units = d.get("units") or {}
        if units.get("position") not in ("m", "mm"):
            raise RecordMappingError(f"record mapping: {where}.units.position must be m or mm")
        angle = units.get("angle", "rad" if layout != "xyz_rpy_xyz_extrinsic" else None)
        if angle not in ("rad", "deg"):
            raise RecordMappingError(f"record mapping: {where}.units.angle must be rad or deg")
        if not d.get("frame_id"):
            raise RecordMappingError(f"record mapping: {where}.frame_id is required")
        if (d.get("pose_type") or "absolute") != "absolute":
            raise RecordMappingError(f"record mapping: {where}.pose_type must be absolute")
        qk = d.get("quaternion_key") or None
        if qk and (not key or not layout.startswith("xyz_quat")):
            raise RecordMappingError(f"record mapping: {where}.quaternion_key goes with key and an xyz_quat layout")
        return SourceSpec(**common, layout=layout, quaternion_key=qk,
                          position_scale=1e-3 if units["position"] == "mm" else 1.0, angle_unit=angle,
                          frame_id=str(d["frame_id"]), robot=None)
    units = d.get("units")
    if units not in ("rad", "deg"):
        raise RecordMappingError(f"record mapping: {where}.units must be rad or deg")
    robot = d.get("robot")
    if robot not in RB.ROBOTS:
        raise RobotModelUnknown(f"record mapping: {where}.robot {robot!r} is not built in (known: {', '.join(RB.ROBOTS)})")
    return SourceSpec(**common, layout=None, quaternion_key=None, position_scale=1.0, angle_unit=units,
                      frame_id=RB.get(robot).tip_frame, robot=robot)


def _frames(d: Any) -> dict[str, tuple[str, np.ndarray]]:
    from scipy.spatial.transform import Rotation

    if not isinstance(d, dict):
        raise RecordMappingError("record mapping: record.frames must be an object")
    out = {}
    for name, f in d.items():
        where = f"record.frames.{name}"
        if not isinstance(f, dict) or not f.get("parent"):
            raise RecordMappingError(f"record mapping: {where}.parent is required")
        xyz, rpy = f.get("xyz_m", [0, 0, 0]), f.get("rpy_deg", [0, 0, 0])
        if not (len(xyz) == 3 and len(rpy) == 3):
            raise RecordMappingError(f"record mapping: {where}.xyz_m and rpy_deg take three numbers")
        out[str(name)] = (str(f["parent"]), G.se3(Rotation.from_euler("xyz", rpy, degrees=True).as_matrix(),
                                                  np.asarray(xyz, float)))
    return out


# ------------------------------------------------------------------------------------ reading


@dataclasses.dataclass
class Series:
    """One source's record of an episode."""

    t: np.ndarray | None               # (M,) seconds from the episode's (or the topic's) start
    index: np.ndarray                  # (M,) row number / message ordinal, what source_state_index names
    T: np.ndarray                      # (M, 4, 4) pose of spec.frame_id in spec.reference_frame


def _poses(spec: SourceSpec, vec: np.ndarray, quat: np.ndarray | None = None) -> np.ndarray:
    if spec.slice is not None:
        if vec.shape[1] < spec.slice[1]:
            raise RecordDataError(f"{spec.key or spec.topic}: {vec.shape[1]} values per frame, slice needs {spec.slice[1]}")
        vec = vec[:, spec.slice[0]:spec.slice[1]]
    if spec.kind == "joints":
        q = np.radians(vec) if spec.angle_unit == "deg" else vec
        try:
            return RB.get(spec.robot).fk(q)
        except ValueError as exc:
            raise RecordDataError(str(exc)) from exc
    from scipy.spatial.transform import Rotation

    need = {"xyz_rpy_xyz_extrinsic": 6, "xyz_quat_xyzw": 7, "xyz_quat_wxyz": 7, "xyz_rotmat": 12}[spec.layout]
    if quat is None and vec.shape[1] != need:
        raise RecordDataError(f"{spec.key or spec.topic}: {vec.shape[1]} values per frame, {spec.layout} takes {need}")
    rot = quat if quat is not None else vec[:, 3:]
    if spec.layout == "xyz_rpy_xyz_extrinsic":
        R = Rotation.from_euler("xyz", rot, degrees=spec.angle_unit == "deg").as_matrix()
    elif spec.layout == "xyz_quat_xyzw":
        R = Rotation.from_quat(rot).as_matrix()
    elif spec.layout == "xyz_quat_wxyz":
        R = Rotation.from_quat(rot[:, [1, 2, 3, 0]]).as_matrix()
    else:
        R = rot.reshape(-1, 3, 3)
    T = np.tile(np.eye(4), (len(vec), 1, 1))
    T[:, :3, :3] = R
    T[:, :3, 3] = vec[:, :3] * spec.position_scale
    return T


class LeRobotRecords:
    """Columns of a local LeRobot v2.1 / v3.0 dataset (meta/ read once per call)."""

    def __init__(self, root: str | os.PathLike):
        self.root = str(root)
        self._lr = None

    def columns(self) -> set[str] | None:
        info = pathlib.Path(self.root) / "meta" / "info.json"
        try:
            return set(json.loads(info.read_text(encoding="utf-8")).get("features") or {})
        except (OSError, ValueError):
            return None

    def read(self, sample, specs: Iterable[SourceSpec]) -> dict[str, Series]:
        from .adapters.lerobot_mapping import LeRobot, MappingError, _column

        specs = list(specs)
        if self._lr is None:
            self._lr = LeRobot(self.root)
        ep = int(sample.episode_index)
        if ep not in self._lr.episodes:
            raise RecordDataError(f"episode {ep} is not in the dataset")
        cols = list(dict.fromkeys(c for s in specs for c in (s.key, s.quaternion_key) if c))
        try:
            df = self._lr.rows(ep, cols)
        except MappingError as exc:
            raise RecordDataError(str(exc)) from exc
        t = df["timestamp"].to_numpy(float) if "timestamp" in df else None
        index = df["frame_index"].to_numpy(int)
        out = {}
        for s in specs:
            vec = _column(df, s.key)
            quat = _column(df, s.quaternion_key) if s.quaternion_key else None
            out[s.kind] = Series(t, index, _poses(s, vec, quat))
        return out


class McapRecords:
    """Topics of a local mcap dataset: an episode's file is the one its views name (F5.13), else the
    dataset's numbering (``episode_<N>.mcap``, or the sorted files)."""

    def __init__(self, root: str | os.PathLike, numbering: dict[int, str] | None = None):
        self.root = str(root)
        self.numbering = numbering

    def file(self, sample) -> str:
        for cam in sample.cameras.values():
            if cam.media.get("topic"):
                return os.path.join(self.root, cam.media["uri"])
        numbering = self.numbering
        if numbering is None:
            from ...cli import containers

            numbering = self.numbering = containers.mcap_episodes(
                [n for n in os.listdir(self.root) if n.endswith(".mcap")])
        name = numbering.get(int(sample.episode_index))
        if name is None:
            raise RecordDataError(f"episode {sample.episode_index}: no .mcap file for it in the dataset")
        return os.path.join(self.root, name)

    def read(self, sample, specs: Iterable[SourceSpec]) -> dict[str, Series]:
        from ...ingest import mcap_reader as MR

        specs = list(specs)
        path = self.file(sample)
        by_topic: dict[str, list[SourceSpec]] = {}
        for s in specs:
            by_topic.setdefault(s.topic, []).append(s)
        rows: dict[str, list[tuple[int, np.ndarray]]] = {s.kind: [] for s in specs}
        factories: dict = {}
        decoders: dict = {}
        make_reader = MR._mcap_reader_mod()
        with open(path, "rb") as fh:
            for schema, channel, message in make_reader(fh).iter_messages(topics=list(by_topic), log_time_order=True):
                decoded = MR._decode(channel, schema, message, factories, decoders)
                for s in by_topic.get(channel.topic, []):
                    vec = MR._extract_source(decoded, s.fields)
                    if vec is not None:
                        rows[s.kind].append((int(message.log_time), np.asarray(vec, float)))
        out = {}
        for s in specs:
            got = rows[s.kind]
            if not got:
                raise RecordDataError(f"{os.path.basename(path)}: topic {s.topic} has no {s.fields or 'numeric'} values")
            widths = {len(v) for _, v in got}
            if len(widths) != 1:
                raise RecordDataError(f"{s.topic}: messages carry {sorted(widths)} values; one width expected")
            times = np.array([t for t, _ in got], float)
            vec = np.stack([v for _, v in got])
            out[s.kind] = Series((times - times[0]) / 1e9, np.arange(len(got)), _poses(s, vec))
        return out


# ------------------------------------------------------------------------------------ comparing


def _upload(sample) -> tuple[np.ndarray, np.ndarray, str] | None:
    """(poses (N,4,4), valid mask, absolute | relative) of the upload, None without a 3D pose."""
    if sample.pose_type == "absolute" and sample.has_absolute_pose:
        T = sample.T_reference_eef
        return T, sample.eef_mask & np.isfinite(T[:, 0, 0]), "absolute"
    if sample.pose_type == "relative_to_start" and sample.eef_mask.any():
        T = sample.T_declared
        return T, sample.eef_mask & np.isfinite(T[:, 0, 0]), "relative"
    return None


def _timeline(sample, series: Series) -> tuple[np.ndarray, float]:
    """The upload's timeline for segments and lags, and its frame rate."""
    if sample.t is not None and np.isfinite(sample.t).sum() >= 2:
        t = np.asarray(sample.t, float)
    elif series.t is not None and len(series.t) == sample.n_frames:
        t = np.asarray(series.t, float)
    else:
        t = np.arange(sample.n_frames) / 15.0
    step = TL.median_step(t)
    return t, (1.0 / step if np.isfinite(step) and step > 0 else 15.0)


def align(sample, series: Series) -> tuple[np.ndarray, str] | None:
    """The record at every upload frame (NaN where it has none) and how the frames were paired."""
    n = sample.n_frames
    out = np.full((n, 4, 4), np.nan)
    ssi = np.asarray(sample.source_state_index)
    if (ssi >= 0).any():
        at = {int(i): k for k, i in enumerate(series.index)}
        for f in np.flatnonzero(ssi >= 0):
            k = at.get(int(ssi[f]))
            if k is not None:
                out[f] = series.T[k]
        return out, "source_state_index"
    if len(series.index) == n:
        return series.T.copy(), "frame_index"
    if sample.t is not None and series.t is not None:
        t = np.asarray(sample.t, float)
        pos = TL.interp_track(series.t, series.T[:, :3, 3], t, gap_factor=4.0)
        rot = TL.slerp_rotations(np.asarray(series.t, float), series.T[:, :3, :3], t)
        good = np.isfinite(pos).all(1) & np.isfinite(rot).all(axis=(1, 2))
        out[good, :3, :3] = rot[good]
        out[good, :3, 3] = pos[good]
        out[good, 3, :] = [0, 0, 0, 1]
        return out, "timestamp"
    return None


def fit_tool_side(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Constant X with ``B ~ A X``: rotation the chordal mean of ``A_R^T B_R``, translation the mean of
    ``A_R^T (B_t - A_t)``."""
    from scipy.spatial.transform import Rotation

    Rs = np.einsum("nji,njk->nik", A[:, :3, :3], B[:, :3, :3])
    Rx = Rotation.from_matrix(Rs).mean().as_matrix()
    tx = np.einsum("nji,nj->ni", A[:, :3, :3], B[:, :3, 3] - A[:, :3, 3]).mean(axis=0)
    return G.se3(Rx, tx)


def _fit_shifted(t: np.ndarray, A: np.ndarray, B: np.ndarray, valid: np.ndarray, lag_s: float) -> np.ndarray | None:
    """The tool-side constant with the record taken at ``t + lag`` (positions linear, rotations SLERP)."""
    tq = np.asarray(t, float) + lag_s
    ok = valid & np.isfinite(A[:, 0, 0])
    if ok.sum() < 3:
        return None
    pos = TL.interp_track(t[ok], A[ok, :3, 3], tq)
    rot = TL.slerp_rotations(np.asarray(t, float)[ok], A[ok, :3, :3], tq)
    good = valid & np.isfinite(pos).all(1) & np.isfinite(rot).all(axis=(1, 2))
    if good.sum() < 3:
        return None
    return fit_tool_side(G.se3(rot[good], pos[good]), B[good])


def _residual(P: np.ndarray, B: np.ndarray, valid: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Per frame: position mm and rotation deg between P and B; NaN outside ``valid``."""
    valid = np.ones(len(P), bool) if valid is None else valid
    pos = np.full(len(P), np.nan)
    rot = np.full(len(P), np.nan)
    Pv, Bv = P[valid], B[valid]
    pos[valid] = np.linalg.norm(Pv[:, :3, 3] - Bv[:, :3, 3], axis=1) * 1000.0
    rot[valid] = G.rotation_angle_deg(np.einsum("nji,njk->nik", Pv[:, :3, :3], Bv[:, :3, :3]))
    return pos, rot


def _describe(T: np.ndarray) -> dict:
    rv = G.rotation_log(T[:3, :3])
    ang = float(np.degrees(np.linalg.norm(rv)))
    axis = (rv / np.linalg.norm(rv)).tolist() if ang > 1e-6 else [0.0, 0.0, 1.0]
    return {"translation_mm": [round(float(x) * 1000.0, 2) for x in T[:3, 3]],
            "translation_norm_mm": round(float(np.linalg.norm(T[:3, 3])) * 1000.0, 2),
            "rotation_deg": round(ang, 3), "rotation_axis": [round(float(x), 3) for x in axis]}


def _stats(x: np.ndarray) -> dict | None:
    x = x[np.isfinite(x)]
    if not len(x):
        return None
    return {"median": round(float(np.median(x)), 3), "p95": round(float(np.percentile(x, 95)), 3),
            "max": round(float(x.max()), 3)}


def _thresholds(profile) -> dict:
    sec = getattr(profile, "record", None) or {}
    return {**DEFAULTS, **{k: v for k, v in sec.items() if k in DEFAULTS}}


def _curves(t: np.ndarray, valid: np.ndarray, series: dict[str, np.ndarray]) -> dict:
    idx = np.flatnonzero(valid)
    if len(idx) > CURVE_POINTS:
        idx = idx[np.linspace(0, len(idx) - 1, CURVE_POINTS).round().astype(int)]
    return {"frame": idx.tolist(), "t_s": [round(float(t[i]), 3) for i in idx],
            **{k: [None if not np.isfinite(v[i]) else round(float(v[i]), 3) for i in idx] for k, v in series.items()}}


def compare_source(sample, spec: SourceSpec, series: Series, mapping: RecordMapping, profile, *,
                   lag_search_s: tuple[float, float] = (-1.0, 1.0)) -> dict:
    """One source against the upload; ``_pred`` (the record as the upload's pose, N x 4 x 4) rides along for
    the overlay evidence and is dropped before the record goes into a result."""
    th = _thresholds(profile)
    base = {"source": spec.as_dict()}
    up = _upload(sample)
    if up is None:
        return {**base, "status": C.UNSUPPORTED, "reasons": [C.UPLOAD_POSE_MISSING]}
    T_up, up_valid, mode = up
    paired = align(sample, series)
    if paired is None:
        return {**base, "status": C.UNKNOWN, "reasons": [C.CLOCK_ALIGNMENT_UNKNOWN]}
    T_rec, how = paired
    E = mapping.transform(spec.frame_id, sample.eef_frame or "", spec.robot) if sample.eef_frame else None
    declared = E is not None
    if mode == "absolute":
        W = mapping.transform(sample.reference_frame or "", spec.reference_frame, spec.robot) \
            if sample.reference_frame else None
        if W is None:
            return {**base, "status": C.UNSUPPORTED, "reasons": [C.REFERENCE_FRAMES_UNRELATED],
                    "frames": {"upload": sample.reference_frame, "record": spec.reference_frame}}
        A = W @ T_rec
    valid = up_valid & np.isfinite(T_rec[:, 0, 0])
    n_valid, n_up = int(valid.sum()), int(up_valid.sum())
    out = {**base, "alignment": how, "frames_compared": n_valid, "frames_with_pose": n_up,
           "frame_ids": {"upload": sample.eef_frame, "record": spec.frame_id}}
    if n_valid < th["min_frames"] or n_valid < th["min_fraction"] * max(n_up, 1):
        return {**out, "status": C.UNKNOWN, "reasons": [C.COVERAGE_INSUFFICIENT]}
    if mode == "relative":
        k = int(np.flatnonzero(valid)[0])
        A = np.einsum("ij,njk->nik", G.se3_inverse(T_rec[k]), T_rec)
        B = np.einsum("ij,njk->nik", G.se3_inverse(T_up[k]), T_up)
        Ek = E if declared else np.eye(4)
        P = np.einsum("ij,njk,kl->nil", G.se3_inverse(Ek), A, Ek)
        fitted, fit_P = None, P
    else:
        B = T_up
        fitted = fit_tool_side(A[valid], B[valid])
        P = A @ (E if declared else fitted)
        fit_P = A @ fitted
    raw_pos, raw_rot = _residual(P, B, valid)
    fit_pos, fit_rot = _residual(fit_P, B, valid)
    t, fps = _timeline(sample, series)
    reasons: list[str] = []
    notes: list[str] = []
    lag = MX.lag_scan(t, [P[:, :3, 3]], [np.where(valid[:, None], B[:, :3, 3], np.nan)], fps=fps,
                      search_s=lag_search_s)
    time_offset = None
    shifted_fit = None
    if lag is not None:
        time_offset = {"lag_s": round(float(lag["lag_s"]), 4), "lag_frames": round(float(lag["lag_frames"]), 2),
                       "improvement": round(float(lag["improvement"]), 2) if np.isfinite(lag["improvement"]) else None,
                       "edge_at_limit": bool(lag["edge_at_limit"])}
        # 0.05 frame of slack: the sub-frame refinement puts an exact one-frame lag at 0.999
        if abs(lag["lag_frames"]) + 0.05 >= th["lag_min_frames"] and lag["improvement"] >= th["lag_min_improvement"] \
                and not lag["edge_at_limit"]:
            notes.append(C.TIME_OFFSET)
            if fitted is not None:           # a time offset biases the constant: judge it on the shifted record
                shifted_fit = _fit_shifted(t, A, B, valid, float(lag["lag_s"]))
    deviation = None
    if declared and fitted is not None:
        deviation = _describe(G.se3_inverse(E) @ (shifted_fit if shifted_fit is not None else fitted))
        if deviation["translation_norm_mm"] > th["constant_mm"] or deviation["rotation_deg"] > th["constant_deg"]:
            reasons.append(C.CONSTANT_MISMATCH)
    elif not declared:
        notes.append(C.CONSTANT_UNCHECKED)
    w = int(th["rolling_median_frames"])
    segs = []
    for values, on, off, what in ((SG.rolling_median(fit_pos, w), th["pos_on_mm"], th["pos_off_mm"], "position"),
                                  (SG.rolling_median(fit_rot, w), th["rot_on_deg"], th["rot_off_deg"], "rotation")):
        for s in SG.hysteresis(values, t, on=on, off=off, min_duration_s=th["min_duration_s"],
                               max_gap_s=th["max_gap_s"], reason=C.RECORD_DEVIATION):
            segs.append(s.as_dict(aspect=what, evidence_frames=SG.worst_frames(values, s)))
    if segs:
        reasons.append(C.RECORD_DEVIATION)
    return {**out, "status": C.SUSPECT if reasons else C.OK, "reasons": sorted(reasons), "notes": notes,
            "relation": {"declared": declared, "expected": _describe(E) if declared else None,
                         "fitted": _describe(fitted) if fitted is not None else None,
                         "fitted_after_time_offset": _describe(shifted_fit) if shifted_fit is not None else None,
                         "deviation": deviation},
            "residual": {"position_mm": _stats(raw_pos), "rotation_deg": _stats(raw_rot)},
            "residual_after_constant": {"position_mm": _stats(fit_pos), "rotation_deg": _stats(fit_rot)},
            "segments": segs, "time_offset": time_offset,
            "curves": _curves(t, valid, {"position_mm": raw_pos, "rotation_deg": raw_rot,
                                         "position_after_constant_mm": fit_pos,
                                         "rotation_after_constant_deg": fit_rot}),
            "_pred": P}


def compare_internal(sample, mapping: RecordMapping, got: dict[str, Series], profile) -> dict | None:
    """The dataset's own two records against each other (pose columns vs the joints' kinematics): a note."""
    if not ({"pose", "joints"} <= set(got)):
        return None
    th = _thresholds(profile)
    ps, js = mapping.sources["pose"], mapping.sources["joints"]
    if len(got["pose"].index) != len(got["joints"].index):
        return {"compared": False, "reason": "different_lengths"}
    E = mapping.transform(js.frame_id, ps.frame_id, js.robot)
    W = mapping.transform(ps.reference_frame, js.reference_frame, js.robot)
    A = got["joints"].T if W is None else W @ got["joints"].T
    B = got["pose"].T
    ok = np.isfinite(A[:, 0, 0]) & np.isfinite(B[:, 0, 0])
    if ok.sum() < 2:
        return {"compared": False, "reason": "no_common_frames"}
    fitted = fit_tool_side(A[ok], B[ok])
    pos, rot = _residual(A @ (E if E is not None else fitted), B, ok)
    consistent = bool(np.nanpercentile(pos, 95) <= th["pos_on_mm"] and np.nanpercentile(rot, 95) <= th["rot_on_deg"])
    return {"compared": True, "consistent": consistent, "relation_declared": E is not None,
            "position_mm": _stats(pos), "rotation_deg": _stats(rot)}


def compare_episode(sample, mapping: RecordMapping | None, reader, profile, *,
                    lag_search_s: tuple[float, float] = (-1.0, 1.0)) -> dict:
    """``details.record`` of one episode (design 12 §8.7)."""
    if mapping is None:
        return {"status": C.UNSUPPORTED, "reasons": [C.RECORD_MAPPING_MISSING], "sources": {}}
    if _upload(sample) is None:
        return {"status": C.UNSUPPORTED, "reasons": [C.UPLOAD_POSE_MISSING], "sources": {}}
    try:
        got = reader.read(sample, mapping.sources.values())
    except RecordDataError as exc:
        return {"status": C.UNSUPPORTED, "reasons": [C.RECORD_COLUMNS_MISSING], "sources": {},
                "message": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - the record is a report: a failure is recorded, never raised
        return {"status": C.ERROR, "reasons": [C.EXECUTION_FAILED], "sources": {},
                "message": f"{type(exc).__name__}: {exc}"[:300]}
    sources = {k: compare_source(sample, mapping.sources[k], got[k], mapping, profile, lag_search_s=lag_search_s)
               for k in SOURCES if k in got}
    status = max((s["status"] for s in sources.values()), key=lambda st: _RANK[st], default=C.UNSUPPORTED)
    reasons = sorted({r for s in sources.values() for r in s.get("reasons", [])})
    return {"status": status, "reasons": reasons, "sources": sources, "mapping_sha256": mapping.sha256,
            "internal": compare_internal(sample, mapping, got, profile)}


#: overlay colours (BGR): the upload red, the record by source
_COLOURS = {"upload": (0, 0, 255), "pose": (255, 128, 0), "joints": (0, 165, 255)}
TRAIL = 15                                       # frames drawn before and after the one shown


def write_evidence(sample, record: dict, *, media_root: str, out_dir: str, mode: str = "flagged",
                   max_frames: int = 3) -> list[dict]:
    """Up to ``max_frames`` frames per camera with a calibration: the upload's TCP (red) and the TCP the
    record gives (blue: pose columns, orange: joints), each with its trail. Only where a source is suspect
    (``mode=flagged``), or always (``all``); nothing with ``off``, for relative poses or without calibration.
    For viewing only - the only place the record comparison decodes video."""
    import cv2

    from . import observations as O

    if mode == "off" or sample.pose_type != "absolute":
        return []
    preds = {k: s["_pred"] for k, s in (record.get("sources") or {}).items()
             if "_pred" in s and (mode == "all" or s.get("status") == C.SUSPECT)}
    if not preds:
        return []
    score = np.zeros(sample.n_frames)            # the largest position gap of any shown source
    picked: set[int] = set()                     # the segments' own worst frames come first
    for k, P in preds.items():
        d = np.linalg.norm(P[:, :3, 3] - sample.T_reference_eef[:, :3, 3], axis=1)
        score = np.maximum(score, np.nan_to_num(d, nan=0.0))
        for seg in record["sources"][k].get("segments") or []:
            picked |= {int(f) for f in seg.get("evidence_frames") or []}
    pool = sorted(picked, key=lambda f: -score[f]) + [int(f) for f in np.argsort(-score) if score[f] > 0]
    frames = _spread(pool, max_frames, max(2 * TRAIL, sample.n_frames // (2 * max(max_frames, 1))))
    point = sample.points.get("tcp")
    offsets = G.point_offsets(point, sample.gripper) if point is not None else np.zeros((sample.n_frames, 3))
    if offsets is None:
        offsets = np.zeros((sample.n_frames, 3))
    out: list[dict] = []
    ep = f"{sample.episode_index:06d}"
    for cid, cam in sample.cameras.items():
        cal = cam.calibration(sample)
        if cal is None or not frames:
            continue
        tracks = {"upload": sample.T_reference_eef, **preds}
        uv = {k: G.project_chain(T, offsets, cam.T_reference_camera, cal["K"], cal["model"],
                                 cal["distortion_coefficients"], cam.H)[0] for k, T in tracks.items()}
        vf = cam.video_frame_index
        want = {int(vf[f]): f for f in frames if vf[f] >= 0}
        if not want:
            continue
        d = pathlib.Path(out_dir) / "evidence" / ep / "record"
        d.mkdir(parents=True, exist_ok=True)
        for fr in O.view_frames(sample, cid, media_root):
            if fr.index not in want:
                continue
            f = want.pop(fr.index)
            img = fr.bgr().copy()
            lo, hi = max(0, f - TRAIL), min(sample.n_frames, f + TRAIL + 1)
            for k, track in uv.items():
                seg = track[lo:hi]
                seg = seg[np.isfinite(seg).all(1)]
                if len(seg) >= 2:
                    cv2.polylines(img, [np.round(seg).astype(np.int32)], False, _COLOURS[k], 2, cv2.LINE_AA)
                if np.isfinite(track[f]).all():
                    cv2.circle(img, (int(round(track[f][0])), int(round(track[f][1]))), 7, _COLOURS[k], 2)
            name = f"{cid}_frame_{f:06d}.jpg"
            cv2.imwrite(str(d / name), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
            out.append({"camera_id": cid, "frame_index": f, "path": f"evidence/{ep}/record/{name}", "kind": "record",
                        "legend": {"upload": "red", **{k: ("blue" if k == "pose" else "orange") for k in preds}}})
            if not want:
                break
    return out


def _spread(order: list[int], k: int, sep: int) -> list[int]:
    """The first ``k`` frames of ``order`` at least ``sep`` frames apart (so the trails do not overlap)."""
    got: list[int] = []
    for f in order:
        if all(abs(f - g) >= sep for g in got):
            got.append(f)
            if len(got) == k:
                break
    return sorted(got)


def strip(record: dict) -> dict:
    """The record without the in-memory arrays (what goes into ``details``)."""
    return {**record, "sources": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                  for k, v in (record.get("sources") or {}).items()}}
