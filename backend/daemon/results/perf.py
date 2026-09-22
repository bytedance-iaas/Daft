"""The performance profile of a revision: all calls, the main run only, or one subtask.

v1's latency buckets are kept to the letter (design doc 06 §6.1): per call kind the
attempts (``count``), the logical calls that got no answer even after hedging and
retries (``failed``), the hedged calls, P50 / P90 / P99 over the successful requests,
and the wall clock as the union of the busy intervals - never count x mean.

* ``scope=all`` is the revision's committed ``perf.json`` (``curation report``).
* ``scope=main`` / ``scope=subtask`` split ``details/vlm_latency.csv``. The CSV has no
  subtask column (contract gap, SUMMARY §8 #4), so a request belongs to the subtask
  whose run window (``started_at`` .. ``finished_at``) contains its start - subtasks
  of a task run one at a time - and to the main run otherwise. Requests started after
  the revision was committed are not part of it.

Stage wall clocks come from the stored progress (``elapsed_s`` per stage) of the main
run and of the subtasks; merge figures from the token ledger (merged requests are
booked under ``call_kind = merged`` with the module ids joined by ``+``). Outer retries
are not recorded anywhere yet, so ``retries`` is left out, and so is
``redone_after_interruption`` outside ``scope=all``. ``container`` is this Daemon's
own cgroup quota: the CLI runs in the same container.
"""
from __future__ import annotations

import functools
import logging
from typing import Iterable

from ..errors import ApiError
from ..repo import protocol as P
from .files import json_safe
from .revision import Revision

log = logging.getLogger("daemon.results")

LATENCY_FILE = "details/vlm_latency.csv"
MERGED = "merged"


def _kind_order(kind: str) -> tuple[int, str]:
    from curation.vlm_call_kinds import CALL_KIND_ORDER

    order = list(CALL_KIND_ORDER) + [MERGED]
    return (order.index(kind) if kind in order else len(order), kind)


def latency_items(by_kind: dict) -> list[dict]:
    """``latency_summary`` output (v1's buckets) -> C4 ``Perf.latency``."""
    out = []
    for kind in sorted((k for k in by_kind if isinstance(k, str)), key=_kind_order):
        s = by_kind[kind] if isinstance(by_kind[kind], dict) else {}
        out.append({
            "call_kind": kind,
            "count": int(s.get("attempts", s.get("n", 0)) or 0),
            "failed": int(s.get("unanswered", s.get("errors", 0)) or 0),
            "hedged": int(s.get("hedged", 0) or 0),
            # no successful request -> no percentile; C4 wants numbers, 0 with failed = count
            "p50_s": float(s.get("p50_s") or 0.0), "p90_s": float(s.get("p90_s") or 0.0),
            "p99_s": float(s.get("p99_s") or 0.0), "wall_s": float(s.get("wall_s") or 0.0),
        })
    return out


def _union(intervals: Iterable[tuple[float, float]]) -> float | None:
    ivs = sorted(intervals)
    if not ivs:
        return None
    total, cs, ce = 0.0, ivs[0][0], ivs[0][1]
    for s, e in ivs[1:]:
        if s > ce:
            total += ce - cs
            cs, ce = s, e
        else:
            ce = max(ce, e)
    return round(total + ce - cs, 2)


def _rows(rev: Revision) -> list[tuple]:
    from curation.adapters.vlm_client import read_latency_csv

    path = rev.run_dir / LATENCY_FILE
    if not path.is_file():
        return []
    try:
        return read_latency_csv(str(path))
    except Exception as exc:  # noqa: BLE001 - a damaged CSV leaves the split empty
        log.info("latency CSV of task %s unreadable: %s", rev.task.id, exc)
        return []


def _window(sub: P.Subtask) -> tuple[float, float] | None:
    """The subtask's run window in epoch seconds (the latency CSV's unit); open while running."""
    if sub.started_at is None:
        return None
    end = float("inf") if sub.finished_at is None else sub.finished_at / 1000.0
    return (sub.started_at / 1000.0, end)


def _owner_of(started_at: float | None, windows: list[tuple[str, tuple[float, float]]]) -> str:
    """The subtask id whose window holds ``started_at`` (seconds), '' for the main run."""
    if started_at is not None:
        for sid, (lo, hi) in windows:
            if lo <= started_at <= hi:
                return sid
    return ""


def _scoped(rev: Revision, subtasks: list[P.Subtask], want: str, cutoff_s: float | None):
    from curation.adapters.vlm_client import latency_summary

    windows = [(s.id, _window(s)) for s in subtasks if s.started_at is not None]
    rows = []
    for r in _rows(rev):
        started = r[3]
        if cutoff_s is not None and started is not None and started > cutoff_s:
            continue
        if _owner_of(started, windows) == want:
            rows.append(r)
    wall = _union((r[3], r[3] + r[1]) for r in rows if r[3] is not None)
    busy = sum(float(r[1]) for r in rows)
    eff = round(busy / wall, 2) if wall else None
    return latency_items(latency_summary(rows)), eff


