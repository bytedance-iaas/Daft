"""Export manifest: what the delivered dataset holds (design doc 06, section 4.3).

Two files live next to ``lerobot_curated/`` in the export directory:

* ``manifest.json`` -- the frozen C2 contract (``cli/export-manifest.schema.json``):
  one entry per delivered episode with its source ``content_key``, its ``task_key``
  and the files it lives in.  The Daemon and ``curation verify`` read it.
* ``manifest.detail.json`` -- what the incremental re-export needs and the contract
  has no room for: the task text and its source, the frame layout (global index
  offset, task_index, video windows) and the size, sha256 and a content descriptor
  of every output file.  Only this package reads it.

Both carry the same export fingerprint.  A pair whose fingerprints differ is not
from one export, and the next export rebuilds from scratch instead of trusting it.

A third file, ``_EXPORTING``, exists only while an export is changing the export
directory in place; finding it means the previous export died half way.

Everything here is pure: no dataset I/O, so the Daemon can compute the fingerprint
of "what should be delivered now" from ``passed.json`` alone (``delivery_stale``,
design doc 01, section 2.3).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
MANIFEST_NAME = "manifest.json"
DETAIL_NAME = "manifest.detail.json"
JOURNAL_NAME = "_EXPORTING"
DATASET_DIR = "lerobot_curated"
DETAIL_SCHEMA = "curation.export.detail/1"
JOURNAL_SCHEMA = "curation.export.journal/1"

#: Bumped whenever the bytes the exporter writes for the same input change (layout,
#: parquet writer, meta files).  Part of the fingerprint, so a new version makes old
#: deliveries stale and the next export rebuilds them instead of patching them.
EXPORT_IMPL_VERSION = "1"

#: mcap / lance (D44) are exported by ``export/containers.py`` (always in full)
SOURCE_FORMATS = ("lerobot_v2", "lerobot_v3", "mcap", "lance")

#: task_text.source values (common.schema.json#/$defs/task_text_source).
TASK_SOURCE_ORIGINAL = "原始标注"
TASK_SOURCE_CAPTION = "自产caption"
TASK_SOURCE_HUMAN = "人工改标"
TASK_SOURCE_NONE = "无"
TASK_SOURCES = (TASK_SOURCE_ORIGINAL, TASK_SOURCE_CAPTION, TASK_SOURCE_HUMAN, TASK_SOURCE_NONE)

#: Export parameters that change what gets written (v3 file-size thresholds).  None
#: means "the source info.json's *_files_size_in_mb, else the v1 default".
DEFAULT_PARAMS = {"video_file_mb": None, "data_file_mb": None}


# ── digests ──────────────────────────────────────────────────────────────────

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=_json_default).encode("utf-8")


def _json_default(obj: Any):
    # numpy scalars / arrays sneak in from pandas rows; digests must not depend on
    # whether a value came from JSON or from a DataFrame.
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_json(obj: Any) -> str:
    """sha256 of the canonical JSON form (sorted keys, no whitespace, UTF-8)."""
    return digest_bytes(_canonical(obj))


def task_key(tasks: Iterable[str], source: str) -> str:
    """Identity of the task text written for an episode: the texts and where they
    came from.  A human relabel changes it; so does a caption replacing nothing."""
    return digest_json({"tasks": [str(t) for t in tasks], "source": str(source)})


def normalize_params(params: dict | None) -> dict:
    out = dict(DEFAULT_PARAMS)
    for k, v in (params or {}).items():
        if k not in DEFAULT_PARAMS:
            raise ValueError(f"unknown export parameter: {k}")
        out[k] = None if v is None else float(v)
    return out


def export_fingerprint(passed: Iterable[dict], *, source_format: str, source_digest: str,
                       params: dict | None = None) -> str:
    """Fingerprint of what an export of ``passed`` delivers (design doc 06, section 4.3).

    = the passed list and its order + each episode's task text and source + the source
    digest (``source_manifest.json`` summary) + the format and export parameters.
    It differs from ``task.export_fingerprint`` exactly when the delivery is stale;
    a relabel alone makes it differ.  ``passed`` are final-list entries
    (``cli/final-list.schema.json#/$defs/entry``); only ``episode_index`` and
    ``task_text`` are read.
    """
    if source_format not in SOURCE_FORMATS:
        raise ValueError(f"unknown source format: {source_format}")
    rows = []
    for e in passed:
        tt = e.get("task_text") or {}
        rows.append([int(e["episode_index"]), tt.get("text"), tt.get("source")])
    return digest_json({"fingerprint": 1, "impl": EXPORT_IMPL_VERSION,
                        "format": source_format, "source": str(source_digest),
                        "params": normalize_params(params), "episodes": rows})


# ── model ────────────────────────────────────────────────────────────────────

@dataclass
class EpisodeEntry:
    """One delivered episode, as both files describe it."""

    episode_index: int                       # in the source dataset
    new_index: int                           # in the delivered dataset
    content_key: str
    task_key: str
    tasks: list[str]
    task_source: str
    task_index: int
    length: int
    index_from: int                          # global frame index of its first frame
    parquet: str                             # dataset-relative data file
    videos: dict[str, str] = field(default_factory=dict)   # camera -> dataset-relative mp4
    data_file: int | None = None             # v3: linear data file number
    windows: dict[str, list[float]] = field(default_factory=dict)  # v3: camera -> [from_ts, to_ts]
    video_files: dict[str, int] = field(default_factory=dict)      # v3: camera -> linear file number

    def manifest_entry(self) -> dict:
        art: dict = {"parquet": self.parquet, "videos": dict(self.videos)}
        if self.data_file is not None:
            art["chunk"] = int(self.data_file)
        out = {"episode_index": int(self.episode_index), "new_index": int(self.new_index),
               "content_key": self.content_key, "task_key": self.task_key, "artifacts": art}
        if self.task_source in TASK_SOURCES:
            # C2 1.1: the task text written for the episode and where it came from
            out["task"] = {"text": str(self.tasks[0]) if self.tasks else "",
                           "source": self.task_source}
        return out

    def detail_entry(self) -> dict:
        d = {"episode_index": int(self.episode_index), "new_index": int(self.new_index),
             "content_key": self.content_key, "task_key": self.task_key,
             "tasks": list(self.tasks), "task_source": self.task_source,
             "task_index": int(self.task_index), "length": int(self.length),
             "index_from": int(self.index_from), "parquet": self.parquet,
             "videos": dict(self.videos)}
        if self.data_file is not None:
            d["data_file"] = int(self.data_file)
            d["video_files"] = {k: int(v) for k, v in self.video_files.items()}
            d["windows"] = {k: [float(a), float(b)] for k, (a, b) in self.windows.items()}
        return d

    @classmethod
    def from_detail(cls, d: dict) -> EpisodeEntry:
        return cls(episode_index=int(d["episode_index"]), new_index=int(d["new_index"]),
                   content_key=str(d["content_key"]), task_key=str(d["task_key"]),
                   tasks=[str(t) for t in d.get("tasks") or []],
                   task_source=str(d.get("task_source") or ""),
                   task_index=int(d["task_index"]), length=int(d["length"]),
                   index_from=int(d["index_from"]), parquet=str(d["parquet"]),
                   videos={str(k): str(v) for k, v in (d.get("videos") or {}).items()},
                   data_file=None if d.get("data_file") is None else int(d["data_file"]),
                   windows={str(k): [float(v[0]), float(v[1])]
                            for k, v in (d.get("windows") or {}).items()},
                   video_files={str(k): int(v) for k, v in (d.get("video_files") or {}).items()})


@dataclass
class FileRecord:
    size: int
    sha256: str
    desc: str          # what the content was made from; same desc = same bytes

    def to_json(self) -> dict:
        return {"size": int(self.size), "sha256": self.sha256, "desc": self.desc}

    @classmethod
    def from_json(cls, d: dict) -> FileRecord:
        return cls(size=int(d["size"]), sha256=str(d["sha256"]), desc=str(d["desc"]))


@dataclass
class ExportState:
    """manifest.json + manifest.detail.json of one export."""

    source_format: str
    fingerprint: str
    episodes: list[EpisodeEntry]
    meta_files: list[str]
    files: dict[str, FileRecord]             # every file under lerobot_curated/
    codebase_version: str = ""
    params: dict = field(default_factory=dict)   # resolved export parameters
    source: dict = field(default_factory=dict)   # {"uri", "digest"}
    stats: str = "none"                      # v2 per-episode stats: source | computed | none
    task_table: list[str] = field(default_factory=list)   # meta/tasks, in task_index order

    def manifest(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "source_format": self.source_format,
                "fingerprint": self.fingerprint,
                "episodes": [e.manifest_entry() for e in self.episodes],
                "meta_files": list(self.meta_files),
                # C2 1.1: every delivered file with size and sha256, for curation verify
                "files": {k: {"size": int(v.size), "sha256": v.sha256}
                          for k, v in sorted(self.files.items())}}

    def detail(self) -> dict:
        return {"schema": DETAIL_SCHEMA, "impl": EXPORT_IMPL_VERSION,
                "fingerprint": self.fingerprint, "source_format": self.source_format,
                "codebase_version": self.codebase_version, "params": dict(self.params),
                "source": dict(self.source), "stats": self.stats,
                "task_table": list(self.task_table),
                "episodes": [e.detail_entry() for e in self.episodes],
                "files": {k: v.to_json() for k, v in sorted(self.files.items())}}

    @classmethod
    def from_documents(cls, manifest: dict, detail: dict) -> ExportState:
        problems = check_manifest(manifest)
        if problems:
            raise ValueError("manifest.json: " + "; ".join(problems[:5]))
        if detail.get("schema") != DETAIL_SCHEMA:
            raise ValueError(f"manifest.detail.json: unknown schema {detail.get('schema')!r}")
        if detail.get("fingerprint") != manifest["fingerprint"]:
            raise ValueError("manifest.json and manifest.detail.json come from different exports")
        eps = [EpisodeEntry.from_detail(d) for d in detail.get("episodes") or []]
        by_man = [(int(e["episode_index"]), int(e["new_index"]), e["content_key"], e["task_key"])
                  for e in manifest["episodes"]]
        by_det = [(e.episode_index, e.new_index, e.content_key, e.task_key) for e in eps]
        if by_man != by_det:
            raise ValueError("manifest.json and manifest.detail.json disagree on the episodes")
        return cls(source_format=manifest["source_format"], fingerprint=manifest["fingerprint"],
                   episodes=eps, meta_files=list(manifest["meta_files"]),
                   files={str(k): FileRecord.from_json(v)
                          for k, v in (detail.get("files") or {}).items()},
                   codebase_version=str(detail.get("codebase_version") or ""),
                   params=dict(detail.get("params") or {}),
                   source=dict(detail.get("source") or {}),
                   stats=str(detail.get("stats") or "none"),
                   task_table=[str(t) for t in detail.get("task_table") or []])


# ── structural check (runtime; jsonschema is a test-only dependency) ─────────

def _is_digest(v: Any) -> bool:
    if not isinstance(v, str) or not v.startswith("sha256:") or len(v) != 71:
        return False
    return all(c in "0123456789abcdef" for c in v[7:])


def check_manifest(doc: Any) -> list[str]:
    """Problems that make ``doc`` violate ``cli/export-manifest.schema.json``.

    The contract tests validate with jsonschema; this mirror keeps the runtime free
    of that dependency and is only used to refuse a damaged manifest before trusting
    it for an incremental export.
    """
    out: list[str] = []
    if not isinstance(doc, dict):
        return ["not an object"]
    required = {"schema_version", "source_format", "fingerprint", "episodes", "meta_files"}
    allowed = required | {"files", "dataset_dir"}
    out += [f"unexpected key {k}" for k in doc if k not in allowed]
    out += [f"missing {k}" for k in sorted(required) if k not in doc]
    if out:
        return out
    files = doc.get("files")
    if files is not None:
        if not isinstance(files, dict) or not all(
                isinstance(v, dict) and set(v) == {"size", "sha256"}
                and isinstance(v["size"], int) and v["size"] >= 0 and _is_digest(v["sha256"])
                for v in files.values()):
            out.append("files must map each path to {size, sha256}")
    if doc["schema_version"] != SCHEMA_VERSION:
        out.append(f"schema_version {doc['schema_version']!r}")
    if doc["source_format"] not in SOURCE_FORMATS:
        out.append(f"source_format {doc['source_format']!r}")
    if not _is_digest(doc["fingerprint"]):
        out.append("fingerprint is not a sha256 digest")
    if not isinstance(doc["meta_files"], list) or not all(
            isinstance(x, str) for x in doc["meta_files"]):
        out.append("meta_files must be a list of strings")
    if not isinstance(doc["episodes"], list):
        return out + ["episodes must be a list"]
    ekeys = {"episode_index", "new_index", "content_key", "task_key", "artifacts"}
    for i, e in enumerate(doc["episodes"]):
        where = f"episodes[{i}]"
        if not isinstance(e, dict) or not ekeys <= set(e) or not set(e) <= ekeys | {"task"}:
            out.append(f"{where}: keys must be {sorted(ekeys)} (and optionally task)")
            continue
        if "task" in e:
            t = e["task"]
            if not isinstance(t, dict) or set(t) != {"text", "source"} \
                    or not isinstance(t["text"], str) or t["source"] not in TASK_SOURCES:
                out.append(f"{where}.task must be {{text, source}} with a known source")
        for k in ("episode_index", "new_index"):
            if not isinstance(e[k], int) or isinstance(e[k], bool) or e[k] < 0:
                out.append(f"{where}.{k} must be a non-negative integer")
        for k in ("content_key", "task_key"):
            if not _is_digest(e[k]):
                out.append(f"{where}.{k} is not a sha256 digest")
        art = e["artifacts"]
        if doc["source_format"] in ("mcap", "lance"):       # D44: export/containers.py
            if not isinstance(art, dict) or not set(art) <= {"file", "videos", "windows"}:
                out.append(f"{where}.artifacts may have file, videos and windows only")
            elif doc["source_format"] == "mcap" and not isinstance(art.get("file"), str):
                out.append(f"{where}.artifacts of an mcap episode must name its file")
            elif doc["source_format"] == "lance" and not (
                    isinstance(art.get("videos"), dict) and isinstance(art.get("windows"), dict)):
                out.append(f"{where}.artifacts of a lance episode must have videos and windows")
            continue
        if not isinstance(art, dict) or not {"parquet", "videos"} <= set(art) \
                or not set(art) <= {"parquet", "videos", "chunk"}:
            out.append(f"{where}.artifacts must have parquet and videos (and optionally chunk)")
            continue
        if not isinstance(art["parquet"], str) or not isinstance(art["videos"], dict) \
                or not all(isinstance(v, str) for v in art["videos"].values()):
            out.append(f"{where}.artifacts has the wrong types")
        if "chunk" in art and (not isinstance(art["chunk"], int) or art["chunk"] < 0):
            out.append(f"{where}.artifacts.chunk must be a non-negative integer")
    return out
