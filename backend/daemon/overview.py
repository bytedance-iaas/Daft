"""``GET /overview`` in one call (C4 ``Overview``; design doc 07, section 4.3; D36).

What each figure counts (soft-deleted tasks never count, except in tokens):

* ``todo.error_tasks`` - tasks in ``completed_with_errors`` (retryable), the same
  set ``GET /tasks?state=completed_with_errors`` lists;
* ``todo.adjudication`` / ``todo.delivery_pending`` - see :mod:`daemon.repo.extras`;
* ``todo.datasets_changed`` - registrations whose fingerprints differ
  (``check_state=changed``), the ``datasets.changed`` figure too;
* ``todo.credentials_failed`` - access keys (``kind='tos'``) whose last
  verification failed; a VLM backend's API key is a credential row too
  (``ark`` / ``custom_vlm``) but belongs to its backend, so it counts in
  ``backends_failed`` - backends whose last verification failed;
* ``running`` - the work the workers have: main runs and subtasks (a retry,
  resume, adjudication run or re-export hangs off a finished task, so its task's
  state alone would hide it). ``running`` is what a worker is busy with
  (``running``, ``pausing``, ``stopping``), ``queued`` and ``paused`` are those
  states; ``active`` lists the busy ones, newest first, each with its task and
  the stage that is running now (or the last one that started) and its counts;
* ``recent`` - the period the page picked (``days``: 7, 30, 90 or 365; the console
  offers 近 7 天 / 近 1 月 / 近 3 月 / 近 1 年) in the site's time zone
  (``CURATOR_TZ_OFFSET``), cut into buckets that end with the current one: 7 or 30
  days; for 90 the 13 calendar weeks (Monday to Sunday) up to this one; for 365 the
  12 calendar months up to this one. ``since`` is where the first bucket starts, and
  the figures cover ``since`` up to now: tasks that finished ``succeeded`` or
  ``completed_with_errors``, the episodes they checked (their summary's ``total``),
  the pass rate over those episodes (``passed / total``, null without any), and
  actual-ledger tokens (``prompt + completion``) per bucket, oldest first, every
  bucket listed - tokens stay counted when their task is deleted later.
"""
from __future__ import annotations

import bisect
import datetime as dt

from .repo import protocol as P
from .views import stage_progress, task_ref

DAY_MS = 24 * 60 * 60 * 1000
#: ``days`` a request may ask for -> (bucket, how many buckets, the current one included).
RANGES: dict[int, tuple[str, int]] = {7: ("day", 7), 30: ("day", 30), 90: ("week", 13),
                                      365: ("month", 12)}
DEFAULT_DAYS = 7
#: The overview shows at most this many busy tasks; the task list has the rest.
ACTIVE_LIMIT = 20
_BUSY = ("running", "pausing", "stopping")
_EPOCH = dt.date(1970, 1, 1)


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _current_stage(progress: dict | None) -> dict | None:
    stages = stage_progress(progress)["stages"]
    running = [s for s in stages if s["state"] == "running"]
    if running:
        return running[0]
    started = [s for s in stages if s["state"] != "pending"]
    return started[-1] if started else None


def _active(task: P.Task, progress: dict | None) -> dict:
    """One busy main run (``progress`` = the task's) or subtask (``progress`` = its own)."""
    stage = _current_stage(progress)
    return {"task": task_ref(task), "stage": stage["id"] if stage else None,
            "done": _count(stage["done"]) if stage else 0,
            "total": _count(stage["total"]) if stage else 0}


def _midnight(day: dt.date, offset_ms: int) -> int:
    """Epoch ms of 00:00 on ``day`` in the site's time zone (a fixed UTC offset)."""
    return (day - _EPOCH).days * DAY_MS - offset_ms


