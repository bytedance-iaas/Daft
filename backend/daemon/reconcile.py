"""Startup reconciliation (design doc 01, section 3.2; D26).

The Daemon may have been killed (OOM, node loss, upgrade) and left tasks that
look busy with nobody running them. Before requests are accepted:

| found | becomes |
|---|---|
| ``running`` | ``pausing`` -> ``paused`` (system) -> ``queued`` |
| ``pausing`` with pause reason empty or ``system`` | ``paused`` (system) -> ``queued`` |
| ``pausing`` (user) | ``paused`` (user), stays paused |
| ``paused`` (system or empty; graceful shutdown) | ``queued`` |
| ``stopping`` | ``stopped`` |
| ``queued`` | unchanged (W5 re-enqueues it) |

C5 has no ``running -> paused`` edge, so that step is two legal compare-and-sets.
Subtasks follow the same table, with their own ``pause_reason`` (C5 1.2); they
are found with ``subtasks_in_states``, their owner is their parent task's.
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
from .transitions import change_subtask_state, change_task_state

log = logging.getLogger("daemon.reconcile")

SYSTEM_PAUSE_REASON = "Daemon 重启时任务还在运行"
BY = "reconcile"

#: The states nobody may be left in after a restart.
_BUSY = frozenset({"running", "pausing", "stopping"})

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

    def walk(state: str, pause_reason: str | None, state_reason: str | None, step) -> None:
        """The table above, for a task or a subtask; ``step(frm, to, **kw)`` does one CAS."""
        if state == "running":
            step("running", "pausing", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
            step("pausing", "paused", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
            step("paused", "queued")
        elif state == "pausing" and pause_reason == "user":
            step("pausing", "paused", pause_reason="user", reason=state_reason)
        elif state == "pausing":
            step("pausing", "paused", pause_reason="system", reason=SYSTEM_PAUSE_REASON)
            step("paused", "queued")
        elif state == "paused" and pause_reason != "user":
            step("paused", "queued")
        elif state == "stopping":
            step("stopping", "stopped", reason=state_reason)

    for t in repo.tasks_in_states(_BUSY | {"paused"}):
        def task_step(frm: str, to: str, _t=t, **kw) -> bool:
            ok = change_task_state(repo, hub, _t.id, {frm}, to, at=clock(), by=BY,
                                   owner=_t.owner_id, **kw)
            if ok:
                counts[f"task:{frm}->{to}"] += 1
                note(_t.id, to, kw.get("pause_reason"))
            return ok

        walk(t.state, t.pause_reason, t.state_reason, task_step)

    subtasks = repo.subtasks_in_states(_BUSY | {"paused"})
    # every subtask hangs off a finished task (SUBTASK_PARENT_STATES); one scan finds the owners
    owners = {t.id: t.owner_id for t in repo.tasks_in_states(P.TERMINAL_STATES)} if subtasks else {}
    for sub in subtasks:
        owner = owners.get(sub.task_id)
        if owner is None:
            log.warning("subtask %s is %s but its task %s is not finished; left alone",
                        sub.id, sub.state, sub.task_id)
            continue

        def sub_step(frm: str, to: str, _s=sub, _owner=owner, **kw) -> bool:
            ok = change_subtask_state(repo, hub, _s.id, {frm}, to, at=clock(), by=BY,
                                      owner=_owner, **kw)
            if ok:
                counts[f"subtask:{frm}->{to}"] += 1
                note(_s.task_id, to, kw.get("pause_reason"), subtask_id=_s.id)
            return ok

        walk(sub.state, sub.pause_reason, sub.state_reason, sub_step)

    summary = dict(sorted(counts.items()))
    repo.append_event(actor="system", action="daemon.reconcile", resource="daemon", at=clock(),
                      detail={"counts": summary})
    if summary:
        log.info("startup reconciliation: %s", summary)
    return summary
