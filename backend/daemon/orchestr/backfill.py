"""Bringing a cleaned run directory back from the delivery (design doc 00 §4.2).

7 days after a task ended the janitor removes its local run directory, all but
``.orchestr/`` (:mod:`.janitor`). What was delivered stays in
``<delivery>/<run_id>/`` and comes back when it is needed again:

* a subtask that comes days later (retry, continue, adjudication, re-export)
  restores it before its first command (``Run.ensure_local``);
* a result reader that finds no committed revision - W5b's ``ResultStore.backfill``
  hook, installed by the orchestrator: the report of an old task opens as before.

A restore fetches only files missing locally and never overwrites one: nothing
local is older than what was delivered (the ``human-decisions/`` copies written
after the last sync, for one). It leaves the big files in the delivery
(``delivery.RESTORE_SKIP``, v1's mirror skip table): nothing local reads them. What
it fetched is recorded as synced, so the next publish does not upload it again.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from ..repo import protocol as P
from ..secrets import Unavailable
from .delivery import DeliveryError, mark_synced, open_delivery, restorable
from .workdir import WorkDir, write_json_atomic

log = logging.getLogger("daemon.orchestr")

#: after a failed restore for a reader, readers do not try again for this long
READER_RETRY_S = 60.0


class Restorer:
    """One per orchestrator: restores, and the lock the janitor shares with them."""

    def __init__(self, orch):
        self.orch = orch
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._failed: dict[str, float] = {}

    def lock(self, task_id: str) -> threading.Lock:
        """Held while a task's run directory is being restored or cleaned."""
        with self._guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def needed(self, task: P.Task) -> bool:
        """Whether the run directory lacks what a subtask builds on."""
        wd = WorkDir(self.orch.work_root, task.id)
        if wd.cleaned_mark.is_file():
            return True
        rev = int(task.result_rev or 0)
        return rev > 0 and not (wd.revision_dir(rev) / "commit.json").is_file()

    def open(self, task: P.Task):
        """``with restorer.open(task) as d:`` - the task's delivery directory (output key)."""
        cfg, svc = self.orch.cfg, self.orch.svc
        key = None
        if cfg.local_delivery_root is None:
            try:
                key = svc.tos_key(task.output_cred_id, owner=task.owner_id, role="output")
            except Unavailable as err:
                raise DeliveryError(err.message_zh, err.code) from None
        region = task.output_region or (key.region if key is not None else None)
        return open_delivery(task.output_uri, local_root=cfg.local_delivery_root, svc=svc,
                             key=key, region=region)

    def restore(self, task: P.Task, *, check_stop: Callable[[], None] | None = None,
                progress: Callable[[int, int], None] | None = None) -> int:
        """Fetch what the run directory lacks from ``<delivery>/<run_id>/``; how many files."""
        if not task.run_id or not task.output_uri:
            return 0
        wd = WorkDir(self.orch.work_root, task.id)
        with self.lock(task.id):
            fetched: list[str] = []
            try:
                with self.open(task) as d:
                    cut = len(task.run_id) + 1
                    todo = [rel for rel in (k[cut:] for k in sorted(d.list(task.run_id)))
                            if rel and restorable(rel) and not (wd.root / rel).exists()]
                    for n, rel in enumerate(todo, 1):
                        if check_stop is not None:
                            check_stop()
                        d.get_file(f"{task.run_id}/{rel}", wd.root / rel)
                        fetched.append(rel)
                        if progress is not None:
                            progress(n, len(todo))
            finally:
                if fetched:
                    wd.ensure()
                    mark_synced(wd.sync_state, task.run_id, wd.root, fetched)
            if fetched or wd.cleaned_mark.is_file():
                # the janitor counts the retention from here again, instead of cleaning
                # what was just fetched in its next round
                write_json_atomic(wd.restored_mark, {"at": self.orch.clock(),
                                                     "files": len(fetched)})
            wd.cleaned_mark.unlink(missing_ok=True)
        if fetched:
            self._copy_decisions(task)
        return len(fetched)

    def _copy_decisions(self, task: P.Task) -> None:
        """The delivered ``human-decisions/`` may predate the last decisions: the database
        is the authority, so the copies are written again from it (W5b's writer)."""
        from ..results import store_of, write_copies

        try:
            if self.orch.repo.latest_adjudications(task.id):
                write_copies(store_of(self.orch.rt), self.orch.repo, task)
        except Exception:  # noqa: BLE001 - a copy; the database has the decisions
            log.warning("task %s: human-decisions/ not rewritten after the restore", task.id,
                        exc_info=True)

    def hook(self, task: P.Task, missing: str) -> bool:
        """W5b's ``ResultStore.backfill(task, missing)``: True when ``missing`` is back."""
        if not task.run_id:
            return False
        with self._guard:
            failed_at = self._failed.get(task.id)
        if failed_at is not None and time.monotonic() - failed_at < READER_RETRY_S:
            return False
        try:
            n = self.restore(task)
        except (DeliveryError, OSError) as err:
            with self._guard:
                self._failed[task.id] = time.monotonic()
            log.warning("task %s: restoring its run directory from %s/%s failed: %s", task.id,
                        task.output_uri.rstrip("/"), task.run_id, err)
            return False
        with self._guard:
            self._failed.pop(task.id, None)
        if n:
            log.info("task %s: %d file(s) restored from the delivery for a reader", task.id, n)
        return (WorkDir(self.orch.work_root, task.id).root / missing).is_file()
