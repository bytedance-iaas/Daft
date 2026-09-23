"""The delivered dataset of an mcap / lance source (D44; v1's PR #155).

The same passed list as a LeRobot export (``passed.json`` of the revision: the episodes
waiting for a human are delivered, the held ones are not), delivered the way v1 does it:

* **mcap** -> ``export/mcap_curated/``: v1's ``export/mcap_writer.export_mcap_curated``,
  unchanged - every passed episode's ``.mcap`` copied byte for byte (renamed
  ``episode_<N>.mcap`` when the source names do not follow that pattern) and
  ``index.json`` listing each episode with its task text and its source; a relabel or a
  caption goes into ``index.json`` only, never into the file (the writer says why).
* **lance** -> ``export/lance_episodes/``: v1 has no lance-native delivery yet, so it is
  v1's ``episodes_parquet`` - the rows of the passed episodes (``read_lance_rows``) with the
  delivered task text written into ``instruction`` / ``instruction_source`` - under
  ``episodes_parquet/``, their videos (``persist_videos``) under ``videos/``, the video
  pointers rewritten to where the videos are delivered, and an ``index.json`` that says the
  native lance delivery is not done.

Every export of these formats is a full one: the incremental re-export (``incremental.py``)
knows LeRobot's file layouts only. ``--incremental`` therefore says so in ``full_reason``;
files whose bytes did not change are still not uploaded again (``written`` lists only the
changed ones, compared with the previous export's ``manifest.detail.json``).

``manifest.json`` (``cli/export-manifest.schema.json``, C2) names the dataset directory
(``dataset_dir``) so ``verify``, the Daemon's sync and its video lookup find the files.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field

from .diff import EpisodeKey, diff_episodes
from .manifest import (DETAIL_NAME, MANIFEST_NAME, SCHEMA_VERSION, digest_json,
                       export_fingerprint, task_key)
from .source import ExportError, ExportInputError

DATASET_DIRS = {"mcap": "mcap_curated", "lance": "lance_episodes"}
DETAIL_SCHEMA = "curation.export.detail/container-1"
INCREMENTAL_NOTE = ("incremental re-export covers LeRobot datasets only; mcap and lance "
                    "datasets are exported in full every time")
LANCE_NOTE = ("lance 原格式交付本版本未做：交付的是 episodes_parquet（轨迹级数值与任务文本）"
              "和视频，判决清单见本结果版本的 passed / reject / held")
#: the task text sources written into the delivery (common.schema task_text_source)
_RELABEL = ("自产caption", "人工改标")


@dataclass
class ContainerOutcome:
    """What ``curation export`` prints (``result``) and uploads: ``written`` are paths
    relative to ``export/<dataset_dir>/``, ``files`` every delivered file."""

    result: dict
    dataset_dir: str
    files: dict = field(default_factory=dict)
    written: list[str] = field(default_factory=list)
    renamed: list = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _lists(rev_dir: str) -> dict:
    passed = _read_json(os.path.join(rev_dir, "passed.json"))
    if passed.get("list") != "passed":
        raise ExportInputError(f"{rev_dir}/passed.json is not a passed list")
    held_path = os.path.join(rev_dir, "held.json")
    held = set()
    if os.path.isfile(held_path):
        doc = _read_json(held_path)
        if doc.get("list") != "held":
            raise ExportInputError(f"{held_path} is not a held list")
        held = {int(e["episode_index"]) for e in doc.get("episodes") or []}
    both = sorted(held & {int(e["episode_index"]) for e in passed.get("episodes") or []})
    if both:
        raise ExportInputError(f"episodes both passed and held in {rev_dir}: {both[:8]}")
    return passed


def _swap(staging: str, final: str) -> None:
    """The new dataset directory replaces the old one only once it is complete."""
    old = f"{final}.old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(final):
        os.rename(final, old)
    os.rename(staging, final)
    shutil.rmtree(old, ignore_errors=True)


def _files(root: str) -> dict:
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = {"size": os.path.getsize(full), "sha256": _sha256(full)}
    return out


def export_container(run_dir: str, fmt: str, input_dir: str, *, revision: int | None = None,
                     incremental: bool = False, source_digest: str = "",
                     content_of=None, fetch=None, output_uri: str | None = None,
                     log=None, progress=None) -> ContainerOutcome:
    """Export the passed list of a revision of an mcap / lance run into ``<run-dir>/export``.

    ``input_dir``: the directory v1's reader gets (the dataset, or its local copy);
    ``fetch(episodes)`` makes a remote dataset's episodes local first; ``content_of(ep)``
    identifies an episode's source objects (the listing's size and ETag); ``output_uri``:
    the task's directory in the delivery, where the lance video pointers point."""
    from .incremental import COMPLETE_MARKER, EXPORT_DIR, resolve_revision_dir

    log = log or (lambda level, msg: None)
    if fmt not in DATASET_DIRS:
        raise ExportError(f"not a container format: {fmt}")
    rev_dir = resolve_revision_dir(run_dir, revision)
    passed = _lists(rev_dir)
    entries = list(passed.get("episodes") or [])
    complete = os.path.join(run_dir, COMPLETE_MARKER)
    if os.path.exists(complete):
        os.remove(complete)
    export_dir = os.path.join(run_dir, EXPORT_DIR)
    os.makedirs(export_dir, exist_ok=True)
    name = DATASET_DIRS[fmt]
    final = os.path.join(export_dir, name)
    staging = os.path.join(export_dir, f"{name}.staging")
    shutil.rmtree(staging, ignore_errors=True)
    keep = [int(e["episode_index"]) for e in entries]
    task = {int(e["episode_index"]): (e.get("task_text") or {}) for e in entries}
    if fetch is not None and keep:
        fetch(keep)
    if progress:
        progress(0, max(1, len(keep)))
    try:
        if fmt == "mcap":
            artifacts = _mcap(export_dir, name, staging, input_dir, keep, task)
            copied = 0
        else:
            artifacts, copied = _lance(staging, name, input_dir, keep, task, output_uri, final)
    except (ExportError, ExportInputError):
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001 - the v1 writers' errors are many
        shutil.rmtree(staging, ignore_errors=True)
        raise ExportError(f"{fmt} export failed: {type(e).__name__}: {e}") from e

    previous = {}
    try:
        detail = _read_json(os.path.join(export_dir, DETAIL_NAME))
        if detail.get("dataset_dir") == name:
            previous = {k: v.get("sha256") for k, v in (detail.get("files") or {}).items()}
        old_manifest = _read_json(os.path.join(export_dir, MANIFEST_NAME))
    except (OSError, ValueError):
        old_manifest = None
    files = _files(staging)
    _swap(staging, final)
    written = sorted(rel for rel, rec in files.items() if previous.get(rel) != rec["sha256"])
    deleted = sorted(rel for rel in previous if rel not in files)

    fingerprint = export_fingerprint(entries, source_format=fmt, source_digest=source_digest)
    episodes = []
    for i, e in enumerate(entries):
        idx = int(e["episode_index"])
        t = task[idx]
        text, source = str(t.get("text") or ""), str(t.get("source") or "无")
        episodes.append({
            "episode_index": idx, "new_index": i,
            "content_key": digest_json({"format": fmt, "episode": idx,
                                        "source": content_of(idx) if content_of else None}),
            "task_key": task_key([text], source), "task": {"text": text, "source": source},
            "artifacts": artifacts[idx]})
    manifest = {"schema_version": SCHEMA_VERSION, "source_format": fmt,
                "fingerprint": fingerprint, "episodes": episodes, "meta_files": [],
                "files": files, "dataset_dir": name}
    detail = {"schema": DETAIL_SCHEMA, "fingerprint": fingerprint, "source_format": fmt,
              "dataset_dir": name, "source": {"digest": source_digest}, "files": files,
              "episodes": [{"episode_index": ep["episode_index"], "new_index": ep["new_index"],
                            "task": ep["task"]} for ep in episodes]}
    from .safe_write import write_json

    write_json(os.path.join(export_dir, DETAIL_NAME), detail)
    write_json(os.path.join(export_dir, MANIFEST_NAME), manifest)      # the commit point
    new_keys = [EpisodeKey(ep["episode_index"], ep["new_index"], ep["content_key"],
                           ep["task_key"]) for ep in episodes]
    old_keys = None
    if isinstance(old_manifest, dict) and old_manifest.get("source_format") == fmt:
        old_keys = [EpisodeKey(int(ep["episode_index"]), int(ep["new_index"]),
                               ep["content_key"], ep["task_key"])
                    for ep in old_manifest.get("episodes") or []]
    counts = diff_episodes(old_keys, new_keys).counts()
    if progress:
        progress(len(keep), max(1, len(keep)))
    log("info", f"exported {len(entries)} episode(s) of the {fmt} dataset into export/{name}/: "
                + ", ".join(f"{k} {v}" for k, v in counts.items())
                + f"; {len(written)} file(s) written, {len(deleted)} gone")
    result = {"schema_version": SCHEMA_VERSION, "format": fmt, "incremental": False,
              "episodes": len(entries), "diff": counts, "fingerprint": fingerprint,
              "manifest": f"{EXPORT_DIR}/{MANIFEST_NAME}", "videos_copied": copied,
              "videos_reencoded": 0, "videos_renamed": 0,
              "full_reason": INCREMENTAL_NOTE if incremental else None,
              "dataset_dir": f"{EXPORT_DIR}/{name}"}
    if fmt == "lance":
        result["note"] = LANCE_NOTE
    return ContainerOutcome(result=result, dataset_dir=name, files=files, written=written,
                            deleted=deleted)


