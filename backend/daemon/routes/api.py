"""``{base}/api/v1`` routes that need only the repository and the work directory (W4).

Everything else in C4 is owned by W5 / W8 / W3 and not registered yet; see
:mod:`daemon.operations`.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from curation.contracts import modules as registry

from .. import taskspec, timeline, views
from ..errors import ApiError
from ..logs import ALL_RUNS, LEVEL_RANK
from ..repo import protocol as P
from ..transitions import record
from .common import idempotency_key, in_thread, principal, read_json_body, runtime, validate

router = APIRouter()

#: Soft-deleted tasks can be restored for 30 days (P12).
RESTORE_WINDOW_MS = 30 * 24 * 60 * 60 * 1000
PAGE_SIZES = (10, 20, 50, 100)
_TASK_STATES = tuple(P.TASK_TRANSITIONS)
_REBIND_BODY = ("openapi.yaml#/paths/~1tasks~1{id}~1rebind-credentials/post/requestBody/content/"
                "application~1json/schema")
_STAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TIMELINE_EVENT_CAP = 5000


def _task_json(rt, task: P.Task, owner: str) -> dict:
    return views.task_detail(task, repo=rt.repo, names=views.Names(rt.repo, owner),
                             links=rt.links, now=rt.clock())


def _task_response(rt, task: P.Task, owner: str, status: int = 200) -> JSONResponse:
    return JSONResponse(_task_json(rt, task, owner), status_code=status,
                        headers={"ETag": views.etag(task)})


def _owned_task(rt, task_id: str, owner: str) -> P.Task:
    return rt.repo.get_task(task_id, owner=owner)


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------

@router.get("/modules")
def get_modules():
    return registry.export()


# ---------------------------------------------------------------------------
# task list and detail
# ---------------------------------------------------------------------------

@router.get("/tasks")
def list_tasks(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20),
               state: str | None = None, q: str | None = None, delivery: str | None = None):
    if page_size not in PAGE_SIZES:
        raise ApiError("validation_failed", "每页条数只能是 10、20、50 或 100",
                       details={"errors": [{"field": "page_size", "problem": "not in 10/20/50/100"}]})
    if state is not None and state not in _TASK_STATES and state != "deleted":
        raise ApiError("validation_failed", f"没有 {state} 这个任务状态",
                       details={"errors": [{"field": "state", "problem": "unknown state"}]})
    rt, owner = runtime(request), principal(request).owner_id
    key = taskspec.normalize_tos_uri(delivery, field_name="delivery") if delivery else None
    result = rt.repo.list_tasks(owner=owner, page=page, page_size=page_size, state=state,
                                q=(q or "").strip() or None, delivery_key=key)
    return {"items": [views.task_list_item(t, repo=rt.repo) for t in result.items],
            "page": result.page, "page_size": result.page_size, "total": result.total}


@router.get("/tasks/{task_id}")
def get_task(request: Request, task_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    return _task_response(rt, _owned_task(rt, task_id, owner), owner)


def _if_match(request: Request) -> int:
    raw = request.headers.get("if-match")
    if raw is None:
        raise ApiError("validation_failed", "修改任务要带 If-Match 请求头（取任务详情里的 updated_at）",
                       details={"errors": [{"field": "If-Match", "problem": "missing"}]})
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"')
    if not value.isdigit():
        raise ApiError("validation_failed", "If-Match 应该是任务的 updated_at（整数）",
                       details={"errors": [{"field": "If-Match", "problem": "not an integer"}]})
    return int(value)


@router.patch("/tasks/{task_id}")
async def update_task(request: Request, task_id: str):
    body = await read_json_body(request, required=True)
    validate("TaskPatch", body)
    expected = _if_match(request)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        with rt.repo.transaction():
            task = _owned_task(rt, task_id, who.owner_id)
            if task.updated_at != expected:
                raise ApiError("precondition_failed", details={"updated_at": task.updated_at})
            resolved = taskspec.resolve_config(rt.repo, rt.settings, task, body, now=rt.clock(),
                                               owner=who.owner_id)
            fields = resolved.fields or {"name": task.name}      # still bump updated_at
            updated = rt.repo.update_task_fields(task_id, if_updated_at=expected,
                                                 owner=who.owner_id, **fields)
            if resolved.modules is not None:
                rt.repo.upsert_task_modules(task_id, resolved.modules)
            record(rt.repo, action="task.update", task_id=task_id, actor=who.display_name,
                   at=rt.clock(), detail={"fields": sorted(body)}, owner=who.owner_id)
        return _task_response(rt, updated, who.owner_id)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="updateTask",
                           owner=who.owner_id, method="PATCH", path=request.url.path, body=body,
                           handler=handler)


@router.delete("/tasks/{task_id}")
async def delete_task(request: Request, task_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        with rt.repo.transaction():
            task = _owned_task(rt, task_id, who.owner_id)
            try:
                rt.repo.soft_delete_task(task_id, at=rt.clock())
            except P.StateConflict:
                raise ApiError("task_state_conflict",
                               "只有待启动或已结束的任务可以删除；运行中的任务请先停止",
                               details={"state": task.state}) from None
            except P.Conflict as err:
                if err.code != "subtask_active":
                    raise
                active = rt.repo.active_subtask(task_id)
                raise ApiError("subtask_active", "任务还有子任务没结束，等它结束后再删除",
                               details={"state": task.state,
                                        "active_subtask": active.id if active else None}) from None
            record(rt.repo, action="task.delete", task_id=task_id, actor=who.display_name,
                   at=rt.clock(), owner=who.owner_id)
        return Response(status_code=204)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="deleteTask",
                           owner=who.owner_id, method="DELETE", path=request.url.path, body=None,
                           handler=handler)


@router.post("/tasks/{task_id}/restore")
async def restore_task(request: Request, task_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        now = rt.clock()
        with rt.repo.transaction():
            task = rt.repo.get_task(task_id, owner=who.owner_id, include_deleted=True)
            if task.deleted_at is not None and now - task.deleted_at > RESTORE_WINDOW_MS:
                raise ApiError("not_found", "这个任务删除已超过 30 天，无法恢复")
            restored = rt.repo.restore_task(task_id)
            if task.deleted_at is not None:
                record(rt.repo, action="task.restore", task_id=task_id, actor=who.display_name,
                       at=now, owner=who.owner_id)
        return _task_response(rt, restored, who.owner_id)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="restoreTask",
                           owner=who.owner_id, method="POST", path=request.url.path, body=None,
                           handler=handler)


@router.post("/tasks/{task_id}/rebind-credentials")
async def rebind_credentials(request: Request, task_id: str):
    body = await read_json_body(request, required=True)
    validate(_REBIND_BODY, body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        with rt.repo.transaction():
            task = _owned_task(rt, task_id, who.owner_id)
            if task.state not in P.TERMINAL_STATES:
                raise ApiError("task_state_conflict",
                               "只有已结束的任务才能重新绑定访问密钥；待启动的任务请直接编辑",
                               details={"state": task.state})
            ids = {}
            if "input_credential" in body:
                if task.input_source != "tos":
                    raise ApiError("validation_failed", "这个任务的数据来源不需要访问密钥",
                                   details={"errors": [{"field": "input_credential",
                                                        "problem": "input is not tos"}]})
                ids["input_cred_id"] = taskspec.credential_id(rt.repo, body["input_credential"],
                                                               who.owner_id, "input_credential")
            if "output_credential" in body:
                ids["output_cred_id"] = taskspec.credential_id(rt.repo, body["output_credential"],
                                                                who.owner_id, "output_credential")
            updated = rt.repo.rebind_task_credentials(
                task_id, input_cred_id=ids.get("input_cred_id"),
                output_cred_id=ids.get("output_cred_id"))
            record(rt.repo, action="task.rebind_credentials", task_id=task_id,
                   actor=who.display_name, at=rt.clock(), owner=who.owner_id,
                   detail={"input": body.get("input_credential"),
                           "output": body.get("output_credential")})
        return _task_response(rt, updated, who.owner_id)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request),
                           operation="rebindTaskCredentials", owner=who.owner_id, method="POST",
                           path=request.url.path, body=body, handler=handler)


# ---------------------------------------------------------------------------
# subtasks, timeline, logs, usage
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}/subtasks")
def list_subtasks(request: Request, task_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    _owned_task(rt, task_id, owner)
    return {"items": [views.subtask(s) for s in rt.repo.list_subtasks(task_id)]}


@router.get("/tasks/{task_id}/timeline")
def get_timeline(request: Request, task_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    task = _owned_task(rt, task_id, owner)
    events, cursor = [], None
    while len(events) < _TIMELINE_EVENT_CAP:
        page = rt.repo.list_events(resource=task_id, cursor=cursor, limit=500, owner=task.owner_id)
        events += page.items
        if not page.has_more:
            break
        cursor = page.next_cursor
    return {"items": timeline.build(task, rt.repo.list_subtasks(task_id), events)}


@router.get("/tasks/{task_id}/logs")
def get_logs(request: Request, task_id: str, stage: str | None = None,
             subtask: str | None = None, level: str | None = None, cursor: str | None = None,
             limit: int = Query(50, ge=1, le=200)):
    rt, owner = runtime(request), principal(request).owner_id
    _owned_task(rt, task_id, owner)
    if level is not None and level not in LEVEL_RANK:
        raise ApiError("validation_failed", "level 只能是 error、warn、info、debug",
                       details={"errors": [{"field": "level", "problem": "unknown level"}]})
    if stage is not None and not _STAGE_RE.match(stage):
        raise ApiError("validation_failed", "stage 的写法不对",
                       details={"errors": [{"field": "stage", "problem": "invalid name"}]})
    if subtask:
        sub = rt.repo.get_subtask(subtask)
        if sub.task_id != task_id:
            raise P.NotFound(subtask)
    page = rt.logs.page(task_id, stage=stage, subtask=ALL_RUNS if subtask is None else subtask,
                        min_level=level, cursor=cursor, limit=limit)
    return {"items": page.items, "next_cursor": page.next_cursor, "has_more": page.has_more}


@router.get("/tasks/{task_id}/usage")
def get_usage(request: Request, task_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    _owned_task(rt, task_id, owner)
    return views.usage_report(rt.repo.usage_buckets(task_id, ledger="actual"),
                              rt.repo.usage_buckets(task_id, ledger="attributed"))


@router.api_route("/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                  include_in_schema=False)
def unknown(rest: str):
    """Unknown API paths are JSON 404s - never the frontend's index.html."""
    raise ApiError("not_found", "这个接口不存在（或还没有实现）")
