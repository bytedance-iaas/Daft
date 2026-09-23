"""Read and validate ``trajectory.json`` (``trajectory-bundle/1.0`` of ``eef-video/1.0.0`` samples).

Order (design 12 §3.7): strict JSON (no NaN / Infinity) -> evaluation keys anywhere reject the file
-> container Schema -> per sample: sample / calibration / frame Schemas -> cross-section semantics
(format §7) -> media existence -> provided vs recomputed projection (kept apart, never overwritten).

Errors are located to sample / frame / camera / point / JSON path. An error rejects the file;
a warning (e.g. ``input_inconsistent``) is reported and the sample is kept. The parsed samples
carry numpy arrays for the numeric layers; the raw frame objects are not retained.
"""
from __future__ import annotations

import dataclasses
import decimal
import hashlib
import json
import math
import os
import pathlib
from typing import Any, Callable, Iterable

import numpy as np

from . import contracts as C
from . import geometry as G

MAX_ERRORS_PER_SAMPLE = 50
#: Codes of the loader's own findings (Schema errors use ``schema``).
PARSE_ERROR, FORBIDDEN_KEY, SCHEMA, SEMANTIC, MEDIA = (
    "json_invalid", "forbidden_key", "schema", "semantic", "media_missing")
MEDIA_URI_BASES = ("lerobot_root", "bundle_file")
_UNIT_S = {"s": decimal.Decimal(1), "ms": decimal.Decimal("1e-3"), "us": decimal.Decimal("1e-6"),
           "ns": decimal.Decimal("1e-9")}


# --- parsed objects --------------------------------------------------------------------------

@dataclasses.dataclass
class ProjectedTrack:
    """One projected point across the sample's frames (media pixels)."""

    uv: np.ndarray                     # (N, 2), NaN where no pixel
    depth: np.ndarray                  # (N,), NaN where unknown
    status: np.ndarray                 # (N,) object: valid | out_of_frame | behind_camera | unknown | absent


@dataclasses.dataclass
class CameraStream:
    camera_id: str
    view_id: str
    mount: str
    media: dict
    image_size_wh: tuple[int, int]
    video_frame_index: np.ndarray      # (N,) int, -1 = no paired media frame
    video_timestamp_s: np.ndarray      # (N,) float, NaN = no paired media frame
    calibration_id: np.ndarray         # (N,) object, None where absent
    T_reference_camera: np.ndarray     # (N, 4, 4) effective camera pose, NaN where unknown
    H: np.ndarray                      # (N, 3, 3) calibration -> media pixels
    projection_source: str | None      # provided | recomputed | None (no projection at all)
    provided: dict[str, ProjectedTrack]

    def calibration(self, sample: "EefSample") -> dict | None:
        ids = {c for c in self.calibration_id if c is not None}
        if len(ids) != 1 or sample.calibration is None:
            return None
        return sample.calibration["calibrations"].get(next(iter(ids)))


@dataclasses.dataclass
class SourceClock:
    channel: str
    clock_id: str
    unit: str
    semantics: str
    seconds: np.ndarray                # (N,) seconds relative to the clock's first value, NaN missing


@dataclasses.dataclass
class EefSample:
    episode_index: int
    sample_id: str
    sample: dict                       # sample.json object as uploaded (views, points, axes, notes)
    calibration: dict | None
    input_hash: str                    # sha256 of the canonical JSON of this bundle entry
    n_frames: int
    timebase: str
    t: np.ndarray | None               # (N,) main timeline seconds; None for index_only
    eef_frame: str | None
    reference_frame: str | None
    pose_type: str | None              # absolute | relative_to_start | None (no interpreted pose)
    T_reference_eef: np.ndarray        # (N, 4, 4) absolute pose, NaN where not recoverable
    T_declared: np.ndarray             # (N, 4, 4) pose as declared (relative poses unanchored)
    eef_mask: np.ndarray               # (N,) bool, an interpreted pose exists on this frame
    anchor_missing: bool
    gripper: np.ndarray                # (N,) closed_fraction, NaN unknown
    source_state_index: np.ndarray     # (N,) int, -1 unknown
    clocks: dict[str, SourceClock]
    points: dict[str, dict]
    axes: dict[str, dict]
    cameras: dict[str, CameraStream]
    consistency: dict[str, Any]        # provided vs recomputed summary (design 12 §6.1)
    warnings: list[C.Issue]

    @property
    def has_absolute_pose(self) -> bool:
        return bool(np.isfinite(self.T_reference_eef[:, 0, 0]).any())


