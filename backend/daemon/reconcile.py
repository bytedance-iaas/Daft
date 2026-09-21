"""Startup reconciliation (design doc 01, section 3.2; D26).

The Daemon may have been killed (OOM, node loss, upgrade) and left tasks that
look busy with nobody running them. Before requests are accepted:

| found | becomes |
|---|---|
| ``running`` | ``pausing`` -> ``paused`` (system) -> ``queued`` |
| ``pausing`` with pause reason empty or ``system`` | ``paused`` (system) -> ``queued`` |
| ``pausing`` (user) | ``paused`` (user), stays paused |
| ``paused`` (system; graceful shutdown) | ``queued`` |
| ``stopping`` | ``stopped`` |
| ``queued`` | unchanged (W5 re-enqueues it) |

C5 has no ``running -> paused`` edge, so that step is two legal compare-and-sets.
Subtasks follow the same table; their pause reason comes from their audit events
(C5 gap), and a subtask with no recorded reason counts as system paused.
Every step is an audit event (``task.state`` / ``subtask.state`` with
``by: reconcile``) plus one ``daemon.reconcile`` summary, so "why did this task
stop for a while" has an answer. Running the queue is W5's job.
"""
from __future__ import annotations

import collections
import logging
from typing import Callable

from .events import EventHub
from .logs import TaskLogs
from .repo import protocol as P
from .transitions import change_subtask_state, change_task_state, subtask_pause_reason

log = logging.getLogger("daemon.reconcile")

SYSTEM_PAUSE_REASON = "Daemon 重启时任务还在运行"
BY = "reconcile"

#: What the task's own log (stage ``system``) says about each step; English like the CLI's lines.
_LOG_LINES = {
    ("paused", "system"): ("warn", "daemon restarted while this was running: paused by the "
                                   "system, it resumes on its own"),
    ("paused", "user"): ("info", "daemon restarted while pausing: kept paused as requested"),
    ("queued", None): ("info", "auto-resuming after the daemon restart: queued again"),
    ("stopped", None): ("info", "daemon restarted while stopping: stopped"),
}


def reconcile(repo: P.Repository, hub: EventHub | None, clock: Callable[[], int],
              logs: TaskLogs | None = None) -> dict:
    """Bring every task and subtask to a state nobody has to be running for; returns counts.

    With ``logs``, each step also leaves a ``system`` line in the task's (or subtask's) log.
    """
    counts: collections.Counter = collections.Counter()

    def note(task_id: str, to: str, pause_reason: str | None, subtask_id: str | None = None):
        line = _LOG_LINES.get((to, pause_reason if to == "paused" else None))
        if logs is None or line is None:
            return
        try:
            logs.append(task_id, "system", {"ts": clock(), "kind": "log", "level": line[0],
                                            "msg": line[1]}, subtask_id=subtask_id)
        except (OSError, ValueError) as err:
            log.warning("could not write the system log line of %s: %s", task_id, err)

    def task_step(t: P.Task, frm: str, to: str, **kw) -> bool:
        ok = change_task_state(repo, hub, t.id, {frm}, to, at=clock(), by=BY, **kw)
        if ok:
            counts[f"task:{frm}->{to}"] += 1
            note(t.id, to, kw.get("pause_reason"))
        return ok

    for t in repo.tasks_in_states({"running", "pausing", "stopping"}):
        if t.state == "running":
            task_step(t, "running", "pausing", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
            task_step(t, "pausing", "paused", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
        elif t.state == "pausing":
            if t.pause_reason == "user":
                task_step(t, "pausing", "paused", pause_reason="user", reason=t.state_reason)
            else:
                task_step(t, "pausing", "paused", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
        elif t.state == "stopping":
            task_step(t, "stopping", "stopped", reason=t.state_reason)
    for t in repo.tasks_in_states({"paused"}):
        if t.pause_reason != "user":
            task_step(t, "paused", "queued")

    def sub_step(s: P.Subtask, frm: str, to: str, **kw) -> bool:
        ok = change_subtask_state(repo, hub, s.id, {frm}, to, at=clock(), by=BY, **kw)
        if ok:
            counts[f"subtask:{frm}->{to}"] += 1
            note(s.task_id, to, kw.get("pause_reason"), subtask_id=s.id)
        return ok

    for parent in repo.tasks_in_states(P.TERMINAL_STATES):
        sub = repo.active_subtask(parent.id)
        if sub is None:
            continue
        reason = subtask_pause_reason(repo, sub) if sub.state in ("pausing", "paused") else None
        if sub.state == "running":
            sub_step(sub, "running", "pausing", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
            sub_step(sub, "pausing", "paused", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
            sub_step(sub, "paused", "queued")
        elif sub.state == "pausing":
            if reason == "user":
                sub_step(sub, "pausing", "paused", pause_reason="user", reason=sub.state_reason)
            else:
                sub_step(sub, "pausing", "paused", pause_reason="system",
                         reason=SYSTEM_PAUSE_REASON)
                sub_step(sub, "paused", "queued")
        elif sub.state == "paused" and reason != "user":
            sub_step(sub, "paused", "queued")
        elif sub.state == "stopping":
            sub_step(sub, "stopping", "stopped", reason=sub.state_reason)

    summary = dict(sorted(counts.items()))
    repo.append_event(actor="system", action="daemon.reconcile", resource="daemon", at=clock(),
                      detail={"counts": summary})
    if summary:
        log.info("startup reconciliation: %s", summary)
    return summary
