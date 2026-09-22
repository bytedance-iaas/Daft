"""Token usage from C3 ``usage`` lines into the two ledgers (design doc 01 §2.6, 04 §5).

A task makes tens of thousands of model requests; writing each one would flood the
single SQLite writer. So increments are added up in memory per bucket - (ledger,
subtask, module, call kind, model) - and written with one ``add_usage`` about every
5 seconds and at the end of every stage (UPSERT that accumulates). The two ledgers
are kept apart and never added together: the task's totals (and the SSE ``usage``
event, cumulative) come from the ``actual`` ledger only.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from ..repo import protocol as P
from .c3 import USAGE_COUNTERS

log = logging.getLogger("daemon.exec")

FLUSH_EVERY_S = 5.0

_Key = tuple[str, str, str, str, str]      # ledger, subtask, module, call_kind, model


class UsageAccumulator:
    def __init__(self, repo: P.Repository, task_id: str, *, subtask_id: str = "",
                 clock: Callable[[], int], publish: Callable[[dict], None] | None = None):
        self.repo = repo
        self.task_id = task_id
        self.subtask_id = subtask_id or ""
        self.clock = clock
        self.publish = publish
        self._lock = threading.Lock()
        self._pending: dict[_Key, dict[str, int]] = {}
        self._totals = {k: 0 for k in USAGE_COUNTERS}
        for bucket in repo.usage_buckets(task_id, ledger="actual"):
            for k in USAGE_COUNTERS:
                self._totals[k] += int(getattr(bucket, k) or 0)

    def add(self, event: dict) -> None:
        """One C3 ``usage`` line (already normalized by :func:`daemon.exec.c3.parse`)."""
        ledger = event.get("ledger") or "actual"
        key = (ledger, self.subtask_id, str(event.get("module") or "unknown"),
               str(event.get("call_kind") or "unknown"), str(event.get("model") or "unknown"))
        with self._lock:
            bucket = self._pending.setdefault(key, {k: 0 for k in USAGE_COUNTERS})
            for k in USAGE_COUNTERS:
                bucket[k] += int(event.get(k) or 0)
            if ledger == "actual":
                for k in USAGE_COUNTERS:
                    self._totals[k] += int(event.get(k) or 0)
            totals = dict(self._totals)
        if self.publish is not None and ledger == "actual":
            try:
                self.publish(totals)
            except Exception:  # noqa: BLE001 - SSE is best effort, the ledger is the truth
                log.debug("usage publish failed", exc_info=True)

    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._totals)

    def flush(self) -> int:
        """Write what accumulated since the last flush; returns the number of buckets."""
        with self._lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return 0
        deltas = [P.UsageDelta(task_id=self.task_id, ledger=ledger, subtask_id=sub,
                               module_id=module, call_kind=kind, model_name=model, **counts)
                  for (ledger, sub, module, kind, model), counts in pending.items()]
        try:
            self.repo.add_usage(deltas, at=self.clock())
        except Exception:
            # keep them for the next flush rather than lose tokens that were spent
            with self._lock:
                for (key, counts) in pending.items():
                    bucket = self._pending.setdefault(key, {k: 0 for k in USAGE_COUNTERS})
                    for k in USAGE_COUNTERS:
                        bucket[k] += counts[k]
            raise
        return len(deltas)
