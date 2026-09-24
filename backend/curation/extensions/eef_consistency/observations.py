"""Independent visual observations: the provider protocol, its input whitelist and observation files
(design 12 §7.1, format §6, D-E5).

A provider receives decoded frames, the ids of the physical points to locate and - for the seeded
tracker - the seed observations. It never receives the declared projection, the EEF pose, the
calibration, notes or anything from ``evaluation/``: ``provider_inputs`` is the only constructor of
its inputs and copies whitelisted fields only. Observation files carry
``projection_visible_to_localizer=false`` and the hash of the image each row was measured on.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import pathlib
from typing import Iterable, Iterator, Protocol

import numpy as np

from . import contracts as C
from . import mcap_media as MM
from . import video as V
from .load import CameraStream, EefSample
from .video import DecodedFrame

VISIBLE, OCCLUDED, OUT_OF_FRAME, UNCERTAIN = "visible", "occluded", "out_of_frame", "uncertain"
NO_OBSERVATION = "none"                          # frame not processed / no row written


@dataclasses.dataclass(frozen=True)
class SeedPoint:
    uv: tuple[float, float] | None
    visibility: str
    confidence: float


@dataclasses.dataclass(frozen=True)
class Seeds:
    """Independently placed points (a person clicking, or the DEMO ``synthetic_fixture``)."""

    sample_id: str
    camera_id: str
    method: str
    model_version: str
    by_media_frame: dict[int, dict[str, SeedPoint]]
    image_sha256: dict[int, str]

    @property
    def point_ids(self) -> set[str]:
        return {pid for pts in self.by_media_frame.values() for pid in pts}


class SeedError(ValueError):
    pass


def _parse_rows(path: pathlib.Path, text: str) -> list[tuple[str, dict]]:
    """(location, row) for a JSONL file, a JSON array, or ``{"rows": [...]}``."""
    stripped = text.lstrip()
    if path.suffix == ".json" or stripped.startswith("["):
        doc = json.loads(text)
        rows = doc.get("rows") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise SeedError(f"{path}: expected a JSON array of observation rows")
        return [(f"{path}[{i}]", r) for i, r in enumerate(rows)]
    return [(f"{path}:{n}", json.loads(line)) for n, line in enumerate(text.splitlines(), 1) if line.strip()]


@functools.lru_cache(maxsize=8)
def _file_rows(path: str, mtime_ns: int, size: int) -> tuple[tuple[str, dict], ...]:
    p = pathlib.Path(path)
    return tuple(_parse_rows(p, p.read_text(encoding="utf-8")))


def seed_rows(seed_root: str | os.PathLike | None, sample_id: str, camera_id: str | None = None
              ) -> list[tuple[str, dict]]:
    """Seed rows of one sample (and camera). ``seed_root`` is a directory laid out as
    ``<sample_id>/<camera_id>.jsonl``, or one file (JSONL or a JSON array) holding rows of any
    sample and camera - the form the console uploads."""
    if not seed_root:
        return []
    root = pathlib.Path(seed_root)
    if root.is_dir():
        files = [root / sample_id / f"{camera_id}.jsonl"] if camera_id else \
            sorted((root / sample_id).glob("*.jsonl")) if (root / sample_id).is_dir() else []
        out = []
        for f in files:
            if f.is_file():
                out += _parse_rows(f, f.read_text(encoding="utf-8"))
        return out
    if root.is_file():
        st = root.stat()
        return [(loc, r) for loc, r in _file_rows(str(root), st.st_mtime_ns, st.st_size)
                if isinstance(r, dict) and r.get("sample_id") == sample_id
                and (camera_id is None or r.get("camera_id") == camera_id)]
    return []


def _seeds_from_rows(rows: list[tuple[str, dict]], *, sample_id: str, camera_id: str) -> Seeds:
    from curation.contracts import schemas

    validator = schemas.validator("eef/observation.schema.json")
    by_frame: dict[int, dict[str, SeedPoint]] = {}
    hashes: dict[int, str] = {}
    methods = set()
    versions = set()
    for loc, row in rows:
        errs = [e.message for e in validator.iter_errors(row)]
        if errs:
            raise SeedError(f"{loc}: {errs[0]}")
        if row["sample_id"] != sample_id or row["camera_id"] != camera_id:
            raise SeedError(f"{loc}: row is for {row['sample_id']}/{row['camera_id']}")
        vf = int(row["video_frame_index"])
        by_frame[vf] = {pid: SeedPoint(tuple(p["uv_px"]) if p["uv_px"] is not None else None, p["visibility"],
                                       float(p["confidence"])) for pid, p in row["points"].items()}
        hashes[vf] = row["input_image_sha256"]
        methods.add(row["method"])
        versions.add(row["model_version"])
    if not by_frame:
        raise SeedError(f"no seed rows for {sample_id}/{camera_id}")
    return Seeds(sample_id, camera_id, "+".join(sorted(methods)), "+".join(sorted(versions)), by_frame, hashes)


def load_seed_file(path: str | os.PathLike, *, sample_id: str, camera_id: str) -> Seeds:
    p = pathlib.Path(path)
    return _seeds_from_rows(_parse_rows(p, p.read_text(encoding="utf-8")), sample_id=sample_id, camera_id=camera_id)


def find_seeds(seed_root: str | os.PathLike | None, sample_id: str, camera_id: str) -> Seeds | None:
    rows = seed_rows(seed_root, sample_id, camera_id)
    if not rows:
        return None
    return _seeds_from_rows(rows, sample_id=sample_id, camera_id=camera_id)


def seeded_points(seed_root: str | os.PathLike | None, sample: EefSample) -> dict[str, set[str]] | None:
    """camera id -> point ids a seeded provider can observe (for the capability table)."""
    if not seed_root:
        return None
    out = {}
    for cid in sample.cameras:
        rows = seed_rows(seed_root, sample.sample_id, cid)
        if rows:
            out[cid] = {pid for _, r in rows for pid in (r.get("points") or {})}
    return out


def seeds_digest(seed_root: str | os.PathLike | None, sample_id: str) -> str | None:
    """sha256 of one sample's seed rows (any camera), so a changed seed file reads as a new input."""
    rows = seed_rows(seed_root, sample_id)
    if not rows:
        return None
    text = json.dumps(sorted((json.dumps(r, sort_keys=True) for _, r in rows)), separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


# --- provider interface ----------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProviderContext:
    """What a provider may know about the media. Built only by ``provider_inputs``."""

    sample_id: str
    camera_id: str
    media_path: str
    image_size_wh: tuple[int, int]
    media_frame_count: int
    fps: float | None
    clip_start_s: float
    clip_end_s: float | None
    topic: str | None = None           # an mcap image topic of the file at media_path (F5.13)


@dataclasses.dataclass(frozen=True)
class PointTargets:
    point_ids: tuple[str, ...]
    seeds: Seeds | None


@dataclasses.dataclass
class ObservationBatch:
    """Per media frame and point: pixels, visibility, confidence, uncertainty."""

    camera_id: str
    method: str
    model_version: str
    n: int
    uv: dict[str, np.ndarray]
    visibility: dict[str, np.ndarray]
    confidence: dict[str, np.ndarray]
    uncertainty: dict[str, np.ndarray]
    image_sha256: dict[int, str]
    stats: dict
    seeded_by_projection: bool = False

    @classmethod
    def empty(cls, camera_id: str, method: str, model_version: str, n: int, point_ids: Iterable[str]):
        pids = list(point_ids)
        return cls(camera_id, method, model_version, n,
                   {p: np.full((n, 2), np.nan) for p in pids}, {p: np.full(n, NO_OBSERVATION, object) for p in pids},
                   {p: np.zeros(n) for p in pids}, {p: np.full(n, np.nan) for p in pids}, {}, {})


class ObservationProvider(Protocol):
    method: str
    model_version: str

    def locate(self, frames: Iterable[DecodedFrame], targets: PointTargets,
               ctx: ProviderContext) -> ObservationBatch: ...


def media_path(sample: EefSample, camera_id: str, media_root: str | os.PathLike) -> str:
    return str(pathlib.Path(media_root) / sample.cameras[camera_id].media["uri"])


def media_frames(path: str, media: dict) -> Iterator[DecodedFrame]:
    """The decoded frames of a view, numbered as ``video_frame_index`` counts them: a video file's
    clip on its PTS timeline, or an mcap image topic's messages in log_time order (F5.13). Lazy, like
    the decoder: nothing is read before the first frame is asked for."""
    if media.get("topic"):
        try:
            local = MM.video(path, media["topic"])
        except (MM.TopicError, OSError) as exc:
            raise V.DecodeError(str(exc)) from exc
        yield from V.iter_clip(local, clip_start_s=0.0, clip_end_s=None, fps=MM.RATE,
                               frame_count=int(media["frame_count"]))
        return
    yield from V.iter_clip(path, clip_start_s=float(media["clip_start_s"]), clip_end_s=media["clip_end_s"],
                           fps=media["fps"], frame_count=int(media["frame_count"]))


def view_frames(sample: EefSample, camera_id: str, media_root: str | os.PathLike) -> Iterator[DecodedFrame]:
    return media_frames(media_path(sample, camera_id, media_root), sample.cameras[camera_id].media)


def context_frames(ctx: ProviderContext) -> Iterator[DecodedFrame]:
    """The frames a provider context points at (offline tools)."""
    return media_frames(ctx.media_path, {"topic": ctx.topic, "clip_start_s": ctx.clip_start_s,
                                         "clip_end_s": ctx.clip_end_s, "fps": ctx.fps,
                                         "frame_count": ctx.media_frame_count})


def provider_inputs(sample: EefSample, camera_id: str, *, media_root: str | os.PathLike,
                    seeds: Seeds | None, point_ids: Iterable[str] | None = None) -> tuple[ProviderContext, PointTargets]:
    """The whitelist: media location and size, point ids, seeds. No projection, pose or calibration."""
    cam: CameraStream = sample.cameras[camera_id]
    m = cam.media
    ctx = ProviderContext(sample.sample_id, camera_id, media_path(sample, camera_id, media_root),
                          tuple(m["image_size_wh"]), int(m["frame_count"]), m["fps"], float(m["clip_start_s"]),
                          m["clip_end_s"], m.get("topic"))
    ids = sorted(set(point_ids) if point_ids is not None else (seeds.point_ids if seeds else set()))
    return ctx, PointTargets(tuple(ids), seeds)


# --- mapping to the sample and files ---------------------------------------------------------

def on_sample_frames(batch: ObservationBatch, cam: CameraStream, point_id: str):
    """(uv (N,2), visibility (N,), confidence (N,)) on the sample's frames via ``video_frame_index``."""
    vf = cam.video_frame_index
    n = len(vf)
    uv = np.full((n, 2), np.nan)
    vis = np.full(n, NO_OBSERVATION, object)
    conf = np.zeros(n)
    if point_id not in batch.uv:
        return uv, vis, conf
    ok = (vf >= 0) & (vf < batch.n)
    uv[ok] = batch.uv[point_id][vf[ok]]
    vis[ok] = batch.visibility[point_id][vf[ok]]
    conf[ok] = batch.confidence[point_id][vf[ok]]
    uv[vis != VISIBLE] = np.nan
    return uv, vis, conf


def observation_rows(batch: ObservationBatch, sample: EefSample, camera_id: str) -> list[dict]:
    """observation.schema.json rows, one per sample frame that has a processed media frame."""
    cam = sample.cameras[camera_id]
    rows = []
    for i, vf in enumerate(cam.video_frame_index):
        if vf < 0 or vf >= batch.n or vf not in batch.image_sha256:
            continue
        pts = {}
        for pid in batch.uv:
            vis = batch.visibility[pid][vf]
            if vis == NO_OBSERVATION:
                continue
            uv = batch.uv[pid][vf]
            has = vis == VISIBLE and np.isfinite(uv).all()
            unc = batch.uncertainty[pid][vf]
            pts[pid] = {"uv_px": [round(float(uv[0]), 3), round(float(uv[1]), 3)] if has else None,
                        "visibility": vis if (has or vis != VISIBLE) else UNCERTAIN,
                        "confidence": float(np.clip(batch.confidence[pid][vf], 0, 1)) if has else 0.0,
                        "uncertainty_px": round(float(unc), 3) if has and np.isfinite(unc) else None}
        rows.append({"schema_version": C.SCHEMA_VERSION, "sample_id": sample.sample_id, "frame_index": i,
                     "camera_id": camera_id, "video_frame_index": int(vf), "pixel_space": "media",
                     "method": batch.method, "model_version": batch.model_version,
                     "input_image_sha256": batch.image_sha256[int(vf)], "projection_visible_to_localizer": False,
                     "points": pts})
    return rows


def write_rows(path: str | os.PathLike, rows: list[dict]) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, p)
