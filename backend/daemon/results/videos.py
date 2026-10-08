"""Where each camera of an episode can be played from (design doc 03 §7; v1's order).

For every camera the first of these that exists wins:

1. **clip** - ``details/audit_clips/ep<NNNNNN>__<camera>.mp4``, pre-cut for the
   adjudication page (v1's layout), in the task's run directory (``scope=delivery``);
2. **source_dataset** - the input dataset (``scope=input``); rejected episodes only
   exist there. For an mcap dataset, whose cameras are inside the episode file, this
   origin is a virtual path (``stream/cameras/ep<NNNNNN>__<camera>.mp4``) that
   ``GET /media/sign`` resolves to this Daemon's own streaming URL: the episode is
   muxed from the source in memory on demand (:mod:`.clips`).

The browser signs a path with ``GET /media/sign`` (W8) and plays it directly from TOS.
LeRobot v3 keeps several episodes in one mp4, so those entries carry ``from_ts`` /
``to_ts`` from the dataset's episode table (``videos/<camera>/from_timestamp``,
``to_timestamp``) and the page plays ``url#t=from,to``; signing does not compute them.

The input dataset's metadata (``meta/info.json``, v3 ``meta/episodes/...parquet``) is
read with the task's input key through the store's ``open_input`` hook, once per task:
the task's source is frozen (D27), and the metadata sizes are checked against the
task's ``source_manifest.json``. A failure (key deleted, TOS unreachable, metadata
changed) leaves the source out for a minute instead of failing the request.
"""
from __future__ import annotations

import fnmatch
import io
import json
import logging
import time
from pathlib import Path
from typing import Iterable

from curation.cli import lerobot_meta as M

from ..repo import protocol as P
from .files import cached_json, identity
from .revision import Revision

log = logging.getLogger("daemon.results")

CLIP_DIR = "details/audit_clips"
SOURCE_MANIFEST = "source_manifest.json"
PREFLIGHT = "preflight.json"
#: after a failed read of the input metadata, how long the source stays out
RETRY_AFTER_S = 60.0

Window = tuple[str, "float | None", "float | None"]      # (key, from_ts, to_ts)


class LeRobotVideos:
    """Per-episode video keys of a LeRobot v2 / v3 dataset, by short camera name."""

    def __init__(self, info: dict, version: str, v3_rows: dict[int, dict] | None = None):
        self.info = info
        self.version = version
        self.features = M.cameras_of(info)
        self.v3_rows = v3_rows or {}

    def cameras(self) -> list[str]:
        return [M.short_camera(f) for f in self.features]

    def of(self, episode: int) -> dict[str, Window]:
        tpl = self.info.get("video_path")
        if not isinstance(tpl, str) or not tpl:
            return {}
        out: dict[str, Window] = {}
        if self.version == "v2":
            try:
                chunk = int(episode) // int(self.info["chunks_size"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                return {}
            for vk in self.features:
                try:
                    key = tpl.format(episode_chunk=chunk, video_key=vk, episode_index=int(episode))
                except (KeyError, IndexError, ValueError):
                    return {}
                out[M.short_camera(vk)] = (key, None, None)
            return out
        row = self.v3_rows.get(int(episode))
        if row is None:
            return {}
        for vk in self.features:
            try:
                key = tpl.format(video_key=vk, chunk_index=int(row[f"videos/{vk}/chunk_index"]),
                                 file_index=int(row[f"videos/{vk}/file_index"]))
                start = row.get(f"videos/{vk}/from_timestamp")
                end = row.get(f"videos/{vk}/to_timestamp")
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            out[M.short_camera(vk)] = (key, _num(start), _num(end))
        return out


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _read_checked(storage, key: str, sizes: dict[str, int] | None) -> bytes:
    data = storage.read_bytes(key)
    if sizes is not None and key in sizes and sizes[key] != len(data):
        raise ValueError(f"{key} changed since the task started ({len(data)} != {sizes[key]} bytes)")
    return data


def read_lerobot(storage, keys: Iterable[str], sizes: dict[str, int] | None = None) -> LeRobotVideos:
    """The video index of the dataset behind ``storage``; ``keys`` is its listing (for v3's
    episode tables), ``sizes`` the expected sizes (the task's source manifest)."""
    import pyarrow.parquet as pq

    info = json.loads(_read_checked(storage, M.INFO_KEY, sizes).decode("utf-8"))
    if not isinstance(info, dict):
        raise ValueError("meta/info.json is not an object")
    version = M.version_of(str(info.get("codebase_version") or ""))
    if version == "v2":
        return LeRobotVideos(info, "v2")
    if version != "v3":
        raise ValueError(f"not LeRobot v2 / v3: {info.get('codebase_version')!r}")
    features = M.cameras_of(info)
    wanted = ["episode_index"] + [f"videos/{vk}/{c}" for vk in features
                                  for c in ("chunk_index", "file_index", "from_timestamp",
                                            "to_timestamp")]
    rows: dict[int, dict] = {}
    for key in sorted(k for k in keys if fnmatch.fnmatchcase(k, M.V3_EPISODES_GLOB)):
        buf = io.BytesIO(_read_checked(storage, key, sizes))
        names = set(pq.ParquetFile(buf).schema_arrow.names)
        buf.seek(0)
        table = pq.read_table(buf, columns=[c for c in wanted if c in names])
        for r in table.to_pylist():
            if r.get("episode_index") is not None:
                rows[int(r["episode_index"])] = r
    return LeRobotVideos(info, "v3", rows)


