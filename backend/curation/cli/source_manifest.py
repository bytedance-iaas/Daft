"""``source_manifest.json``: the source objects a task reads, fixed at start (D27).

``snapshot`` writes it (:func:`build`); every command that reads source data
takes ``--source-manifest`` and checks the objects it reads against it
(:meth:`SourceManifest.verify`). Any difference - an object missing, resized,
re-uploaded (new ETag), touched (new mtime on a local path) or newly added in
the checked scope - ends the command with exit code 6, ``source_changed``: one
task never mixes two versions of its data.

Contract: ``docs/contracts/cli/source-manifest.schema.json``. The summary
digest is ``sha256`` over one ``key<TAB>size<TAB>identity`` line per object,
sorted by key, where identity is the ETag without quotes or the local mtime in ns.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable

from .errors import SourceChanged, UsageError
from .lerobot_meta import fingerprint
from .storage import ObjectInfo, is_remote, normalize_etag

SCHEMA_VERSION = "1.0"


def entry(obj: ObjectInfo) -> dict:
    out = {"key": obj.key, "size": int(obj.size)}
    if obj.etag is not None:
        out["etag"] = obj.etag
    else:
        out["mtime_ns"] = int(obj.mtime_ns or 0)
    return out


def build(input_uri: str, objects: Iterable[ObjectInfo],
          skipped: list[dict] | None = None) -> dict:
    """``skipped``: selected episodes whose source files are missing (D40), already in
    the contract's shape; they are left out of the task and of ``objects``."""
    objs = sorted(objects, key=lambda o: o.key)
    doc = {"schema_version": SCHEMA_VERSION, "input": input_uri,
           "objects": [entry(o) for o in objs]}
    if skipped:
        doc["skipped_episodes"] = sorted(skipped, key=lambda s: s["episode_index"])
    doc["summary"] = {"count": len(objs), "bytes": sum(int(o.size) for o in objs),
                      "digest": fingerprint(objs)}
    return doc


def normalize_uri(uri: str) -> str:
    uri = str(uri or "").strip()
    if is_remote(uri):
        return uri.rstrip("/")
    return os.path.abspath(os.path.expanduser(uri))


def write(path: str, doc: dict) -> None:
    """Write atomically: a reader sees the old file or the complete new one."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class SourceManifest:
    def __init__(self, doc: dict, path: str = "<memory>"):
        self.path = path
        self.input = normalize_uri(doc["input"])
        self.objects: dict[str, ObjectInfo] = {}
        for o in doc["objects"]:
            self.objects[o["key"]] = ObjectInfo(o["key"], int(o["size"]), etag=o.get("etag"),
                                                mtime_ns=o.get("mtime_ns"))
        #: episodes left out for missing source files (D40): never read
        self.skipped: dict[int, list[str]] = {
            int(s["episode_index"]): list(s.get("missing") or [])
            for s in doc.get("skipped_episodes") or [] if isinstance(s, dict)}

    @classmethod
    def load(cls, path: str) -> SourceManifest:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise UsageError(f"--source-manifest {path}: cannot read it: {e}") from None
        problem = _structure_problem(doc)
        if problem:
            raise UsageError(f"--source-manifest {path}: not a source manifest ({problem})")
        return cls(doc, path)

    def check_input(self, uri: str) -> None:
        if normalize_uri(uri) != self.input:
            raise UsageError(f"--source-manifest {self.path} was taken for {self.input}, "
                             f"not for {normalize_uri(uri)}")

    def verify(self, listing: dict[str, ObjectInfo], *, prefix: str | None = None,
               keys: Iterable[str] | None = None) -> None:
        """Raise :class:`SourceChanged` at the first object that differs.

        Scope: ``keys`` (the objects about to be read), else everything under
        ``prefix``, else the whole manifest. With a prefix or the whole
        manifest, objects that appeared since the snapshot count as changes too.
        """
        if keys is not None:
            scope = sorted(set(keys))
            added: list[str] = []
        else:
            def within(k: str) -> bool:
                return prefix is None or k.startswith(prefix)
            scope = sorted(k for k in self.objects if within(k))
            added = sorted(k for k in listing if within(k) and k not in self.objects)
        for key in scope:
            want = self.objects.get(key)
            have = listing.get(key)
            if want is None:
                raise self._changed(key, "not_in_manifest", None, have)
            if have is None:
                raise self._changed(key, "missing", want, None)
            if int(have.size) != int(want.size):
                raise self._changed(key, "size", want, have)
            if want.etag is not None and normalize_etag(want.etag) != normalize_etag(have.etag):
                raise self._changed(key, "etag", want, have)
            if want.etag is None and want.mtime_ns != have.mtime_ns:
                raise self._changed(key, "mtime", want, have)
        if added:
            raise self._changed(added[0], "added", None, listing[added[0]])

    def _changed(self, key: str, change: str, want: ObjectInfo | None,
                 have: ObjectInfo | None) -> SourceChanged:
        what = {"missing": "is gone", "size": "changed size", "etag": "was rewritten (ETag)",
                "mtime": "was modified (mtime)", "added": "appeared after the snapshot",
                "not_in_manifest": "is not in the source manifest"}[change]
        return SourceChanged(
            f"source object {key} {what}; the data no longer matches the task's source "
            f"manifest - run preflight again and create a new task",
            {"key": key, "change": change,
             "expected": entry(want) if want else None,
             "actual": entry(have) if have else None})


def _structure_problem(doc) -> str:
    if not isinstance(doc, dict):
        return "not a JSON object"
    if doc.get("schema_version") != SCHEMA_VERSION:
        return f"schema_version {doc.get('schema_version')!r} is not {SCHEMA_VERSION}"
    if not isinstance(doc.get("input"), str) or not doc["input"]:
        return "no input"
    objects = doc.get("objects")
    if not isinstance(objects, list):
        return "no objects list"
    for i, o in enumerate(objects):
        if not isinstance(o, dict) or not isinstance(o.get("key"), str) \
                or not isinstance(o.get("size"), int):
            return f"objects[{i}] needs key and size"
        if ("etag" in o) == ("mtime_ns" in o):
            return f"objects[{i}] needs exactly one of etag and mtime_ns"
    return ""


def guard(path: str | None, storage_uri: str) -> SourceManifest | None:
    """Load ``--source-manifest`` (if given) and check it belongs to this input."""
    if not path:
        return None
    manifest = SourceManifest.load(path)
    manifest.check_input(storage_uri)
    return manifest