def _mcap(export_dir: str, name: str, staging: str, input_dir: str, keep: list[int],
          task: dict) -> dict:
    from .mcap_writer import INDEX_NAME, export_mcap_curated

    keep_ids = [f"ep{i:06d}" for i in keep]
    texts = {f"ep{i:06d}": task[i] for i in keep}
    # v1 (run.py / rejudge.py): a caption or a human label goes into index.json, the file
    # stays byte for byte the source's
    relabels = {eid: str(t.get("text") or "") for eid, t in texts.items()
                if t.get("source") in _RELABEL and str(t.get("text") or "").strip()}
    episodes = {eid: {"verdict": "通过", "instruction": str(t.get("text") or ""),
                      "instruction_source": str(t.get("source") or "")}
                for eid, t in texts.items()}
    export_mcap_curated(export_dir, input_dir, keep_ids, relabels=relabels, episodes=episodes,
                        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        out_name=os.path.basename(staging))
    index = _read_json(os.path.join(staging, INDEX_NAME))
    by_id = {rec["episode_id"]: rec for rec in index.get("episodes") or []}
    return {i: {"file": by_id[f"ep{i:06d}"]["file"]} for i in keep}


def _lance(staging: str, name: str, input_dir: str, keep: list[int], task: dict,
           output_uri: str | None, final: str) -> tuple[dict, int]:
    from ..ingest.lance_reader import persist_videos, read_lance_rows
    from ..pipeline.rows import index_of
    from .safe_write import write_json
    from .writers import write_episodes_parquet

    os.makedirs(staging, exist_ok=True)
    rows = read_lance_rows(input_dir, episode_indices=set(keep)) if keep else []
    rows.sort(key=lambda r: keep.index(index_of(r["episode_id"])))
    for r in rows:                      # v1's delivery backfill, with v2's task texts
        t = task[index_of(r["episode_id"])]
        r["instruction"] = str(t.get("text") or "")
        r["instruction_source"] = str(t.get("source") or "无")
    videos_dir = os.path.join(staging, "videos")
    copied = persist_videos(rows, videos_dir)
    base = (f"{output_uri.rstrip('/')}/export/{name}" if output_uri else final)
    artifacts: dict[int, dict] = {}
    for r in rows:
        vids, windows = {}, {}
        for cam, v in (r.get("video") or {}).items():
            rel = f"videos/{os.path.basename(str(v['path']))}"
            v["path"] = f"{base}/{rel}"         # where the delivery keeps it
            vids[cam] = rel
            windows[cam] = [float(v["from_ts"]), float(v["to_ts"])]
        artifacts[index_of(r["episode_id"])] = {"videos": vids, "windows": windows}
    if rows:
        write_episodes_parquet(rows, os.path.join(staging, "episodes_parquet"))
    write_json(os.path.join(staging, "index.json"), {
        "source_format": "lance", "n_kept": len(rows),
        "说明": LANCE_NOTE + "。episodes_parquet 的视频指针指向交付目录里的 videos/。",
        "episodes": [{"episode_id": r["episode_id"], "instruction": r["instruction"],
                      "instruction_source": r["instruction_source"]} for r in rows]})
    missing = [i for i in keep if i not in artifacts]
    if missing:
        raise ExportError(f"lance: episodes {missing[:8]} were not read from the source")
    return artifacts, copied