def _month_start(day: dt.date, months_back: int) -> dt.date:
    index = day.year * 12 + day.month - 1 - months_back
    return dt.date(index // 12, index % 12 + 1, 1)


def buckets(now: int, tz_offset_minutes: int,
            days: int = DEFAULT_DAYS) -> tuple[str, list[tuple[int, str]]]:
    """``(bucket, [(start in epoch ms, label), ...])`` of a ``days`` range: the bucket that
    holds ``now`` and the ones before it, oldest first. Labels are ``09-23`` for a day,
    ``09-21 周`` for the week that starts on Monday 09-21 and ``2026-09`` for a month."""
    kind, count = RANGES[days]
    offset = tz_offset_minutes * 60_000
    today = _EPOCH + dt.timedelta(days=(now + offset) // DAY_MS)
    back = range(count - 1, -1, -1)
    if kind == "day":
        firsts = [(today - dt.timedelta(days=i), "{:%m-%d}") for i in back]
    elif kind == "week":
        monday = today - dt.timedelta(days=today.weekday())
        firsts = [(monday - dt.timedelta(weeks=i), "{:%m-%d} 周") for i in back]
    else:
        firsts = [(_month_start(today, i), "{:%Y-%m}") for i in back]
    return kind, [(_midnight(day, offset), label.format(day)) for day, label in firsts]


def build(repo, *, owner: str, now: int, tz_offset_minutes: int,
          days: int = DEFAULT_DAYS) -> dict:
    """``repo`` is a C5 repository (1.3 took the overview's queries in)."""
    if days not in RANGES:
        raise ValueError(f"the overview covers {sorted(RANGES)} days, not {days}")

    def total(state: str) -> int:
        return repo.list_tasks(owner=owner, page=1, page_size=1, state=state).total

    busy_total, busy = 0, []                     # busy: (created_at, id, active item)
    for state in _BUSY:
        page = repo.list_tasks(owner=owner, page=1, page_size=ACTIVE_LIMIT, state=state)
        busy_total += page.total
        busy += [(t.created_at, t.id, _active(t, t.progress)) for t in page.items]
    queued, paused = total("queued"), total("paused")
    for sub, parent in repo.unfinished_subtasks(owner=owner):
        if sub.state in _BUSY:
            busy_total += 1
            busy.append((sub.created_at, sub.id, _active(parent, sub.progress)))
        elif sub.state == "queued":
            queued += 1
        elif sub.state == "paused":
            paused += 1
    busy.sort(key=lambda x: (x[0], x[1]), reverse=True)

    changed = repo.list_datasets(owner=owner, page=1, page_size=1, check_state="changed").total
    tasks, episodes = repo.adjudication_backlog(owner=owner)

    kind, spans = buckets(now, tz_offset_minutes, days)
    starts = [start for start, _ in spans]
    offset = tz_offset_minutes * 60_000
    until = (now + offset) // DAY_MS * DAY_MS - offset + DAY_MS       # the end of today
    tokens = [0] * len(spans)
    for slot, spent in repo.token_timeline(since=starts[0], until=until, owner=owner):
        tokens[bisect.bisect_right(starts, slot) - 1] += int(spent)
    finished = repo.finished_results(since=starts[0], owner=owner)

    return {
        "todo": {
            "error_tasks": total("completed_with_errors"),
            "adjudication": {"tasks": tasks, "episodes": episodes},
            "delivery_pending": repo.delivery_pending_count(owner=owner),
            "datasets_changed": changed,
            "credentials_failed": sum(1 for c in repo.list_credentials(owner=owner, kind="tos")
                                      if c.verify_state == "failed"),
            "backends_failed": sum(1 for b in repo.list_vlm_backends(owner=owner)
                                   if b.verify_state == "failed"),
        },
        "running": {"running": busy_total, "queued": queued, "paused": paused,
                    "active": [item for _, _, item in busy[:ACTIVE_LIMIT]]},
        "recent": {
            "days": days,
            "bucket": kind,
            "since": starts[0],
            "tasks_finished": finished.tasks,
            "episodes_checked": finished.episodes,
            "pass_rate": (round(min(1.0, finished.passed / finished.episodes), 4)
                          if finished.episodes else None),
            "tokens_per_bucket": [{"start": start, "label": label, "tokens": spent}
                                  for (start, label), spent in zip(spans, tokens)],
        },
        "datasets": {"total": repo.list_datasets(owner=owner, page=1, page_size=1).total,
                     "changed": changed},
        "generated_at": now,
    }
