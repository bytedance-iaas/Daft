"""State changes that keep the database, the audit log and SSE in step.

Every task or subtask state change - W5's worker pool, the API actions and the
startup reconciliation alike - should go through :func:`change_task_state` /
:func:`change_subtask_state`:

1. a compare-and-set on the state (C5), inside one transaction with
2. an audit event (``task.state`` / ``subtask.state``, resource = the task id)
   recording the exact previous state and pause reason - the execution timeline
   (``GET /tasks/{id}/timeline``) is built from these events;
3. after the commit, an SSE ``state`` event, plus ``done`` when the task (or the
   subtask) reached a terminal state.

Audit vocabulary (``event.action``; ``detail`` keys in brackets):

* ``task.create`` / ``task.update`` [fields] / ``task.delete`` / ``task.restore`` /
  ``task.rebind_credentials`` [input, output]
* ``task.state`` [from, to, pause_reason, prev_pause_reason, reason, by]
* ``subtask.state`` [subtask_id, kind, from, to, pause_reason, prev_pause_reason, reason, by]
* ``task.revision`` [revision, subtask_id]
* ``daemon.start`` (resource ``daemon``) / ``daemon.reconcile`` [counts]
"""
from __future__ import annotations

from typing import Iterable

from .events import EventHub
from .repo import protocol as P

_PAUSING = ("pausing", "paused")


def failed_modules(repo: P.Repository, task_id: str) -> list[str]:
    return sorted(m.module_id for m in repo.get_task_modules(task_id) if m.state == "failed")


def change_task_state(repo: P.Repository, hub: EventHub | None, task_id: str, frm: Iterable[str],
                      to: str, *, at: int, actor: str = "system", reason: str | None = None,
                      pause_reason: str | None = None, by: str = "",
                      publish_done: bool = True, owner: str = P.DEFAULT_OWNER) -> bool:
    """CAS ``frm -> to``; False (and nothing written) when the task is not in ``frm``.

    The SSE events carry the audit event's id as their version, so when two threads
    change the same task at once the stream never ends on the older state.
    """
    frm = set(frm)
    with repo.transaction():
        task = repo.get_task(task_id, owner=owner, include_deleted=True)
        if task.state not in frm:
            return False
        if not repo.update_task_state(task_id, {task.state}, to, reason=reason,
                                      pause_reason=pause_reason, at=at):
            return False
        new_pause = (pause_reason or task.pause_reason) if to in _PAUSING else None
        version = repo.append_event(
            actor=actor, action="task.state", resource=task_id, at=at, owner=task.owner_id,
            detail={"from": task.state, "to": to, "pause_reason": new_pause,
                    "prev_pause_reason": task.pause_reason, "reason": reason, "by": by}).id
    if hub is not None:
        hub.publish_state(task_id, to, pause_reason=new_pause, at=at, reason=reason,
                          version=version)
        if publish_done and to in P.TERMINAL_STATES:
            hub.publish_done(task_id, to, failed_modules=failed_modules(repo, task_id),
                             reason=reason, version=version)
    return True


def change_subtask_state(repo: P.Repository, hub: EventHub | None, subtask_id: str,
                         frm: Iterable[str], to: str, *, at: int, actor: str = "system",
                         reason: str | None = None, pause_reason: str | None = None,
                         by: str = "", publish_done: bool = True,
                         owner: str = P.DEFAULT_OWNER) -> bool:
    """CAS on a subtask, like :func:`change_task_state`; ``owner`` is the parent task's.

    A terminal step publishes ``done`` with the subtask's id, after which a stream on
    a finished task ends. So when a subtask ends: recompute the parent's terminal
    state first (D25) with ``change_task_state(..., publish_done=False)`` - its
    ``state`` event goes out, but no ``done`` that clients would close on - and end
    the subtask last. Its ``done`` is then the one end signal, after both changes.
    """
    frm = set(frm)
    with repo.transaction():
        sub = repo.get_subtask(subtask_id)
        if sub.state not in frm:
            return False
        task = repo.get_task(sub.task_id, owner=owner, include_deleted=True)
        if not repo.update_subtask_state(subtask_id, {sub.state}, to, reason=reason,
                                         pause_reason=pause_reason, at=at):
            return False
        prev_pause = sub.pause_reason if sub.state in _PAUSING else None
        new_pause = (pause_reason or prev_pause) if to in _PAUSING else None
        version = repo.append_event(
            actor=actor, action="subtask.state", resource=sub.task_id, at=at, owner=task.owner_id,
            detail={"subtask_id": subtask_id, "kind": sub.kind, "from": sub.state, "to": to,
                    "pause_reason": new_pause, "prev_pause_reason": prev_pause,
                    "reason": reason, "by": by}).id
    if hub is not None:
        hub.publish_state(sub.task_id, to, pause_reason=new_pause, at=at, subtask_id=subtask_id,
                          reason=reason, version=version)
        if publish_done and to in P.TERMINAL_STATES:
            hub.publish_done(sub.task_id, to, failed_modules=failed_modules(repo, sub.task_id),
                             subtask_id=subtask_id, reason=reason, version=version)
    return True


def record(repo: P.Repository, *, action: str, task_id: str, actor: str, at: int,
           detail: dict | None = None, owner: str = P.DEFAULT_OWNER) -> None:
    """Audit one user action on a task (design doc 08, section 7)."""
    repo.append_event(actor=actor, action=action, resource=task_id, at=at, detail=detail,
                      owner=owner)


def record_revision(repo: P.Repository, task_id: str, revision: int, *, at: int,
                    actor: str = "system", subtask_id: str | None = None,
                    owner: str = P.DEFAULT_OWNER) -> None:
    """Call after ``switch_result_rev`` succeeded, so the timeline shows the new revision."""
    repo.append_event(actor=actor, action="task.revision", resource=task_id, at=at, owner=owner,
                      detail={"revision": int(revision), "subtask_id": subtask_id})
