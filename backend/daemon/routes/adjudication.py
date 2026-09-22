"""``{base}/api/v1/tasks/{id}/adjudication`` (W5b): the queue and recording decisions.

* ``GET`` - one card per episode of the current revision (``tab=review``: label
  conflicts and task_success abstentions; ``tab=appeals``: rejects attributed to
  task_success), cursor paging by episode with the revision in the cursor, ``source``
  and ``status`` filters, and the task's counts. It also puts
  ``summary.pending_adjudication`` right when the stored number went stale.
* ``POST`` - append decisions (nothing is executed, D10): validated per line against
  this task's queue, all or nothing; the CSV copy in the run directory and the task
  summary follow. ``Idempotency-Key`` replays the first answer.

Executing the decisions (``POST .../adjudication/apply``) is the orchestration's (W5a).
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from ..results import adjudication as A
from ..results.store import store_of
from .common import idempotency_key, in_thread, principal, read_json_body, runtime, validate

log = logging.getLogger("daemon.results")

router = APIRouter()

_BODY = ("openapi.yaml#/paths/~1tasks~1{id}~1adjudication/post/requestBody/content/"
         "application~1json/schema")


@router.get("/tasks/{task_id}/adjudication")
def list_adjudication(request: Request, task_id: str, source: str | None = None,
                      status: Literal["pending", "decided", "unapplied", "all"] = "pending",
                      tab: Literal["review", "appeals"] = "review", cursor: str | None = None,
                      limit: int = Query(50, ge=1, le=200)):
    A.known_source(source)
    rt, owner = runtime(request), principal(request).owner_id
    task = rt.repo.get_task(task_id, owner=owner)
    store = store_of(rt)
    queue = A.Queue(store, rt.repo, task)
    body = queue.page(tab=tab, status=status, source=source, cursor=cursor, limit=limit)
    stored = (task.summary or {}).get("pending_adjudication") if isinstance(task.summary, dict) \
        else None
    if queue.rev is not None and stored != body["counts"]["pending"]:
        try:
            A.refresh_summary(store, rt.repo, task.id, owner=owner)
        except Exception:  # noqa: BLE001 - a read must not fail on the bookkeeping
            log.warning("summary of task %s not refreshed", task.id, exc_info=True)
    return body


@router.post("/tasks/{task_id}/adjudication")
async def submit_adjudication(request: Request, task_id: str):
    body = await read_json_body(request, required=True)
    validate(_BODY, body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        counts = A.submit(store_of(rt), rt.repo, task_id, body["decisions"], owner=who.owner_id,
                          actor=who.display_name, at=rt.clock())
        return JSONResponse(counts)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request),
                           operation="submitAdjudication", owner=who.owner_id, method="POST",
                           path=request.url.path, body=body, handler=handler)
