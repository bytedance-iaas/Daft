"""A task's work directory and the orchestration's own files in it (design doc 00 §4.2).

``<CURATOR_WORK_DIR>/<task_id>/`` is the CLI's ``--run-dir`` (the run directory of
``backend/curation/cli/README.md``) and the root of the delivered batch: what is in
it (minus work in progress) is synced to ``<delivery>/<run_id>/``. The task logs
live in its ``logs/`` (``daemon.logs.TaskLogs``).

The orchestration keeps its private state under ``.orchestr/`` - hidden, so it is
neither delivered nor verified (``curation verify`` skips hidden files):

* ``start.json`` - written when the start procedure froze the task's inputs; the
  worker pool only runs tasks that have it;
* ``<run>.json`` - the journal of one run (``main`` or a subtask id): which stages
  are done, the result revision it builds, what it published;
* ``episodes.sqlite3`` - per-episode funnel records and next stage for the main
  run's bounded batch pipeline; updated after each completed episode;
* ``<run>/`` - the episode lists handed between stages (``@file`` arguments), the
  decisions of an adjudication run;
* ``sync.json`` - what has been uploaded to the delivery, so a sync only sends
  what changed;
* ``cleaned.json`` - the rest of the directory was removed 7 days after the task
  ended (:mod:`.janitor`); what was delivered comes back on demand (:mod:`.backfill`,
  which leaves ``restored.json``). ``.orchestr/`` itself stays: it is small and lets
  a later subtask go on;
* ``purged.json`` - the batch was deleted from the delivery on request (D28): no
  sync puts it back until the next publish does so on purpose.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any, Iterable

PRIVATE = ".orchestr"
START_MARK = "start.json"
SYNC_STATE = "sync.json"
CLEANED_MARK = "cleaned.json"
RESTORED_MARK = "restored.json"
PURGED_MARK = "purged.json"


def write_json_atomic(path: pathlib.Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: pathlib.Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_lines(path: pathlib.Path, episodes: Iterable[int]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text("".join(f"{int(e)}\n" for e in sorted(set(int(x) for x in episodes))),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_lines(path: pathlib.Path) -> list[int] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.isdigit():
            out.append(int(line))
    return sorted(set(out))


class WorkDir:
    def __init__(self, work_root: os.PathLike | str, task_id: str):
        self.task_id = task_id
        self.root = pathlib.Path(work_root) / task_id
        self.private = self.root / PRIVATE

    # -- the run directory (shared with the CLI) -------------------------------------
    @property
    def preflight(self) -> pathlib.Path:
        return self.root / "preflight.json"

    @property
    def plan(self) -> pathlib.Path:
        return self.root / "plan.json"

    @property
    def manifest(self) -> pathlib.Path:
        return self.root / "source_manifest.json"

    @property
    def run_json(self) -> pathlib.Path:
        return self.root / "run.json"

    def revision_dir(self, revision: int) -> pathlib.Path:
        return self.root / "revisions" / f"r{int(revision):04d}"

    # -- the orchestration's own files --------------------------------------------------
    @property
    def start_mark(self) -> pathlib.Path:
        return self.private / START_MARK

    @property
    def sync_state(self) -> pathlib.Path:
        return self.private / SYNC_STATE

    @property
    def cleaned_mark(self) -> pathlib.Path:
        return self.private / CLEANED_MARK

    @property
    def restored_mark(self) -> pathlib.Path:
        return self.private / RESTORED_MARK

    @property
    def purged_mark(self) -> pathlib.Path:
        return self.private / PURGED_MARK

    def journal(self, run: str) -> pathlib.Path:
        return self.private / f"{run}.json"

    def run_dir(self, run: str) -> pathlib.Path:
        return self.private / run

    def episodes_file(self, run: str, name: str) -> pathlib.Path:
        return self.run_dir(run) / f"{name}.txt"

    def ensure(self) -> None:
        self.private.mkdir(parents=True, exist_ok=True)

    def started(self) -> bool:
        return self.start_mark.is_file()


class Journal:
    """The state of one run on disk; every change is written at once (atomic replace)."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self._lock = threading.RLock()
        data = read_json(path, None)
        self.data: dict = data if isinstance(data, dict) else {}
        self.data.setdefault("stages", {})

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def set(self, **fields: Any) -> None:
        with self._lock:
            self.data.update(fields)
            write_json_atomic(self.path, self.data)

    def stage(self, stage_id: str) -> dict:
        with self._lock:
            return dict(self.data["stages"].get(stage_id) or {})

    def mark(self, stage_id: str, **fields: Any) -> None:
        with self._lock:
            entry = self.data["stages"].setdefault(stage_id, {})
            entry.update(fields)
            write_json_atomic(self.path, self.data)

    def done(self, stage_id: str) -> bool:
        return self.stage(stage_id).get("done") is True

    def forget(self, *stage_ids: str) -> None:
        with self._lock:
            for s in stage_ids:
                self.data["stages"].pop(s, None)
            write_json_atomic(self.path, self.data)
