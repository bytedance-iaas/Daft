"""The console's videos of an mcap episode: muxed from the source on demand, in memory.

LeRobot's videos are files in the bucket, so the browser plays them through a presigned
URL (D16: the Daemon never relays them). An mcap dataset has no such file - its cameras
live inside the episode's ``.mcap`` - so for mcap the Daemon is the one thing between
the bucket and the browser: it reads the episode by ranged GETs (the same streamed read
the checks use), muxes every camera into mp4 in memory (``streams.clip``, the reader's
own muxers, no re-encoding) and serves the bytes with ``Range`` support so the player
can seek. Nothing touches disk and nothing is uploaded.

``EpisodeView.videos`` names these with a virtual path under the delivery scope,
``stream/cameras/ep<NNNNNN>__<camera>.mp4``; ``GET /media/sign`` turns that path into
this Daemon's own URL instead of a TOS signature, so the page code is the same for every
format. The muxed episode stays in a bounded LRU (``ResultStore.clips``): the two or
three cameras of an episode come from one read, and a page that plays them together
asks for them together.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from curation.cli import lerobot_meta as M
from curation.cli.storage import ObjectInfo

from ..repo import protocol as P
from .files import cached_json

log = logging.getLogger("daemon.results")

STREAM_PREFIX = "stream/cameras"
_STREAM_RE = re.compile(r"^stream/cameras/ep(\d{6})__([0-9A-Za-z_-]+)\.mp4$")
PREFLIGHT = "preflight.json"
SOURCE_MANIFEST = "source_manifest.json"


def stream_path(episode: int, camera: str) -> str:
    from curation.streams.clip import safe_name

    return f"{STREAM_PREFIX}/ep{int(episode):06d}__{safe_name(camera)}.mp4"


def parse_stream_path(path: str) -> tuple[int, str] | None:
    """``(episode, camera file name)`` of a virtual stream path, else None."""
    m = _STREAM_RE.match(str(path or ""))
    return (int(m.group(1)), m.group(2)) if m else None


def is_mcap(run_dir: Path, docs) -> bool:
    try:
        doc = cached_json(docs, run_dir / PREFLIGHT) or {}
    except (FileNotFoundError, ValueError):
        return False
    return (doc.get("format") or {}).get("kind") == "mcap"


def _listing(run_dir: Path, docs) -> tuple[dict[str, ObjectInfo], str]:
    """The task's frozen source (D27): ``{key: ObjectInfo}`` and the manifest digest."""
    manifest = cached_json(docs, run_dir / SOURCE_MANIFEST) or {}
    listing = {}
    for o in manifest.get("objects") or []:
        if isinstance(o, dict) and isinstance(o.get("key"), str) and isinstance(o.get("size"), int):
            listing[o["key"]] = ObjectInfo(o["key"], int(o["size"]), etag=o.get("etag"))
    digest = str(((manifest.get("summary") or {}).get("digest")) or "")
    return listing, digest


class ClipSource:
    """Muxes an episode's cameras from the task's source and keeps them in the store's LRU."""

    def __init__(self, store, task: P.Task, run_dir: Path):
        self.store, self.task, self.run_dir = store, task, run_dir

    def _build(self, episode: int) -> dict[str, bytes]:
        from curation.cli import containers
        from curation.pipeline.config import load_config
        from curation.streams.clip import episode_clips
        from curation.streams.objects import LocalDirObjects

        listing, _ = _listing(self.run_dir, self.store.docs)
        name = containers.mcap_episodes(listing).get(int(episode))
        if name is None or self.store.open_input is None:
            return {}
        mapping = containers.mapping_of(load_config(None))
        with self.store.open_input(self.task) as storage:
            objs = (containers.TosObjects(storage, listing) if getattr(storage, "remote", False)
                    else LocalDirObjects(storage.root))
            clips = episode_clips(objs, name, mapping)
        # keyed by the name the page shows: preflight's ``short_camera`` of the topic key
        return {_safe(M.short_camera(cam)): data for cam, data in clips.items()}

    def clips(self, episode: int) -> dict[str, bytes]:
        _, digest = _listing(self.run_dir, self.store.docs)
        key = ("clips", self.task.id, digest, int(episode))
        hit = self.store.clips.get(key)
        if hit is not None:
            return hit
        lock = self.store.clip_locks.setdefault(key, threading.Lock())
        with lock:                                     # one read per episode, not one per camera
            hit = self.store.clips.get(key)
            if hit is not None:
                return hit
            try:
                built = self._build(episode)
            except Exception as exc:  # noqa: BLE001 - source unreadable, mapping unknown: no video
                log.info("mcap clips of task %s episode %s not built: %s", self.task.id, episode, exc)
                built = {}
            self.store.clips.put(key, built, size=sum(len(v) for v in built.values()))
            return built

    def clip(self, episode: int, camera: str) -> bytes | None:
        return self.clips(episode).get(camera)


def _safe(cam: str) -> str:
    from curation.streams.clip import safe_name

    return safe_name(cam)


def cameras_of(run_dir: Path, docs, episode: int) -> dict[str, str]:
    """``{camera: virtual stream path}`` for an mcap task, from preflight's camera list."""
    if not is_mcap(run_dir, docs):
        return {}
    try:
        doc = cached_json(docs, run_dir / PREFLIGHT) or {}
    except (FileNotFoundError, ValueError):
        return {}
    cams = ((doc.get("dataset") or {}).get("cameras")) or []
    return {str(c): stream_path(episode, str(c)) for c in cams if isinstance(c, str)}


def slice_range(data: bytes, range_header: str | None) -> tuple[int, bytes, dict[str, str]]:
    """``(status, body, headers)`` of an HTTP ``Range`` request over ``data`` (one range,
    ``bytes=a-b`` / ``bytes=a-`` / ``bytes=-n``); the whole body without one."""
    total = len(data)
    headers = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4"}
    m = re.match(r"^bytes=(\d*)-(\d*)$", (range_header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        headers["Content-Length"] = str(total)
        return 200, data, headers
    if m.group(1):
        start = int(m.group(1))
        end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
    else:
        n = min(int(m.group(2)), total)
        start, end = total - n, total - 1
    if start >= total or start > end:
        headers["Content-Range"] = f"bytes */{total}"
        return 416, b"", headers
    body = data[start:end + 1]
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    headers["Content-Length"] = str(len(body))
    return 206, body, headers
