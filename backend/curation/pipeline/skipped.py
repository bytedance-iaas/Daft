"""Episodes left out because their source files are missing (D40, as v1 does).

v1 reads LeRobot v2 datasets with ``skip_missing=True``: an episode whose data
parquet or any camera's video is not there (``lerobot_reader._v2_missing``) is
dropped before anything judges it - it is in no list and not in any count.
LeRobot v3 episodes are never dropped this way (v1 does not check them).

v2 does the same, and says so: ``snapshot`` records such episodes in the source
manifest's ``skipped_episodes`` (commands given the manifest never read them);
without a manifest, ``check`` finds them when it reads and lists them in its
output. Both end up here, per run directory: aggregate leaves them out of the
lists and the totals, the report lists them under ``integrity.skipped_episodes``.
The shape is ``common.schema.json#/$defs/skipped_episodes``.

Unless data integrity judges them (D97, design doc 29 §3): a run with data
integrity selected (``run.json``'s ``missing_sources`` is ``judged``) keeps them,
data integrity rejects each on the files it lacks, and an episode it has a record
of is never left out - whatever the manifest says.
"""
from __future__ import annotations

import json
import os
import threading

from .records import SOURCE_MANIFEST_NAME, write_json_atomic

#: read-time finds of the run directory's check calls (no manifest named them)
SKIPPED_FILE = "skipped_episodes.json"
_LOCK = threading.Lock()


def as_list(found: dict[int, list[str]]) -> list[dict]:
    """``{episode: missing keys}`` -> the contract's list, by episode."""
    return [{"episode_index": int(e), "missing": sorted(set(keys))}
            for e, keys in sorted(found.items()) if keys]


def of_list(items) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for it in items or []:
        if isinstance(it, dict) and "episode_index" in it:
            out[int(it["episode_index"])] = [str(k) for k in it.get("missing") or []]
    return out


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def by_manifest(run_dir: str) -> dict[int, list[str]]:
    """``skipped_episodes`` of the run directory's ``source_manifest.json``."""
    return of_list(_read(os.path.join(run_dir, SOURCE_MANIFEST_NAME)).get("skipped_episodes"))


def at_read_time(run_dir: str) -> dict[int, list[str]]:
    return of_list(_read(os.path.join(run_dir, SKIPPED_FILE)).get("skipped_episodes"))


def record(run_dir: str, found: dict[int, list[str]]) -> None:
    """Add read-time finds to the run directory's list (merged by episode)."""
    if not found:
        return
    with _LOCK:
        cur = at_read_time(run_dir)
        for e, keys in found.items():
            cur[int(e)] = sorted(set(cur.get(int(e), [])) | set(keys))
        write_json_atomic(os.path.join(run_dir, SKIPPED_FILE),
                          {"skipped_episodes": as_list(cur)})


def all_skipped(run_dir: str) -> dict[int, list[str]]:
    """The manifest's list and the read-time finds together, without the episodes data integrity judged (D97)."""
    out = by_manifest(run_dir)
    for e, keys in at_read_time(run_dir).items():
        out[e] = sorted(set(out.get(e, [])) | set(keys))
    if out:
        judged = judged_by_integrity(run_dir)
        out = {e: keys for e, keys in out.items() if e not in judged}
    return out


#: run.json's word for what a run does with the episodes whose source files are missing (D97)
JUDGED, LEFT_OUT = "judged", "left_out"


def judges_missing(run_dir: str) -> bool | None:
    """D97: whether this run judges the episodes whose source files are missing (data integrity selected), from
    ``run.json``'s ``missing_sources``; None without a run.json (a run by hand) or with one from before D97."""
    doc = _read(os.path.join(run_dir, "run.json"))
    return {JUDGED: True, LEFT_OUT: False}.get(doc.get("missing_sources"))


def judged_by_integrity(run_dir: str) -> set[int]:
    """The episodes data integrity has a record of in this run directory (an error included): not left out."""
    from .records import latest_results

    return set(latest_results(run_dir, "data_integrity"))


def relative_key(input_dir: str, path: str) -> str:
    """A path under the input -> its object key (``data/chunk-000/episode_000004.parquet``)."""
    base = str(input_dir).rstrip("/")
    p = str(path)
    if p.startswith(base + "/"):
        return p[len(base) + 1:]
    return os.path.relpath(p, base).replace(os.sep, "/") if not p.startswith("tos://") else p