@dataclasses.dataclass
class LoadResult:
    ok: bool
    sha256: str | None
    meta: dict                          # schema_version, container, dataset, media_uri_base
    samples: dict[int, EefSample]
    issues: list[C.Issue]
    report: dict

    @property
    def errors(self) -> list[C.Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[C.Issue]:
        return [i for i in self.issues if i.severity == "warning"]


class _Collector:
    def __init__(self, **where):
        self.where = where
        self.issues: list[C.Issue] = []
        self.n_errors = 0

    def error(self, code, message, **loc):
        self.n_errors += 1
        if self.n_errors <= MAX_ERRORS_PER_SAMPLE:
            self.issues.append(C.Issue("error", code, message, **{**self.where, **loc}))

    def warn(self, code, message, **loc):
        self.issues.append(C.Issue("warning", code, message, **{**self.where, **loc}))

    def require(self, ok, message, code=SEMANTIC, **loc) -> bool:
        if not ok:
            self.error(code, message, **loc)
        return bool(ok)


# --- entry points ----------------------------------------------------------------------------

def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON number {name}")


def parse_json_bytes(data: bytes) -> Any:
    return json.loads(data, parse_constant=_reject_constant)


def find_forbidden_keys(value: Any, path: str = "") -> Iterable[str]:
    """JSON paths of every evaluation-truth key (design 12 §3.7 FORBIDDEN, D-E5)."""
    stack = [(value, path)]
    while stack:
        node, p = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                q = f"{p}/{k}" if p else k
                if k in C.FORBIDDEN_KEYS:
                    yield q
                stack.append((v, q))
        elif isinstance(node, list):
            stack.extend((v, f"{p}/{i}") for i, v in enumerate(node))


def load_bundle(source: str | os.PathLike | bytes | dict, *, lerobot_root: str | os.PathLike | None = None,
                media_exists: Callable[[str], bool] | None = None, check_media: bool = True,
                episodes: Iterable[int] | None = None) -> LoadResult:
    """Validate a trajectory bundle and parse its samples.

    ``lerobot_root`` resolves ``media_uri_base=lerobot_root`` URIs (the directory holding ``meta/``);
    ``media_exists`` overrides the file-existence probe (remote stores). ``episodes`` limits the
    parsed samples (validation of the container still covers the whole file).
    """
    base_dir = None
    if isinstance(source, dict):
        payload, digest = source, None
    else:
        if isinstance(source, bytes):
            data = source
        else:
            path = pathlib.Path(source)
            data = path.read_bytes()
            base_dir = path.parent
        digest = hashlib.sha256(data).hexdigest()
        try:
            payload = parse_json_bytes(data)
        except ValueError as exc:
            issue = C.Issue("error", PARSE_ERROR, f"not strict JSON: {exc}")
            return LoadResult(False, digest, {}, {}, [issue], _report([], [issue], digest))
    issues: list[C.Issue] = []
    leaked = list(find_forbidden_keys(payload))
    if leaked:
        issues += [C.Issue("error", FORBIDDEN_KEY, f"evaluation field is not allowed in detector input: {p}",
                           path=p) for p in leaked[:MAX_ERRORS_PER_SAMPLE]]
        return LoadResult(False, digest, {}, {}, issues, _report([], issues, digest))
    from curation.contracts import schemas

    container_errors = schemas.errors("eef/trajectory_bundle.schema.json", payload)
    if container_errors:
        issues += [C.Issue("error", SCHEMA, e) for e in container_errors[:MAX_ERRORS_PER_SAMPLE]]
        return LoadResult(False, digest, {}, {}, issues, _report([], issues, digest))
    meta = {k: payload[k] for k in ("schema_version", "container", "dataset", "media_uri_base")}
    uri_base = payload["media_uri_base"]
    if uri_base not in MEDIA_URI_BASES:
        issue = C.Issue("error", SEMANTIC, f"media_uri_base={uri_base!r} is not accepted (DEMO: lerobot_root)",
                        path="media_uri_base")
        return LoadResult(False, digest, meta, {}, [issue], _report([], [issue], digest))
    media_root = pathlib.Path(lerobot_root) if uri_base == "lerobot_root" and lerobot_root else base_dir
    if media_exists is None and check_media:
        if media_root is None:
            check_media = False
            issues.append(C.Issue("warning", MEDIA, "media root unknown; media existence not checked"))
        else:
            media_exists = lambda uri: (media_root / uri).is_file()  # noqa: E731
    wanted = None if episodes is None else set(episodes)
    samples: dict[int, EefSample] = {}
    summaries = []
    seen_ep, seen_id = set(), set()
    for i, entry in enumerate(payload["samples"]):
        ep = entry["episode_index"]
        col = _Collector(episode_index=ep, sample_id=entry["sample"].get("sample_id"))
        col.require(ep not in seen_ep, f"duplicate episode_index {ep}", path=f"samples/{i}/episode_index")
        col.require(entry["sample"].get("sample_id") not in seen_id, "duplicate sample_id",
                    path=f"samples/{i}/sample/sample_id")
        seen_ep.add(ep)
        seen_id.add(entry["sample"].get("sample_id"))
        if wanted is not None and ep not in wanted:
            issues += col.issues
            continue
        parsed, summary = _parse_sample(entry, i, col, media_exists if check_media else None)
        issues += col.issues
        summaries.append(summary)
        if parsed is not None and col.n_errors == 0:
            samples[ep] = parsed
    ok = not any(x.severity == "error" for x in issues)
    return LoadResult(ok, digest, meta, samples if ok else {}, issues, _report(summaries, issues, digest))


def _report(summaries: list[dict], issues: list[C.Issue], digest: str | None) -> dict:
    errs = [i for i in issues if i.severity == "error"]
    diffs = [s["max_reprojection_difference_px"] for s in summaries
             if s.get("max_reprojection_difference_px") is not None]
    return {"schema_version": "eef-validation/1.0", "sha256": digest, "valid": not errs,
            "errors": [i.as_dict() for i in errs],
            "warnings": [i.as_dict() for i in issues if i.severity == "warning"][:200],
            "samples": summaries,
            "total_frames": sum(s.get("frames", 0) for s in summaries),
            "total_points_checked": sum(s.get("projection_points_checked", 0) for s in summaries),
            "max_reprojection_difference_px": max(diffs) if diffs else None}


# --- one sample ------------------------------------------------------------------------------

def _canonical_hash(entry: dict) -> str:
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _media_uri_ok(uri: str) -> bool:
    if "://" in uri or uri.startswith(("/", "\\")) or (len(uri) > 1 and uri[1] == ":"):
        return False
    return ".." not in pathlib.PurePosixPath(uri).parts


def _parse_sample(entry: dict, idx: int, col: _Collector,
                  media_exists: Callable[[str], bool] | None) -> tuple[EefSample | None, dict]:
    from curation.contracts import schemas

    ep = entry["episode_index"]
    s, cal, rows = entry["sample"], entry["calibration"], entry["frames"]
    where = f"samples/{idx}"
    summary = {"episode_index": ep, "sample_id": s.get("sample_id"), "frames": len(rows)}
    for e in schemas.errors("eef/sample.schema.json", s):
        col.error(SCHEMA, e, path=f"{where}/sample")
    if cal is not None:
        for e in schemas.errors("eef/calibration.schema.json", cal):
            col.error(SCHEMA, e, path=f"{where}/calibration")
    if col.n_errors:
        return None, summary
    frame_validator = schemas.validator("eef/frame.schema.json")
    for j, r in enumerate(rows):
        for e in frame_validator.iter_errors(r):
            loc = "/".join(map(str, e.absolute_path)) or "<root>"
            col.error(SCHEMA, f"{loc}: {e.message}", frame_index=j, path=f"{where}/frames/{j}")
    if col.n_errors:
        return None, summary

    sid = s["sample_id"]
    col.require(s["annotations_path"] == "#frames", "bundle samples must use annotations_path='#frames'",
                path=f"{where}/sample/annotations_path")
    col.require((cal is None) == (s["calibration_path"] is None)
                and (cal is None or s["calibration_path"] == "#calibration"),
                "calibration_path must be '#calibration' with a calibration object, or null with null",
                path=f"{where}/sample/calibration_path")
    col.require(s["source"]["episode_id"] == str(ep), "source.episode_id must equal str(episode_index)",
                path=f"{where}/sample/source/episode_id")
    n = s["frame_count"]
    col.require(len(rows) == n, f"frame_count={n} but {len(rows)} frames", path=f"{where}/frames")

    views = s["views"]
    col.require(len({v["view_id"] for v in views}) == len(views), "duplicate view_id", path=f"{where}/sample/views")
    cams = {v["camera_id"]: v for v in views if v["kind"] == "camera"}
    col.require(len(cams) == sum(v["kind"] == "camera" for v in views), "duplicate camera_id",
                path=f"{where}/sample/views")
    for k, v in enumerate(views):
        uri = v["media"]["uri"]
        if not col.require(_media_uri_ok(uri), f"media uri must be relative without '..': {uri}",
                           path=f"{where}/sample/views/{k}/media/uri"):
            continue
        if media_exists is not None:
            col.require(media_exists(uri), f"media file not found: {uri}", code=MEDIA,
                        path=f"{where}/sample/views/{k}/media/uri", camera_id=v.get("camera_id"))
        m = v["media"]
        if m["kind"] == "video" and m["clip_end_s"] is not None:
            col.require(m["clip_end_s"] > m["clip_start_s"], "clip_end_s must exceed clip_start_s",
                        path=f"{where}/sample/views/{k}/media")

    raw_seq = s["raw_pose_sequence"]
    if raw_seq is not None and media_exists is not None:
        col.require(_media_uri_ok(raw_seq["path"]) and media_exists(raw_seq["path"]),
                    f"raw_pose_sequence file not found: {raw_seq['path']}", code=MEDIA,
                    path=f"{where}/sample/raw_pose_sequence/path")

    points, axes = s["point_definitions"], s["axis_definitions"]
    for pid, p in points.items():
        if p["model"] != "external_2d":
            col.require(p["frame_id"] == s["eef_frame"], f"point {pid} is not defined in eef_frame",
                        point_id=pid, path=f"{where}/sample/point_definitions/{pid}")
    for aid, a in axes.items():
        if not col.require(a["start_point_id"] in points and a["end_point_id"] in points,
                           f"axis {aid} references an unknown point", path=f"{where}/sample/axis_definitions/{aid}"):
            continue
        p0, p1 = points[a["start_point_id"]], points[a["end_point_id"]]
        if a["length_m"] is not None and p0["model"] == p1["model"] == "fixed":
            length = float(np.linalg.norm(np.subtract(p1["position_eef_m"], p0["position_eef_m"])))
            col.require(abs(length - a["length_m"]) < 1e-8, f"axis {aid} length_m={a['length_m']} but points "
                        f"are {length:.6f} m apart", path=f"{where}/sample/axis_definitions/{aid}")

    calibs = {}
    if cal is not None:
        calibs = cal["calibrations"]
        for cid, c in calibs.items():
            cw = f"{where}/calibration/calibrations/{cid}"
            col.require(c["camera_id"] in cams, f"calibration {cid} names a camera without a view", path=cw)
            col.require(c["reference_frame"] == s["reference_frame"],
                        f"calibration {cid} reference_frame differs from the sample's", path=cw)
            K = np.asarray(c["K"], float)
            col.require(K[0, 0] > 0 and K[1, 1] > 0 and np.allclose(K[2], [0, 0, 1]) and K[1, 0] == 0,
                        f"calibration {cid}: invalid intrinsics K", path=f"{cw}/K")
            if c["T_reference_camera"] is not None:
                col.require(G.is_rigid(c["T_reference_camera"]),
                            f"calibration {cid}: T_reference_camera is not a rigid transform",
                            path=f"{cw}/T_reference_camera")
            if cams.get(c["camera_id"]):
                col.require(list(c["image_size_wh"]) == list(cams[c["camera_id"]]["media"]["image_size_wh"])
                            or True, "")  # calibration resolution may differ; H maps it (format §5)
    if col.n_errors:
        return None, summary

    # --- frames -------------------------------------------------------------------------------
    eye4 = np.full((4, 4), np.nan)
    T_decl = np.repeat(eye4[None], n, 0)
    T_abs = np.repeat(eye4[None], n, 0)
    eef_mask = np.zeros(n, bool)
    grip = np.full(n, np.nan)
    ssi = np.full(n, -1, int)
    t = np.full(n, np.nan)
    pose_types = set()
    anchor_missing = False
    clocks_raw: dict[str, dict] = {}
    cam_data = {cid: {"vf": np.full(n, -1, int), "vt": np.full(n, np.nan), "cid": np.full(n, None, object),
                      "T": np.repeat(eye4[None], n, 0), "H": np.repeat(np.full((3, 3), np.nan)[None], n, 0),
                      "src": set(), "pts": {}} for cid in cams}
    prev_t = -math.inf
    last_vf = {c: -1 for c in cams}
    last_vt = {c: -math.inf for c in cams}
    for i, r in enumerate(rows[:n]):
        fw = f"{where}/frames/{i}"
        loc = {"frame_index": i}
        col.require(r["sample_id"] == sid and r["frame_index"] == i,
                    f"frame {i}: sample_id/frame_index mismatch", path=fw, **loc)
        ts = r["timestamp_s"]
        if s["timebase"] == "video_pts":
            if col.require(ts is not None and ts > prev_t, f"frame {i}: video_pts timeline must strictly increase",
                           path=f"{fw}/timestamp_s", **loc):
                prev_t = ts
                t[i] = ts
        else:
            col.require(ts is None, f"frame {i}: index_only samples cannot carry timestamps",
                        path=f"{fw}/timestamp_s", **loc)
        if r["source_state_index"] is not None:
            ssi[i] = r["source_state_index"]
        for st in r["source_timing"]:
            ch = clocks_raw.setdefault(st["channel"], {"clock_id": st["clock_id"], "unit": st["unit"],
                                                        "semantics": st["semantics"], "values": [None] * n})
            if ch["clock_id"] != st["clock_id"] or ch["unit"] != st["unit"]:
                col.warn(C.CLOCK_ALIGNMENT_UNKNOWN, f"channel {st['channel']} changes clock or unit",
                         path=f"{fw}/source_timing", **loc)
            ch["values"][i] = decimal.Decimal(st["timestamp"]) * _UNIT_S[st["unit"]]
        p = r["eef"]
        if p is not None:
            pose_types.add(p["pose_type"])
            q = np.asarray(p["quaternion_xyzw"], float)
            qn = float(np.linalg.norm(q))
            if not col.require(abs(qn - 1.0) < 1e-5, f"frame {i}: quaternion must be unit length (|q|={qn:.6g})",
                               path=f"{fw}/eef/quaternion_xyzw", **loc):
                continue
            col.require(p["frame_id"] == s["eef_frame"], f"frame {i}: eef.frame_id differs from eef_frame",
                        path=f"{fw}/eef/frame_id", **loc)
            T = G.pose_matrix(p["position_m"], q)
            T_decl[i] = T
            eef_mask[i] = True
            if p["pose_type"] == "absolute":
                col.require(p["reference_frame"] == s["reference_frame"],
                            f"frame {i}: absolute pose reference_frame differs from the sample's",
                            path=f"{fw}/eef/reference_frame", **loc)
                T_abs[i] = T
            else:
                rel = p["relative_to"]
                col.require(p["reference_frame"] == "eef_at_start",
                            f"frame {i}: relative poses use reference_frame eef_at_start", path=f"{fw}/eef", **loc)
                col.require(rel["frame_index"] == 0, f"frame {i}: 1.0 only accepts relative_to.frame_index=0",
                            path=f"{fw}/eef/relative_to", **loc)
                if i == 0:
                    col.require(np.linalg.norm(p["position_m"]) < 1e-7 and abs(abs(q[3]) - 1) < 1e-7,
                                "relative pose at frame 0 must be the identity", path=f"{fw}/eef", **loc)
                anchor = rel["anchor_pose"]
                if anchor is None:
                    anchor_missing = True
                else:
                    aq = np.asarray(anchor["quaternion_xyzw"], float)
                    if col.require(abs(np.linalg.norm(aq) - 1) < 1e-5 and anchor["reference_frame"]
                                   == s["reference_frame"], f"frame {i}: invalid anchor_pose",
                                   path=f"{fw}/eef/relative_to/anchor_pose", **loc):
                        T_abs[i] = G.pose_matrix(anchor["position_m"], aq) @ T
        g = r["gripper"]
        if g is not None and g["closed_fraction"] is not None:
            grip[i] = g["closed_fraction"]
        for cid, cf in r["cameras"].items():
            cw = f"{fw}/cameras/{cid}"
            if not col.require(cid in cams, f"frame {i}: camera {cid} has no camera view", path=cw,
                               camera_id=cid, **loc):
                continue
            d = cam_data[cid]
            m = cams[cid]["media"]
            col.require(list(cf["image_size_wh"]) == list(m["image_size_wh"]),
                        f"frame {i}: {cid} image_size_wh differs from the view media", path=cw, camera_id=cid, **loc)
            vf, vt = cf["video_frame_index"], cf["video_timestamp_s"]
            if col.require((vf is None) == (vt is None), f"frame {i}: {cid} video frame and time must both be set "
                           "or both null", path=cw, camera_id=cid, **loc) and vf is not None:
                col.require(vf < m["frame_count"] and vf >= last_vf[cid],
                            f"frame {i}: {cid} video_frame_index {vf} out of order or beyond the clip",
                            path=f"{cw}/video_frame_index", camera_id=cid, **loc)
                col.require(vt >= last_vt[cid], f"frame {i}: {cid} video time goes backwards",
                            path=f"{cw}/video_timestamp_s", camera_id=cid, **loc)
                if m["kind"] == "image":
                    col.require(vf == 0 and vt == 0, f"frame {i}: still image has only frame 0",
                                path=cw, camera_id=cid, **loc)
                elif m["clip_end_s"] is not None:
                    col.require(vt < m["clip_end_s"] - m["clip_start_s"] + 1e-6,
                                f"frame {i}: {cid} video time outside the clip", path=f"{cw}/video_timestamp_s",
                                camera_id=cid, **loc)
                last_vf[cid], last_vt[cid] = vf, vt
                d["vf"][i], d["vt"][i] = vf, vt
            H = np.asarray(cf["H_media_from_calibration"], float)
            if col.require(abs(np.linalg.det(H)) > 1e-12, f"frame {i}: {cid} H_media_from_calibration is singular",
                           path=f"{cw}/H_media_from_calibration", camera_id=cid, **loc):
                d["H"][i] = H
            if cf["calibration_id"] is not None:
                cal_id = cf["calibration_id"]
                if col.require(cal_id in calibs, f"frame {i}: unknown calibration_id {cal_id}",
                               path=f"{cw}/calibration_id", camera_id=cid, **loc):
                    c = calibs[cal_id]
                    col.require(c["camera_id"] == cid, f"frame {i}: calibration {cal_id} belongs to "
                                f"{c['camera_id']}", path=f"{cw}/calibration_id", camera_id=cid, **loc)
                    d["cid"][i] = cal_id
                    if c["extrinsics_mode"] == "static":
                        col.require(cf["T_reference_camera"] is None, f"frame {i}: static calibration cannot "
                                    "have a per-frame camera pose", path=cw, camera_id=cid, **loc)
                        d["T"][i] = np.asarray(c["T_reference_camera"], float)
                    elif col.require(cf["T_reference_camera"] is not None and G.is_rigid(cf["T_reference_camera"]),
                                     f"frame {i}: per_frame calibration needs a rigid T_reference_camera",
                                     path=f"{cw}/T_reference_camera", camera_id=cid, **loc):
                        d["T"][i] = np.asarray(cf["T_reference_camera"], float)
            else:
                col.require(cf["T_reference_camera"] is None, f"frame {i}: a camera pose needs a calibration_id",
                            path=cw, camera_id=cid, **loc)
            pr = cf["projection"]
            if pr is None:
                continue
            d["src"].add(pr["source"])
            w_, h_ = cf["image_size_wh"]
            for pid, x in pr["points"].items():
                if not col.require(pid in points, f"frame {i}: projection point {pid} is not defined",
                                   path=f"{cw}/projection/points/{pid}", camera_id=cid, point_id=pid, **loc):
                    continue
                trk = d["pts"].setdefault(pid, ProjectedTrack(np.full((n, 2), np.nan), np.full(n, np.nan),
                                                              np.full(n, "absent", object)))
                trk.status[i] = x["status"]
                if x["depth_m"] is not None:
                    trk.depth[i] = x["depth_m"]
                uv = x["uv_px"]
                if uv is not None:
                    trk.uv[i] = uv
                if x["status"] in ("valid", "out_of_frame"):
                    col.require((0 <= uv[0] < w_ and 0 <= uv[1] < h_) == x["in_frame"],
                                f"frame {i}: {cid}/{pid} in_frame does not match the pixels",
                                path=f"{cw}/projection/points/{pid}", camera_id=cid, point_id=pid, **loc)
                    col.require(x["depth_m"] is None or x["depth_m"] > 0,
                                f"frame {i}: {cid}/{pid} projected in front but depth <= 0",
                                path=f"{cw}/projection/points/{pid}", camera_id=cid, point_id=pid, **loc)
    if len(pose_types) > 1:
        col.error(SEMANTIC, "a sample mixes absolute and relative poses", path=f"{where}/frames")
    if col.n_errors:
        return None, summary

    clocks = {}
    origin: dict[str, decimal.Decimal] = {}          # one zero per clock_id, so channels stay comparable
    for raw in clocks_raw.values():
        vals = [v for v in raw["values"] if v is not None]
        origin[raw["clock_id"]] = min([origin.get(raw["clock_id"], vals[0]), *vals])
    for ch, raw in clocks_raw.items():
        t0 = origin[raw["clock_id"]]
        clocks[ch] = SourceClock(ch, raw["clock_id"], raw["unit"], raw["semantics"],
                                 np.array([float(v - t0) if v is not None else np.nan for v in raw["values"]]))
    streams = {}
    for cid, v in cams.items():
        d = cam_data[cid]
        src = d["src"]
        if len(src) > 1:
            col.warn(C.INPUT_INCONSISTENT, f"{cid}: projection source changes between frames", camera_id=cid)
        streams[cid] = CameraStream(cid, v["view_id"], v["mount"], v["media"], tuple(v["media"]["image_size_wh"]),
                                    d["vf"], d["vt"], d["cid"], d["T"], d["H"],
                                    next(iter(sorted(src))) if src else None, d["pts"])
    sample = EefSample(
        episode_index=ep, sample_id=sid, sample=s, calibration=cal, input_hash=_canonical_hash(entry),
        n_frames=n, timebase=s["timebase"], t=t if s["timebase"] == "video_pts" else None,
        eef_frame=s["eef_frame"], reference_frame=s["reference_frame"],
        pose_type=next(iter(pose_types)) if pose_types else None, T_reference_eef=T_abs, T_declared=T_decl,
        eef_mask=eef_mask, anchor_missing=anchor_missing, gripper=grip, source_state_index=ssi, clocks=clocks,
        points=points, axes=axes, cameras=streams, consistency={}, warnings=[])
    sample.consistency = self_consistency(sample)
    for cid, per in sample.consistency.get("cameras", {}).items():
        for pid, stats in per.items():
            if stats["max_px"] is not None and stats["max_px"] >= C.REPROJECTION_TOLERANCE_PX:
                f = stats["worst_frame"]
                col.warn(C.INPUT_INCONSISTENT, f"{cid}/{pid}: provided projection differs from the declared "
                         f"geometry by up to {stats['max_px']:.3f} px (frame {f})", camera_id=cid, point_id=pid,
                         frame_index=f)
            if stats["depth_max_m"] is not None and stats["depth_max_m"] >= 1e-4:
                col.warn(C.INPUT_INCONSISTENT, f"{cid}/{pid}: provided depth differs from the declared geometry "
                         f"by up to {stats['depth_max_m']:.5f} m", camera_id=cid, point_id=pid)
    sample.warnings = [x for x in col.issues if x.severity == "warning"]
    summary.update(cameras=len(cams), projection_points_checked=sample.consistency.get("points_checked", 0),
                   max_reprojection_difference_px=sample.consistency.get("max_px"),
                   media_files_checked=len(views) if media_exists is not None else 0)
    return sample, summary


# --- recomputation ---------------------------------------------------------------------------

def recompute_projection(sample: EefSample, camera_id: str, point_id: str) -> ProjectedTrack | None:
    """Project a defined 3D point with the declared pose and calibration (None when impossible)."""
    cam = sample.cameras[camera_id]
    cal = cam.calibration(sample)
    definition = sample.points.get(point_id)
    if cal is None or definition is None or not sample.has_absolute_pose:
        return None
    offsets = G.point_offsets(definition, sample.gripper)
    if offsets is None:
        return None
    T_cam = cam.T_reference_camera
    uv, depth = G.project_chain(sample.T_reference_eef, offsets, T_cam, cal["K"], cal["model"],
                                cal["distortion_coefficients"], cam.H)
    w, h = cam.image_size_wh
    status = np.full(sample.n_frames, "unknown", object)
    finite_pose = np.isfinite(sample.T_reference_eef[:, 0, 0]) & np.isfinite(offsets).all(-1) \
        & np.isfinite(T_cam[:, 0, 0] if T_cam.ndim == 3 else np.ones(sample.n_frames))
    behind = finite_pose & (depth <= 0)
    inside = (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
    status[finite_pose & ~behind & inside] = "valid"
    status[finite_pose & ~behind & ~inside & np.isfinite(uv).all(-1)] = "out_of_frame"
    status[behind] = "behind_camera"
    return ProjectedTrack(uv, np.where(finite_pose, depth, np.nan), status)


def self_consistency(sample: EefSample) -> dict[str, Any]:
    """Provided vs recomputed projection per camera and point (design 12 §6.1 ``declared_vs_recomputed``)."""
    out: dict[str, Any] = {"cameras": {}, "points_checked": 0, "max_px": None}
    for cid, cam in sample.cameras.items():
        if cam.projection_source != "provided":
            continue
        per = {}
        for pid, trk in cam.provided.items():
            rec = recompute_projection(sample, cid, pid)
            if rec is None:
                continue
            both = np.isfinite(trk.uv).all(-1) & np.isfinite(rec.uv).all(-1)
            if not both.any():
                continue
            diff = np.linalg.norm(trk.uv - rec.uv, axis=-1)
            dd = np.abs(trk.depth - rec.depth)
            dd_ok = np.isfinite(dd)
            worst = int(np.nanargmax(np.where(both, diff, -1)))
            per[pid] = {"frames": int(both.sum()), "max_px": float(diff[both].max()),
                        "median_px": float(np.median(diff[both])), "worst_frame": worst,
                        "depth_max_m": float(dd[dd_ok].max()) if dd_ok.any() else None}
            out["points_checked"] += int(both.sum())
        if per:
            out["cameras"][cid] = per
    maxima = [p["max_px"] for per in out["cameras"].values() for p in per.values()]
    out["max_px"] = max(maxima) if maxima else None
    out["tolerance_px"] = C.REPROJECTION_TOLERANCE_PX
    return out


def declared_track(sample: EefSample, camera_id: str, point_id: str) -> ProjectedTrack | None:
    """The projection under test: the provided one if present, else the platform recomputation."""
    cam = sample.cameras[camera_id]
    if point_id in cam.provided:
        return cam.provided[point_id]
    return recompute_projection(sample, camera_id, point_id)


def declared_point_ids(sample: EefSample, camera_id: str) -> list[str]:
    cam = sample.cameras[camera_id]
    ids = set(cam.provided)
    if cam.calibration(sample) is not None and sample.has_absolute_pose:
        ids |= {pid for pid, d in sample.points.items() if d["model"] != "external_2d"}
    return sorted(ids)
