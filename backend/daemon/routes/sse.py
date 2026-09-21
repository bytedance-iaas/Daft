"""``GET {base}/events/tasks/{id}`` - server-sent events for one task (design doc 03, section 5).

Stream shape:

1. ``retry: 3000``;
2. a reconnect whose ``Last-Event-ID`` can be resumed gets the missed events;
   one that cannot (other epoch, evicted, malformed) gets ``event: reset`` first;
3. a fresh or reset connection gets a snapshot: ``state`` of the task (and of
   its active subtask), read after subscribing so nothing falls in between -
   the snapshot carries the id of the newest event it already covers;
4. then live events, with a ``: ping`` comment line after every
   ``sse_heartbeat_s`` of silence.

After a ``done`` event, the stream ends when the task is terminal and no
subtask is running (clients close their ``EventSource`` on ``done``; if they
reconnect they get the snapshot and ``done`` again). A client that falls too
far behind is sent ``reset`` and disconnected.
"""
from __future__ import annotations

import collections
import logging

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..events import format_event
from ..repo import protocol as P
from ..transitions import failed_modules
from .common import in_thread, principal, runtime

log = logging.getLogger("daemon.sse")

router = APIRouter()

RETRY_MS = 3000
PING = b": ping\n\n"


def _snapshot(repo: P.Repository, task_id: str, owner: str):
    task = repo.get_task(task_id, owner=owner)
    active = repo.active_subtask(task_id)
    failed = failed_modules(repo, task_id) if task.state in P.TERMINAL_STATES else []
    return task, active, failed


def _finished(repo: P.Repository, task_id: str, owner: str) -> bool:
    try:
        task = repo.get_task(task_id, owner=owner)
    except P.NotFound:
        return True
    return task.state in P.TERMINAL_STATES and repo.active_subtask(task_id) is None


@router.get("/tasks/{task_id}")
async def task_events(request: Request, task_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    await in_thread(rt.repo.get_task, task_id, owner=owner)            # 404 before streaming
    last_event_id = request.headers.get("last-event-id")
    hub, heartbeat = rt.hub, rt.settings.sse_heartbeat_s

    async def stream():
        sub = hub.subscribe(task_id, last_event_id)
        try:
            yield f"retry: {RETRY_MS}\n\n".encode()
            head_id = hub.event_id(sub.head)
            if sub.reset:
                yield format_event("reset", {"reason": "replay_unavailable"}, head_id)
            if sub.fresh or sub.reset:
                task, active, failed = await in_thread(_snapshot, rt.repo, task_id, owner)
                yield format_event("state", {"state": task.state, "pause_reason": task.pause_reason,
                                             "at": task.updated_at}, head_id)
                if active is not None:
                    yield format_event("state", {"state": active.state, "pause_reason": None,
                                                 "at": active.started_at or active.created_at,
                                                 "subtask_id": active.id}, head_id)
                elif task.state in P.TERMINAL_STATES:
                    yield format_event("done", {"state": task.state, "failed_modules": failed},
                                       head_id)
                    return
            pending = collections.deque(sub.replay)
            while True:
                if not pending:
                    pending.extend(sub.drain())
                if not pending:
                    if sub.ended:
                        return                   # Daemon shutting down; the client reconnects
                    if sub.overflowed:
                        yield format_event("reset", {"reason": "client_too_slow"},
                                           hub.event_id(hub.head))
                        return
                    if not await sub.wait(heartbeat):
                        yield PING
                    continue
                ev = pending.popleft()
                yield format_event(ev.event, ev.data, hub.event_id(ev.seq))
                if ev.event == "done" and not pending and not sub.has_items() \
                        and await in_thread(_finished, rt.repo, task_id, owner):
                    return
        finally:
            hub.unsubscribe(sub)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