def _stages(progresses: Iterable[dict | None]) -> list[dict]:
    walls: dict[str, float] = {}
    for progress in progresses:
        for st in (progress or {}).get("stages") or []:
            if not isinstance(st, dict) or not isinstance(st.get("id"), str):
                continue
            wall = st.get("elapsed_s")
            if isinstance(wall, (int, float)) and not isinstance(wall, bool) and wall >= 0:
                walls[st["id"]] = walls.get(st["id"], 0.0) + float(wall)
    total = sum(walls.values())
    return [{"id": sid, "wall_s": round(w, 3), "share": round(w / total, 4) if total else 0.0}
            for sid, w in walls.items()]


def _merge(repo: P.Repository, task_id: str, subtask_ids: set[str] | None) -> dict:
    """Merged requests and what they would have been one module at a time (actual ledger)."""
    requests = unmerged = 0
    for b in repo.usage_buckets(task_id, ledger="actual"):
        if b.call_kind != MERGED or (subtask_ids is not None and b.subtask_id not in subtask_ids):
            continue
        n = int(b.requests) + int(b.requests_unknown_usage)
        requests += n
        unmerged += n * max(1, len([m for m in b.module_id.split("+") if m]))
    return {"requests": requests, "estimated_unmerged": unmerged}


def _backend(task: P.Task) -> dict | None:
    snap = task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else None
    if not snap:
        return None
    out = {"name": snap.get("backend"), "kind": snap.get("kind"), "endpoint": snap.get("endpoint"),
           "model": snap.get("model"), "reasoning_effort": snap.get("reasoning_effort"),
           "vlm_parallelism": snap.get("max_concurrency")}
    return {k: v for k, v in out.items() if v is not None or k == "reasoning_effort"}


def _read_first(*paths: str) -> str | None:
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            continue
    return None


@functools.lru_cache(maxsize=1)
def container_quota() -> dict | None:
    """CPU cores and memory GiB of this container's cgroup (v2, then v1); None when unlimited
    or not in a container - an unknown quota is left out, never guessed (v1's rule)."""
    cpu = mem = None
    v2 = _read_first("/sys/fs/cgroup/cpu.max")
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                cpu = round(int(parts[0]) / int(parts[1]), 2)
            except (ValueError, ZeroDivisionError):
                cpu = None
    else:
        q = _read_first("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        p = _read_first("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        try:
            if q and p and int(q) > 0 and int(p) > 0:
                cpu = round(int(q) / int(p), 2)
        except ValueError:
            cpu = None
    raw = _read_first("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if raw and raw != "max":
        try:
            v = int(raw)
            mem = round(v / (1 << 30), 2) if 0 < v < (1 << 50) else None
        except ValueError:
            mem = None
    out = {k: v for k, v in (("cpu_quota", cpu), ("memory_gib", mem)) if v is not None}
    return out or None


def perf(rev: Revision, repo: P.Repository, *, scope: str, subtask_id: str | None) -> dict:
    """C4 ``Perf`` of ``rev``."""
    task = rev.task
    subtasks = repo.list_subtasks(task.id)
    committed = rev.commit().get("created_at")
    cutoff_ms = committed if isinstance(committed, int) else None
    in_rev = [s for s in subtasks if s.started_at is not None
              and (cutoff_ms is None or s.started_at <= cutoff_ms)]
    out: dict = {"revision": rev.number, "scope": scope, "subtask_id": None}
    doc = rev.perf() or {}
    if scope == "all":
        out["latency"] = latency_items(doc.get("latency") or {})
        out["effective_concurrency"] = json_safe(doc.get("effective_concurrency"))
        out["stages"] = _stages([task.progress, *(s.progress for s in in_rev)])
        out["merge"] = _merge(repo, task.id, {"", *(s.id for s in in_rev)})
        redone = doc.get("redone_after_interruption")
        if isinstance(redone, int) and redone >= 0:
            out["redone_after_interruption"] = redone
    else:
        want = ""
        if scope == "subtask":
            sub = next((s for s in subtasks if s.id == subtask_id), None)
            if sub is None:
                raise ApiError("not_found", f"这个任务没有子任务 {subtask_id}",
                               details={"subtask_id": subtask_id})
            want = sub.id
            out["subtask_id"] = sub.id
        cutoff_s = cutoff_ms / 1000.0 if cutoff_ms is not None else None
        out["latency"], out["effective_concurrency"] = _scoped(rev, subtasks, want, cutoff_s)
        if scope == "main":
            out["stages"] = _stages([task.progress])
            out["merge"] = _merge(repo, task.id, {""})
        else:
            counted = any(s.id == want for s in in_rev)
            sub = next(s for s in subtasks if s.id == want)
            out["stages"] = _stages([sub.progress]) if counted else []
            out["merge"] = _merge(repo, task.id, {want}) if counted else {
                "requests": 0, "estimated_unmerged": 0}
    backend = _backend(task)
    if backend:
        out["backend"] = backend
    quota = container_quota()
    if quota:
        out["container"] = quota
    return out
