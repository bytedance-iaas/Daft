"""``{base}/api/v1`` routes that start, steer and extend tasks (W5a; C4 1.4).

createTask, createTasksBatch, taskAction (start / pause / resume / stop), repreflightTask,
retryTask, continueTask, reexportTask, applyAdjudication, purgeTaskArtifacts and
getTaskPlan. The work itself is :class:`daemon.orchestr.service.Orchestrator`'s; every
write accepts ``Idempotency-Key`` and only JSON (``read_json_body``).
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from .. import views
from ..errors import ApiError
from ..orchestr.service import orchestrator_of
from ..repo import protocol as P
from .common import idempotency_key, in_thread, principal, read_json_body, runtime, validate

router = APIRouter()

ACTIONS = ("start", "pause", "resume", "stop")
_RETRY_BODY = ("openapi.yaml#/paths/~1tasks~1{id}~1retry/post/requestBody/content/"
               "application~1json/schema")
_PURGE_BODY = ("openapi.yaml#/paths/~1tasks~1{id}~1purge-artifacts/post/requestBody/content/"
               "application~1json/schema")


def _task_json(rt, task: P.Task, owner: str) -> dict:
    return views.task_detail(task, repo=rt.repo, names=views.Names(rt.repo, owner),
                             links=rt.links, now=rt.clock())


def _task_response(rt, task: P.Task, owner: str, status: int = 200) -> JSONResponse:
    return JSONResponse(_task_json(rt, task, owner), status_code=status,
                        headers={"ETag": views.etag(task)})


def _created(rt, task: P.Task, warnings: list[str]) -> dict:
    return {"id": task.id, "state": task.state, "created_at": task.created_at,
            "warnings": list(warnings),
            "links": rt.links.for_task(task.id, result_rev=int(task.result_rev or 0),
                                       pending_adjudication=views.pending_adjudication(task))}


def _subtask_created(rt, sub: P.Subtask, owner: str) -> JSONResponse:
    task = rt.repo.get_task(sub.task_id, owner=owner)
    body = {"subtask": views.subtask(sub),
            "links": rt.links.for_task(task.id, result_rev=int(task.result_rev or 0),
                                       pending_adjudication=views.pending_adjudication(task))}
    return JSONResponse(body, status_code=202)


def _idem(request: Request, rt, who, operation: str, body, handler):
    return in_thread(rt.idempotency.run, key=idempotency_key(request), operation=operation,
                     owner=who.owner_id, method=request.method, path=request.url.path, body=body,
                     handler=handler)


# ---------------------------------------------------------------------------
# creating tasks
# ---------------------------------------------------------------------------

@router.post("/tasks")
async def create_task(request: Request):
    body = await read_json_body(request, required=True)
    validate("TaskCreate", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        task, warnings = orchestrator_of(rt).create_task(body, who)
        return JSONResponse(_created(rt, task, warnings), status_code=201)

    return await _idem(request, rt, who, "createTask", body, handler)


@router.post("/tasks/batch")
async def create_tasks_batch(request: Request):
    body = await read_json_body(request, required=True)
    validate("TaskBatchCreate", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        made = orchestrator_of(rt).create_batch(body, who)
        return JSONResponse({"tasks": [_created(rt, t, w) for t, w in made]}, status_code=201)

    return await _idem(request, rt, who, "createTasksBatch", body, handler)


# ---------------------------------------------------------------------------
# steering
# ---------------------------------------------------------------------------

@router.post("/tasks/{task_id}/actions/{action}")
async def task_action(request: Request, task_id: str, action: str):
    await read_json_body(request, required=False)
    if action not in ACTIONS:
        raise ApiError("validation_failed", f"没有 {action} 这个操作（可选：start、pause、resume、stop）",
                       details={"errors": [{"field": "action", "problem": "unknown action"}]})
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        orch = orchestrator_of(rt)
        task = getattr(orch, action)(task_id, who)
        return _task_response(rt, task, who.owner_id)

    return await _idem(request, rt, who, "taskAction", None, handler)


@router.post("/tasks/{task_id}/repreflight")
async def repreflight_task(request: Request, task_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        out = orchestrator_of(rt).repreflight_task(task_id, who)
        return JSONResponse({"compatible": out["compatible"],
                             "incompatibilities": out["incompatibilities"],
                             "task": _task_json(rt, out["task"], who.owner_id)})

    return await _idem(request, rt, who, "repreflightTask", None, handler)


# ---------------------------------------------------------------------------
# subtasks
# ---------------------------------------------------------------------------

@router.post("/tasks/{task_id}/retry")
async def retry_task(request: Request, task_id: str):
    body = await read_json_body(request, required=False)
    body = {} if body is None else body
    validate(_RETRY_BODY, body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        sub = orchestrator_of(rt).create_subtask(task_id, "retry", who,
                                                 scope={"modules": body.get("modules") or []})
        return _subtask_created(rt, sub, who.owner_id)

    return await _idem(request, rt, who, "retryTask", body, handler)


@router.post("/tasks/{task_id}/continue")
async def continue_task(request: Request, task_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        return _subtask_created(rt, orchestrator_of(rt).create_subtask(task_id, "resume", who),
                                who.owner_id)

    return await _idem(request, rt, who, "continueTask", None, handler)


@router.post("/tasks/{task_id}/reexport")
async def reexport_task(request: Request, task_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        return _subtask_created(rt, orchestrator_of(rt).create_subtask(task_id, "reexport", who),
                                who.owner_id)

    return await _idem(request, rt, who, "reexportTask", None, handler)


@router.post("/tasks/{task_id}/adjudication/apply")
async def apply_adjudication(request: Request, task_id: str):
    body = await read_json_body(request, required=False)
    body = {} if body is None else body
    validate("AdjudicationApply", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        sub = orchestrator_of(rt).create_subtask(
            task_id, "apply_adjudication", who,
            scope={"relabel_rerun": body.get("relabel_rerun") or "v1"})
        return _subtask_created(rt, sub, who.owner_id)

    return await _idem(request, rt, who, "applyAdjudication", body, handler)


# ---------------------------------------------------------------------------
# artifacts and the plan
# ---------------------------------------------------------------------------

@router.post("/tasks/{task_id}/purge-artifacts")
async def purge_artifacts(request: Request, task_id: str):
    body = await read_json_body(request, required=True)
    validate(_PURGE_BODY, body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        out = orchestrator_of(rt).purge(task_id, body["confirm_path"], who)
        return JSONResponse(out, status_code=202)

    return await _idem(request, rt, who, "purgeTaskArtifacts", body, handler)


@router.get("/tasks/{task_id}/plan")
def get_plan(request: Request, task_id: str):
    rt, who = runtime(request), principal(request)
    return orchestrator_of(rt).plan(task_id, who)