class _Failed:
    def __init__(self, why: str):
        self.why = why
        self.at = time.monotonic()


# -- the two origins ---------------------------------------------------------------------

def clip_videos(run_dir: Path, episode: int, cameras: Iterable[str]) -> dict[str, str]:
    out = {}
    for cam in cameras:
        rel = f"{CLIP_DIR}/ep{int(episode):06d}__{cam}.mp4"
        if (run_dir / rel).is_file():
            out[cam] = rel
    return out


def source_videos(rev: Revision, episode: int) -> dict[str, Window]:
    try:
        kind = ((cached_json(rev.store.docs, rev.run_dir / PREFLIGHT) or {}).get("format")
                or {}).get("kind")
    except (FileNotFoundError, ValueError):
        kind = None
    if kind == "mcap":       # muxed from the source on demand by this Daemon (clips.py)
        from .clips import cameras_of

        return {cam: (rel, None, None)
                for cam, rel in cameras_of(rev.run_dir, rev.store.docs, episode).items()}
    if kind == "lance":
        return {}            # D44: its videos sit inside the tables, not as mp4s
    index = source_index(rev)
    return index.of(episode) if index is not None else {}


def source_index(rev: Revision) -> LeRobotVideos | None:
    """The input dataset's video index (cached per task and source digest)."""
    store, task = rev.store, rev.task
    opener = store.open_input
    if opener is None:
        return None
    manifest = None
    try:
        manifest = cached_json(store.docs, rev.run_dir / SOURCE_MANIFEST)
    except (FileNotFoundError, ValueError):
        manifest = None
    digest = ((manifest or {}).get("summary") or {}).get("digest")
    key = ("source", task.id, task.input_uri, digest)
    hit = store.sources.get(key)
    if isinstance(hit, LeRobotVideos):
        return hit
    if isinstance(hit, _Failed) and time.monotonic() - hit.at < RETRY_AFTER_S:
        return None
    index = _read_source(task, opener, manifest)
    store.sources.put(key, index)
    return index if isinstance(index, LeRobotVideos) else None


def _read_source(task: P.Task, opener, manifest: dict | None):
    objects = [o for o in (manifest or {}).get("objects") or [] if isinstance(o, dict)]
    sizes = {str(o["key"]): int(o["size"]) for o in objects
             if isinstance(o.get("key"), str) and isinstance(o.get("size"), int)} or None
    try:
        with opener(task) as storage:
            keys = list(sizes) if sizes else None
            if keys is None and not getattr(storage, "remote", True):
                keys = [k for k in storage.list() if k.startswith("meta/")]
            return read_lerobot(storage, keys or [], sizes)
    except Exception as exc:  # noqa: BLE001 - key deleted, TOS unreachable, metadata changed
        log.info("input metadata of task %s not read: %s", task.id, exc)
        return _Failed(str(exc))


def preflight_cameras(rev: Revision) -> list[str]:
    try:
        doc = cached_json(rev.store.docs, rev.run_dir / PREFLIGHT)
    except (FileNotFoundError, ValueError):
        return []
    cams = ((doc or {}).get("dataset") or {}).get("cameras") or []
    return [str(c) for c in cams if isinstance(c, str)]


def episode_videos(rev: Revision, episode: int) -> list[dict]:
    """C4 ``EpisodeView.videos``: one entry per camera, from the first origin that has it."""
    source = source_videos(rev, episode)
    cameras = list(dict.fromkeys([*preflight_cameras(rev), *source]))
    clips = clip_videos(rev.run_dir, episode, cameras)
    out = []
    for cam in cameras:
        if cam in clips:
            out.append({"camera": cam, "scope": "delivery", "origin": "clip", "path": clips[cam]})
            continue
        for origin, scope, found in (("source_dataset", "input", source),):
            if cam in found:
                path, start, end = found[cam]
                item = {"camera": cam, "scope": scope, "origin": origin, "path": path}
                if start is not None and end is not None:
                    item.update(from_ts=start, to_ts=end)
                out.append(item)
                break
    return out
