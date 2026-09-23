"""``{base}/api/v1/datasets`` - registered datasets from the repository (D36, D37; C4 1.1).

Registering, re-checking and re-preflighting run the CLI (``curation preflight`` /
``curation snapshot``) and belong to W5; what is here needs the repository only:
the list, one registration with its recent checks and tasks, rename / note, and
removing the registration (the data on TOS is never touched).

Audit events (resource = the dataset id): ``dataset.update`` [fields],
``dataset.delete`` [name, source, uri].

``/datasets/{id}`` only matches ids the repository hands out (``ds_...``), so the
fixed paths next to it (``/datasets/browse``, ``/datasets/episodes``, W3) reach
their own handlers whichever router is included first.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.convertors import Convertor, register_url_convertor
from starlette.responses import JSONResponse, Response

from .. import views
from ..errors import ApiError
from ..repo import protocol as P
from ..repo.extras import DATASET_FORMATS
from .common import (
    check_page_size,
    idempotency_key,
    in_thread,
    principal,
    read_json_body,
    runtime,
    validate,
)


class _DatasetId(Convertor):
    regex = "ds_[0-9A-Za-z]+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("ds_id", _DatasetId())

router = APIRouter()

#: DatasetDetail lists at most this many checks and tasks (C4 maxItems).
SHOWN = 20
_CHECK_STATES = ("ok", "changed")
_UNFINISHED = tuple(s for s in P.TASK_TRANSITIONS if s not in P.TERMINAL_STATES)


def _tasks_on(rt, ds: P.Dataset, owner: str, n: int) -> list[P.Task]:
    """The newest ``n`` tasks on the registration (soft-deleted ones are not listed)."""
    return rt.repo.list_tasks(owner=owner, page=1, page_size=n, dataset_id=ds.id).items


def _detail(rt, ds: P.Dataset, owner: str) -> dict:
    return views.dataset_detail(ds, tasks=_tasks_on(rt, ds, owner, SHOWN),
                                checks=rt.repo.list_dataset_checks(ds.id, limit=SHOWN),
                                names=views.Names(rt.repo, owner))


@router.get("/datasets")
def list_datasets(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20),
                  q: str | None = None, fmt: str | None = Query(None, alias="format"),
                  check_state: str | None = None):
    check_page_size(page_size)
    if fmt is not None and fmt not in DATASET_FORMATS:
        raise ApiError("validation_failed",
                       "format 只能是 lerobot_v2、lerobot_v3、mcap、lance 或 unsupported",
                       details={"errors": [{"field": "format", "problem": "unknown format"}]})
    if check_state is not None and check_state not in _CHECK_STATES:
        raise ApiError("validation_failed", "check_state 只能是 ok 或 changed",
                       details={"errors": [{"field": "check_state", "problem": "unknown state"}]})
    rt, owner = runtime(request), principal(request).owner_id
    result = rt.repo.list_datasets(owner=owner, page=page, page_size=page_size,
                                   q=(q or "").strip() or None, fmt=fmt, check_state=check_state)
    items = []
    for ds in result.items:
        last = _tasks_on(rt, ds, owner, 1)
        items.append(views.dataset_item(ds, last[0] if last else None))
    return {"items": items, "page": result.page, "page_size": result.page_size,
            "total": result.total}


@router.get("/datasets/{dataset_id:ds_id}")
def get_dataset(request: Request, dataset_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    return _detail(rt, rt.repo.get_dataset(dataset_id, owner=owner), owner)


@router.patch("/datasets/{dataset_id:ds_id}")
async def update_dataset(request: Request, dataset_id: str):
    body = await read_json_body(request, required=True)
    validate("DatasetPatch", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        with rt.repo.transaction():
            rt.repo.get_dataset(dataset_id, owner=who.owner_id)
            updated = rt.repo.update_dataset(dataset_id, owner=who.owner_id, **body)
            rt.repo.append_event(actor=who.display_name, action="dataset.update",
                                 resource=dataset_id, at=rt.clock(), owner=who.owner_id,
                                 detail={"fields": sorted(body)})
        return JSONResponse(_detail(rt, updated, who.owner_id))

    return await in_thread(rt.idempotency.run, key=idempotency_key(request),
                           operation="updateDataset", owner=who.owner_id, method="PATCH",
                           path=request.url.path, body=body, handler=handler)


@router.delete("/datasets/{dataset_id:ds_id}")
async def delete_dataset(request: Request, dataset_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        with rt.repo.transaction():
            ds = rt.repo.get_dataset(dataset_id, owner=who.owner_id)
            try:
                rt.repo.delete_dataset(dataset_id, owner=who.owner_id)
            except P.Conflict as err:
                if err.code != "dataset_in_use":
                    raise
                count, users = 0, []
                for state in _UNFINISHED:
                    page = rt.repo.list_tasks(owner=who.owner_id, page=1, page_size=SHOWN,
                                              state=state, dataset_id=dataset_id)
                    count += page.total
                    users += page.items
                users.sort(key=lambda t: (t.created_at, t.id), reverse=True)
                raise ApiError(
                    "dataset_in_use",
                    f"还有 {count} 个未结束的任务在用这个数据集；等它们结束"
                    "（待启动的任务可以先删除）后再删除登记",
                    details={"count": count,
                             "tasks": [views.task_ref(t) for t in users[:SHOWN]]}) from None
            rt.repo.append_event(actor=who.display_name, action="dataset.delete",
                                 resource=dataset_id, at=rt.clock(), owner=who.owner_id,
                                 detail={"name": ds.name, "source": ds.source, "uri": ds.uri})
        return Response(status_code=204)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request),
                           operation="deleteDataset", owner=who.owner_id, method="DELETE",
                           path=request.url.path, body=None, handler=handler)
