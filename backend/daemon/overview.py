"""``GET /overview`` in one call (C4 ``Overview``; design doc 07, section 4.3; D36).

What each figure counts (soft-deleted tasks never count):

* ``todo.error_tasks`` - tasks in ``completed_with_errors`` (retryable), the same
  set ``GET /tasks?state=completed_with_errors`` lists;
* ``todo.adjudication`` / ``todo.delivery_pending`` - see :mod:`daemon.repo.extras`;
* ``todo.datasets_changed`` - registrations whose fingerprints differ
  (``check_state=changed``), the ``datasets.changed`` figure too;
* ``todo.credentials_failed`` / ``backends_failed`` - TOS access keys and VLM
  backends whose last verification failed (VLM keys belong to their backend);
* ``running.running`` - tasks a worker is busy with (``running``, ``pausing``,
  ``stopping``); ``queued`` and ``paused`` are those states; ``active`` lists the
  busy ones, newest first, with the stage that is running now (or the last one
  that started) and its counts;
* ``recent`` - the last 7 days, today included, in the site's time zone
  (``CURATOR_TZ_OFFSET``): tasks that finished ``succeeded`` or
  ``completed_with_errors``, the episodes they checked (their summary's
  ``total``), the pass rate over those episodes (``passed / total``, null without
  any), and actual-ledger tokens per day (``prompt + completion``), oldest first,
  every day listed.
"""
from __future__ import annotations

import datetime as dt

from .repo import protocol as P
from .views import stage_progress, task_ref

DAY_MS = 24 * 60 * 60 * 1000
RECENT_DAYS = 7
#: The overview shows at most this many busy tasks; the task list has the rest.
ACTIVE_LIMIT = 20
_BUSY = ("running", "pausing", "stopping")


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _current_stage(progress: dict | None) -> dict | None:
    stages = stage_progress(progress)["stages"]
    running = [s for s in stages if s["state"] == "running"]
    if running:
        return running[0]
    started = [s for s in stages if s["state"] != "pending"]
    return started[-1] if started else None


def _active(task: P.Task) -> dict:
    stage = _current_stage(task.progress)
    return {"task": task_ref(task), "stage": stage["id"] if stage else None,
            "done": _count(stage["done"]) if stage else 0,
            "total": _count(stage["total"]) if stage else 0}


def day_window(now: int, tz_offset_minutes: int, days: int = RECENT_DAYS) -> list[tuple[int, str]]:
    """``(start in epoch ms, local date)`` of the last ``days`` days, today last."""
    offset = tz_offset_minutes * 60_000
    today = (now + offset) - (now + offset) % DAY_MS - offset
    out = []
    for i in range(days - 1, -1, -1):
        start = today - i * DAY_MS
        date = dt.datetime.fromtimestamp((start + offset) / 1000, tz=dt.timezone.utc)
        out.append((start, date.strftime("%Y-%m-%d")))
    return out


def build(repo, *, owner: str, now: int, tz_offset_minutes: int) -> dict:
    """``repo`` is a C5 repository with :class:`daemon.repo.extras.RepositoryExtras`."""
    def total(state: str) -> int:
        return repo.list_tasks(owner=owner, page=1, page_size=1, state=state).total

    busy_total, busy = 0, []
    for state in _BUSY:
        page = repo.list_tasks(owner=owner, page=1, page_size=ACTIVE_LIMIT, state=state)
        busy_total += page.total
        busy += page.items
    busy.sort(key=lambda t: (t.created_at, t.id), reverse=True)

    changed = repo.list_datasets(owner=owner, page=1, page_size=1, check_state="changed").total
    tasks, episodes = repo.adjudication_backlog(owner=owner)

    days = day_window(now, tz_offset_minutes)
    since, until = days[0][0], days[-1][0] + DAY_MS
    per_day = dict.fromkeys((start for start, _ in days), 0)
    for slot, tokens in repo.token_timeline(since=since, until=until, owner=owner):
        start = since + (slot - since) // DAY_MS * DAY_MS
        if start in per_day:
            per_day[start] += int(tokens)
    finished = repo.finished_results(since=since, owner=owner)

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
        "running": {"running": busy_total, "queued": total("queued"), "paused": total("paused"),
                    "active": [_active(t) for t in busy[:ACTIVE_LIMIT]]},
        "recent": {
            "days": RECENT_DAYS,
            "tasks_finished": finished.tasks,
            "episodes_checked": finished.episodes,
            "pass_rate": (round(min(1.0, finished.passed / finished.episodes), 4)
                          if finished.episodes else None),
            "tokens_per_day": [{"date": date, "tokens": per_day[start]} for start, date in days],
        },
        "datasets": {"total": repo.list_datasets(owner=owner, page=1, page_size=1).total,
                     "changed": changed},
        "generated_at": now,
    }
