"""Cleaning the local work directories of finished tasks (design doc 00 §4.2, 09 §2.1).

A run directory holds a task's results, evidence frames and logs - MB to GB on the
data volume. ``CURATOR_WORK_RETENTION_DAYS`` (7) after the task's last run ended
(the task in a terminal state, no subtask unfinished; a restore from the delivery
counts as activity too) it is removed; the delivery directory is not touched. Kept:
``.orchestr/`` with a ``cleaned.json`` mark, so a subtask that comes later knows the
task and restores what it needs from the delivery (:mod:`.backfill`). The task's
export scratch goes as well.

Before removing, whatever the delivery lacks is uploaded - the last log lines of a
run, a stopped task's partial results, decision copies written after the last
publish. Nothing is removed that the delivery does not hold: when that upload fails
(the key deleted, the bucket unreachable), or the task never had a batch, the
directory stays and the next round tries again. The one exception is a batch purged
on request (D28): its local copy goes without an upload.

Directories of tasks that are no longer in the database (purged 30 days after they
were deleted) are removed whole once they are older than the retention.
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import shutil
import threading
import typing

from ..repo import protocol as P
from .delivery import DeliveryError, sync_run_dir
from .workdir import PRIVATE, WorkDir, read_json, write_json_atomic

log = logging.getLogger("daemon.orchestr")

_TASK_DIR = re.compile(r"^task_[0-9A-Z]{26}$")
_ALL_STATES = typing.get_args(P.TaskState)


def _tree_size(path: pathlib.Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _age(ms: int) -> str:
    if ms < 3_600_000:
        return f"{ms / 60_000:.0f} min"
    if ms < 2 * 86_400_000:
        return f"{ms / 3_600_000:.1f} h"
    return f"{ms / 86_400_000:.1f} days"


def _newest_mtime_ms(path: pathlib.Path) -> int:
    """The newest modification time of ``path`` and its direct entries (and ``.orchestr/``)."""
    newest = 0.0
    for p in (path, path / PRIVATE):
        try:
            newest = max(newest, p.stat().st_mtime)
            for entry in os.scandir(p):
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
        except OSError:
            continue
    return int(newest * 1000)


class Janitor:
    """One per orchestrator; a thread that sweeps every ``janitor_interval_s``."""

    def __init__(self, orch):
        self.orch = orch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        if self.orch.cfg.work_retention_s <= 0 or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="orchestr-janitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = self.orch.cfg.janitor_interval_s
        delay = min(60.0, interval)                 # a minute after the start, then hourly
        while not self._stop.wait(delay):
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - try again next round
                log.exception("janitor: sweep failed")
            delay = interval

    # -- one round ------------------------------------------------------------------------
    def sweep_source_caches(self) -> list[str]:
        """Source caches (D44: a remote mcap / lance dataset's local copy) of tasks with no
        run going: a run removes its own when it ends; these are what a crash left."""
        root = pathlib.Path(self.orch.settings.source_cache_dir)
        try:
            names = sorted(e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False))
        except FileNotFoundError:
            return []
        out = []
        for task_id in names:
            if self._busy(task_id):
                continue
            freed = _tree_size(root / task_id)
            shutil.rmtree(root / task_id, ignore_errors=True)
            log.info("janitor: removed the source cache of task %s (%d bytes)", task_id, freed)
            out.append(task_id)
        return out

    def sweep(self, now: int | None = None) -> list[str]:
        """Clean what is due; returns the ids of the tasks whose directories went."""
        retention_ms = int(self.orch.cfg.work_retention_s * 1000)
        if retention_ms <= 0:
            return []
        try:
            self.sweep_source_caches()
        except Exception:  # noqa: BLE001 - the work directories are swept all the same
            log.exception("janitor: source caches")
        now = self.orch.clock() if now is None else int(now)
        root = pathlib.Path(self.orch.work_root)
        try:
            names = sorted(e.name for e in os.scandir(root)
                           if _TASK_DIR.match(e.name) and e.is_dir(follow_symlinks=False))
        except FileNotFoundError:
            return []
        if not names:
            return []
        tasks = {t.id: t for t in self.orch.repo.tasks_in_states(_ALL_STATES)}
        out = []
        for task_id in names:
            try:
                task = tasks.get(task_id)
                done = (self._remove_orphan(root / task_id, now, retention_ms) if task is None
                        else self._clean(task, now, retention_ms))
            except Exception:  # noqa: BLE001 - one task must not stop the round
                log.exception("janitor: task %s", task_id)
                continue
            if done:
                out.append(task_id)
        if out:
            log.info("janitor: work directories of %d task(s) cleaned", len(out))
        return out

    def _busy(self, task_id: str) -> bool:
        sched = self.orch.scheduler
        if any(job.task_id == task_id for job in sched.jobs()):
            return True
        if any(key[0] == task_id for key in sched.queued()):
            return True
        return self.orch.repo.active_subtask(task_id) is not None

    def _last_activity(self, task: P.Task, wd: WorkDir) -> int | None:
        ends = [task.finished_at] + [s.finished_at for s in self.orch.repo.list_subtasks(task.id)]
        restored = read_json(wd.restored_mark, None)
        if isinstance(restored, dict) and isinstance(restored.get("at"), int):
            ends.append(restored["at"])
        ends = [int(t) for t in ends if t]
        return max(ends) if ends else None

    def _clean(self, task: P.Task, now: int, retention_ms: int) -> bool:
        if task.state not in P.TERMINAL_STATES or self._busy(task.id):
            return False
        wd = WorkDir(self.orch.work_root, task.id)
        if wd.cleaned_mark.is_file() and not self._content(wd):
            return False                                    # cleaned before, nothing back
        last = self._last_activity(task, wd)
        if last is None or now - last < retention_ms:
            return False
        with self.orch.restorer.lock(task.id):
            if self._busy(task.id):                         # a subtask came meanwhile
                return False
            purged = wd.purged_mark.is_file()
            if not purged and not self._final_sync(task, wd):
                return False                                # never remove the only copy
            freed = 0
            for name in self._content(wd):
                path = wd.root / name
                if path.is_dir() and not path.is_symlink():
                    freed += _tree_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        freed += path.lstat().st_size
                        path.unlink()
                    except FileNotFoundError:
                        pass
            wd.restored_mark.unlink(missing_ok=True)
            write_json_atomic(wd.cleaned_mark, {"at": now, "bytes": freed, "purged": purged})
            self._drop_scratch(task.id)
        log.info("janitor: task %s ended %s ago; its local work directory was cleaned "
                 "(%d bytes)%s", task.id, _age(now - last), freed,
                 " (its batch had been purged)" if purged else "")
        return True

    @staticmethod
    def _content(wd: WorkDir) -> list[str]:
        try:
            return sorted(e.name for e in os.scandir(wd.root) if e.name != PRIVATE)
        except FileNotFoundError:
            return []

    def _final_sync(self, task: P.Task, wd: WorkDir) -> bool:
        """Upload what the delivery lacks; True when the delivery holds it all."""
        if not task.run_id or not task.output_uri:
            log.warning("janitor: task %s ended long ago but has no batch to hold its work "
                        "directory; kept", task.id)
            return False
        try:
            with self.orch.restorer.open(task) as d:
                sync_run_dir(d, task.run_id, wd.root, wd.sync_state)
            return True
        except (DeliveryError, OSError) as err:
            log.warning("janitor: task %s - the last upload to %s/%s failed, its work "
                        "directory is kept for now: %s", task.id, task.output_uri.rstrip("/"),
                        task.run_id, err)
            return False

    def _remove_orphan(self, path: pathlib.Path, now: int, retention_ms: int) -> bool:
        """A directory whose task is gone from the database (purged after deletion)."""
        if now - _newest_mtime_ms(path) < retention_ms:
            return False
        freed = _tree_size(path)
        shutil.rmtree(path, ignore_errors=True)
        self._drop_scratch(path.name)
        log.info("janitor: removed %s (%d bytes): its task is no longer in the database",
                 path, freed)
        return True

    def _drop_scratch(self, task_id: str) -> None:
        scratch = pathlib.Path(self.orch.settings.scratch_dir) / task_id
        shutil.rmtree(scratch, ignore_errors=True)
