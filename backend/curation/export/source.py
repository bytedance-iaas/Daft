"""Read-side helpers of the export: source file identities and the source-manifest guard.

Every source object the export reads is identified by (key, size, tag), with
tag = ``etag:<ETag>`` on TOS and ``mtime_ns:<n>`` on a local disk -- the same fields
``curation snapshot`` records in ``source_manifest.json`` (D27).  When the snapshot
is given, every object is checked against it before use and a mismatch raises
``SourceChangedError`` (the CLI's exit code 6).  The identities also make each
episode's ``content_key``: a source that did not change gives the same keys, which
is what lets an incremental export trust the previous one.
"""
from __future__ import annotations

import os

from ..ingest import dsfs
from .manifest import digest_json


class ExportError(RuntimeError):
    """The export cannot be done as asked (bad input lists, broken source)."""


class ExportInputError(ExportError, ValueError):
    """The passed / held lists are unusable (CLI exit code 2)."""


class SourceChangedError(ExportError):
    """A source object differs from ``source_manifest.json`` (CLI exit code 6)."""

    exit_code = 6


def _norm_etag(etag: str) -> str:
    return str(etag or "").strip().strip('"').lower()


class SourceGuard:
    """Identities of the source objects the export reads, checked against the snapshot."""

    def __init__(self, root: str, source_manifest: dict | None = None):
        self.root = str(root).rstrip("/") if dsfs.is_remote(root) else os.path.abspath(root)
        self.manifest = source_manifest
        self._pinned: dict[str, dict] | None = None
        if source_manifest is not None:
            self._pinned = {str(o["key"]).lstrip("/"): o for o in source_manifest.get("objects") or []}
        self.seen: dict[str, list | None] = {}

    def path(self, rel: str) -> str:
        return dsfs.join(self.root, *rel.split("/"))

    def _stat(self, rel: str) -> tuple[int, str] | None:
        p = self.path(rel)
        if dsfs.is_remote(p):
            kind, size, etag = dsfs._stat(p)        # listing-backed, no extra request
            if kind != "file":
                return None
            return int(size), "etag:" + _norm_etag(etag)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return None
        return int(st.st_size), f"mtime_ns:{st.st_mtime_ns}"

    def identity(self, rel: str) -> list | None:
        """[key, size, tag] of ``rel``, or None when it does not exist."""
        rel = rel.lstrip("/")
        if rel in self.seen:
            return self.seen[rel]
        cur = self._stat(rel)
        if self._pinned is not None:
            pin = self._pinned.get(rel)
            if pin is None and cur is not None:
                raise SourceChangedError(
                    f"source object {rel} is not in source_manifest.json; re-run the "
                    "preflight and create a new task")
            if pin is not None:
                want = int(pin["size"])
                tag = ("etag:" + _norm_etag(pin["etag"])) if "etag" in pin \
                    else f"mtime_ns:{int(pin['mtime_ns'])}"
                if cur is None or cur[0] != want or cur[1] != tag:
                    got = "missing" if cur is None else f"size={cur[0]} {cur[1]}"
                    raise SourceChangedError(
                        f"source object {rel} changed since the snapshot "
                        f"(want size={want} {tag}, got {got}); re-run the preflight and "
                        "create a new task")
        ident = None if cur is None else [rel, cur[0], cur[1]]
        self.seen[rel] = ident
        return ident

    def require(self, rel: str) -> list:
        ident = self.identity(rel)
        if ident is None:
            raise ExportError(f"source object missing: {self.path(rel)}")
        return ident

    def digest(self) -> str:
        """The snapshot's digest, else a digest of every identity read so far."""
        if self.manifest is not None:
            d = (self.manifest.get("summary") or {}).get("digest")
            if d:
                return str(d)
        return digest_json(sorted(v for v in self.seen.values() if v is not None))
