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
import json
import os
import pathlib
from typing import Iterable, Protocol

import numpy as np

from . import contracts as C
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


def load_seed_file(path: str | os.PathLike, *, sample_id: str, camera_id: str) -> Seeds:
    from curation.contracts import schemas

    validator = schemas.validator("eef/observation.schema.json")
    by_frame: dict[int, dict[str, SeedPoint]] = {}
    hashes: dict[int, str] = {}
    methods = set()
    versions = set()
    for n, line in enumerate(pathlib.Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        errs = [e.message for e in validator.iter_errors(row)]
        if errs:
            raise SeedError(f"{path}:{n}: {errs[0]}")
        if row["sample_id"] != sample_id or row["camera_id"] != camera_id:
            raise SeedError(f"{path}:{n}: row is for {row['sample_id']}/{row['camera_id']}")
        vf = int(row["video_frame_index"])
        by_frame[vf] = {pid: SeedPoint(tuple(p["uv_px"]) if p["uv_px"] is not None else None, p["visibility"],
                                       float(p["confidence"])) for pid, p in row["points"].items()}
        hashes[vf] = row["input_image_sha256"]
        methods.add(row["method"])
        versions.add(row["model_version"])
    if not by_frame:
        raise SeedError(f"{path}: no seed rows")
    return Seeds(sample_id, camera_id, "+".join(sorted(methods)), "+".join(sorted(versions)), by_frame, hashes)


def find_seeds(seed_root: str | os.PathLike | None, sample_id: str, camera_id: str) -> Seeds | None:
    if not seed_root:
        return None
    p = pathlib.Path(seed_root) / sample_id / f"{camera_id}.jsonl"
    if not p.is_file():
        return None
    return load_seed_file(p, sample_id=sample_id, camera_id=camera_id)


def seeded_points(seed_root: str | os.PathLike | None, sample: EefSample) -> dict[str, set[str]] | None:
    """camera id -> point ids a seeded provider can observe (for the capability table)."""
    if not seed_root:
        return None
    out = {}
    for cid in sample.cameras:
        p = pathlib.Path(seed_root) / sample.sample_id / f"{cid}.jsonl"
        if p.is_file():
            pts = set()
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    pts |= set(json.loads(line)["points"])
            out[cid] = pts
    return out


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


def provider_inputs(sample: EefSample, camera_id: str, *, media_root: str | os.PathLike,
                    seeds: Seeds | None, point_ids: Iterable[str] | None = None) -> tuple[ProviderContext, PointTargets]:
    """The whitelist: media location and size, point ids, seeds. No projection, pose or calibration."""
    cam: CameraStream = sample.cameras[camera_id]
    m = cam.media
    ctx = ProviderContext(sample.sample_id, camera_id, media_path(sample, camera_id, media_root),
                          tuple(m["image_size_wh"]), int(m["frame_count"]), m["fps"], float(m["clip_start_s"]),
                          m["clip_end_s"])
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
