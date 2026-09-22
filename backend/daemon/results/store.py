"""Where a task's results live, and which revision a request reads (design docs 00 §4.2, 06 §1).

A task's run directory is ``CURATOR_WORK_DIR/<task_id>/`` on the data volume; the
orchestration (W5a) runs the CLI in it and syncs it to ``<delivery>/<run_id>/``.
The readers here only read the local copy. When it is gone (cleaned 7 days after
the task ended) or lacks the revision asked for, :attr:`ResultStore.backfill` - a
hook the orchestration installs - may bring the files back; without it the request
answers 404 ``not_found`` with ``details.reason`` saying what is missing.

Revision rules: ``task.result_rev`` is the current revision; ``?rev=N`` reads any
committed revision from 1 to it; a revision that is missing locally or has no
``commit.json`` is 404, and so is one above ``result_rev`` (committed, but not
switched to yet).
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
from pathlib import Path
from typing import Callable, ContextManager, Iterator

from ..errors import ApiError
from ..repo import protocol as P
from .files import LRU
from .revision import COMMIT_NAME, Revision, revision_rel

log = logging.getLogger("daemon.results")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: ``backfill(task, missing)`` -> True when it restored files (``missing`` is the relative
#: path that was looked for, e.g. ``revisions/r0002/commit.json``).
Backfill = Callable[[P.Task, str], bool]
#: ``open_input(task)`` -> a context manager yielding a ``curation.cli.storage.Storage``
#: of the task's input dataset (videos of rejected episodes only exist there).
InputOpener = Callable[[P.Task], ContextManager]

_CREATE_LOCK = threading.Lock()


class ResultStore:
    """The result readers' state: caches and the two hooks. One per Daemon (:func:`store_of`)."""

    def __init__(self, work_dir: Path | str, *, open_input: InputOpener | None = None):
        self.work_dir = Path(work_dir)
        self.backfill: Backfill | None = None
        self.open_input: InputOpener | None = open_input
        self.docs = LRU(max_items=256)                          # parsed JSON documents
        self.derived = LRU(max_items=256)                       # indexes built from them
        self.tables = LRU(max_items=64, max_bytes=256 << 20)    # Arrow row groups, by bytes
        self.sources = LRU(max_items=8)                         # input dataset video indexes

    # -- directories ----------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        if not _NAME_RE.match(task_id or ""):
            raise ApiError("not_found")
        return self.work_dir / task_id

    def _backfilled(self, task: P.Task, missing: str) -> bool:
        if self.backfill is None:
            return False
        try:
            return bool(self.backfill(task, missing))
        except Exception:  # noqa: BLE001 - a failed backfill is reported as the gap it is
            log.exception("backfill of %s for task %s failed", missing, task.id)
            return False

    # -- revisions --------------------------------------------------------------------
    def revision(self, task: P.Task, rev: int | None = None, *, backfill: bool = True) -> Revision:
        """The revision a request reads: ``rev``, or the task's current one.

        ``backfill=False`` never calls the hook - for callers inside a repository
        transaction, where the hook must not run (it may use the repository itself).
        """
        current = int(task.result_rev or 0)
        if current < 1:
            raise ApiError("not_found", "这个任务还没有结果：第一个结果版本提交之后才有报告和裁决队列",
                           details={"result_rev": current})
        number = current if rev is None else int(rev)
        if not 1 <= number <= current:
            raise ApiError("not_found",
                           f"没有结果版本 r{number}：这个任务的结果版本是 r1 到 r{current}",
                           details={"revision": number, "result_rev": current})
        run_dir = self.task_dir(task.id)
        missing = f"{revision_rel(number)}/{COMMIT_NAME}"
        if not (run_dir / missing).is_file() and not (
                backfill and self._backfilled(task, missing) and (run_dir / missing).is_file()):
            if not run_dir.is_dir():
                raise ApiError("not_found",
                               f"这个任务的本地工作目录已不在（任务结束 7 天后会清理），结果版本 "
                               f"r{number} 要先从交付目录取回才能查看",
                               details={"revision": number, "reason": "run_dir_missing"})
            raise ApiError("not_found",
                           f"结果版本 r{number} 在本地工作目录里不完整（没有 commit.json），读不了",
                           details={"revision": number, "reason": "revision_missing"})
        return Revision(self, task, number, run_dir)

    def current(self, task: P.Task, *, backfill: bool = True) -> Revision | None:
        """The current revision, or None when the task has none yet."""
        if int(task.result_rev or 0) < 1:
            return None
        return self.revision(task, backfill=backfill)


def default_input_opener(runtime) -> InputOpener:
    """Reads the input dataset with the task's input key (W8), anonymously for the public
    cache bucket, or from the local path of the experimental local source."""

    @contextlib.contextmanager
    def open_input(task: P.Task) -> Iterator:
        from curation.cli.storage import LocalStorage, TosStorage

        if task.input_source == "local":
            yield LocalStorage(task.input_uri)
            return
        from ..secrets import service_of

        svc = service_of(runtime)
        key = None
        if task.input_source != "public":
            key = svc.tos_key(task.input_cred_id, owner=task.owner_id, role="input")
        region = task.input_region or (key.region if key is not None else None)
        with svc.tos(key, region) as (client, ends):
            yield TosStorage(task.input_uri, client, ends.region, role="input")

    return open_input


def store_of(runtime) -> ResultStore:
    """The runtime's store, created on first use (``runtime.results``)."""
    store = getattr(runtime, "results", None)
    if store is None:
        with _CREATE_LOCK:
            store = getattr(runtime, "results", None)
            if store is None:
                store = ResultStore(runtime.settings.work_dir,
                                    open_input=default_input_opener(runtime))
                runtime.results = store
    return store
