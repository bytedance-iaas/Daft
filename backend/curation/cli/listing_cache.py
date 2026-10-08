"""The input's object listing, listed once per task and shared by the commands of a run directory.

Every stage's worker is its own ``curation check`` process, and each one used to list the whole
dataset prefix on TOS at start - three times over: the command's own listing, the source guard's
and v1's reader's (``ingest/dsfs.prefetch``). On DROID (10k objects) one listing is 11.6 s, on a
100k-episode dataset minutes, and nothing in it changes between the snapshot and the workers.

So ``snapshot`` writes the listing it took next to ``source_manifest.json`` (``.source_listing.json``,
hidden: not delivered, not restored), and every later command given ``--source-manifest`` reads it
back instead of listing the bucket, hands it to its guard and seeds the reader with it. The file
expires after :data:`MAX_AGE_S`, and the Daemon drops it when a task is resumed or retried, so a
worker started long after the snapshot lists afresh and the source guard (D27) still sees a change
made since.
"""
from __future__ import annotations

import json
import os
import time

from .storage import ObjectInfo

CACHE_NAME = ".source_listing.json"
MAX_AGE_S = 3600.0
SCHEMA_VERSION = "1.0"


def path_beside(manifest_path: str) -> str:
    """Where the listing of a run directory lives: next to its source manifest."""
    return os.path.join(os.path.dirname(os.path.abspath(manifest_path)), CACHE_NAME)


def save(path: str, uri: str, listing: dict[str, ObjectInfo]) -> None:
    doc = {"schema_version": SCHEMA_VERSION, "input": uri, "at": time.time(),
           "objects": [{"key": o.key, "size": int(o.size), "etag": o.etag, "crc64": o.crc64}
                       for o in listing.values()]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def load(path: str, uri: str, *, max_age_s: float | None = None) -> dict[str, ObjectInfo] | None:
    """The cached listing of ``uri``, or None when there is none, it is another input's, it is
    older than ``max_age_s`` (default :data:`MAX_AGE_S`) or it cannot be read."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("input") != uri:
        return None
    age = time.time() - float(doc.get("at") or 0)
    if age > (MAX_AGE_S if max_age_s is None else max_age_s):
        return None
    out: dict[str, ObjectInfo] = {}
    for o in doc.get("objects") or []:
        try:
            out[o["key"]] = ObjectInfo(o["key"], int(o["size"]), etag=o.get("etag"), crc64=o.get("crc64"))
        except (KeyError, TypeError, ValueError):
            return None
    return out or None


def forget(run_dir: str) -> None:
    """Drop a run directory's cached listing (the Daemon, before a resume or a retry)."""
    try:
        os.remove(os.path.join(run_dir, CACHE_NAME))
    except FileNotFoundError:
        pass


def _meta_unchanged(storage, cached: dict[str, ObjectInfo]) -> bool:
    """A cheap look at the bucket before trusting the cache: the metadata files (a few objects)
    are read back by HEAD and must still match; a re-exported dataset changes them, and the
    guard then runs on a fresh listing. A data file rewritten alone within the cache's life is
    only caught by the next fresh listing."""
    for key, want in cached.items():
        if not key.startswith("meta/"):
            continue
        have = storage.stat(key)
        if have is None or int(have.size) != int(want.size) or have.identity() != want.identity():
            return False
    return True


def listing_for(ctx, storage, manifest_path: str | None) -> dict[str, ObjectInfo]:
    """The command's listing: the cached one when the command has a manifest, the cache is
    fresh and the metadata files are as listed, else a listing of the bucket, saved for the
    next command."""
    if not manifest_path or not getattr(storage, "remote", False):
        return storage.list()
    path = path_beside(manifest_path)
    cached = load(path, storage.uri)
    if cached is not None and _meta_unchanged(storage, cached):
        ctx.log("info", f"listing of {storage.uri}: {len(cached)} objects, from the snapshot's listing")
        return cached
    listing = storage.list()
    try:
        save(path, storage.uri, listing)
    except OSError as e:
        ctx.log("warn", f"could not keep the listing for later commands: {e}")
    return listing
