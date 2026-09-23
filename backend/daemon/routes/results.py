"""``{base}/api/v1`` result readers (W5b, F6.2): report, detail tables, the episode list,
one episode and its sync curves, performance.

The report, detail tables, episode list and view, sync curves and performance readers
use a committed result revision of the task from its run directory (:mod:`daemon.results`):
the current one (``task.result_rev``) or ``?rev=N``. A task without a result yet, a revision
out of range or missing locally, answers 404. Links (D22) are added here; the files never
hold URLs (C2 ``report.json``). The pipeline episode readers use the private SQLite index
and are available before any revision is committed.
"""
from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, Request

from ..errors import ApiError
from ..results import adjudication as A
from ..results import episode as E
from ..results import episode_list as EL
from ..results import perf as F
from ..results import live as LIVE
from ..results import sync_curves as SC
from ..results import tables as T
from ..results.files import json_safe
from ..results.store import store_of
from .common import principal, runtime

router = APIRouter()


def _task_and_revision(request: Request, task_id: str, rev: int | None):
    rt, owner = runtime(request), principal(request).owner_id
    task = rt.repo.get_task(task_id, owner=owner)
    return rt, task, store_of(rt).revision(task, rev)


def _report_links(rt, task, revision) -> list[dict]:
    tid = quote(task.id, safe="")
    current = revision.number == task.result_rev
    route = f"/tasks/{tid}/report" + ("" if current else f"?rev={revision.number}")
    links = [rt.links.link("task", "Open task", f"/tasks/{tid}"),
             rt.links.link("report", "Open QA report" if current else
                           f"Open QA report (revision {revision.number})", route)]
    if not current:
        return links
    queue = A.Queue(store_of(rt), rt.repo, task)
    by_source: dict[str, int] = {}
    for card in queue.cards("review"):
        if card.status in ("pending", "unsure"):
            for source in dict.fromkeys(q.source_module for q in card.questions):
                by_source[source] = by_source.get(source, 0) + 1
    for source, n in by_source.items():
        noun = "episode needs" if n == 1 else "episodes need"
        links.append(rt.links.link("adjudication", f"{n} {noun} human judgement ({source})",
                                   f"/tasks/{tid}/adjudication?source={quote(source, safe='')}"))
    return links


@router.get("/tasks/{task_id}/report")
def get_report(request: Request, task_id: str, rev: int | None = Query(None, ge=1)):
    rt, task, revision = _task_and_revision(request, task_id, rev)
    return {"revision": revision.number, "report": json_safe(revision.report()),
            "links": _report_links(rt, task, revision)}


@router.get("/tasks/{task_id}/report/tables/{table}")
def get_report_table(request: Request, task_id: str, table: str,
                     rev: int | None = Query(None, ge=1), cursor: str | None = None,
                     limit: int = Query(T.DEFAULT_LIMIT, ge=1, le=T.MAX_LIMIT),
                     sort: str | None = None, order: Literal["asc", "desc"] = "asc"):
    _, _, revision = _task_and_revision(request, task_id, rev)
    return T.page(revision, table, sort=sort, order=order, cursor=cursor, limit=limit)


@router.get("/tasks/{task_id}/episodes")
def list_task_episodes(request: Request, task_id: str, rev: int | None = Query(None, ge=1),
                       list_name: Literal["passed", "reject", "held"] | None = Query(None, alias="list"),
                       review: bool | None = None, q: str | None = Query(None, max_length=64),
                       cursor: str | None = None,
                       limit: int = Query(EL.DEFAULT_LIMIT, ge=1, le=EL.MAX_LIMIT)):
    rt, task, revision = _task_and_revision(request, task_id, rev)
    return EL.page(store_of(rt), rt.repo, task, revision, list_name=list_name, review=review,
                  q=q, cursor=cursor, limit=limit)


def _index(index: int) -> None:
    if index < 0:
        raise ApiError("validation_failed", "episode 下标不能是负数",
                       details={"errors": [{"field": "index", "problem": "negative"}]})


@router.get("/tasks/{task_id}/episodes/{index}")
def get_episode(request: Request, task_id: str, index: int, rev: int | None = Query(None, ge=1)):
    _index(index)
    _, _, revision = _task_and_revision(request, task_id, rev)
    return E.episode_view(revision, index)


@router.get("/tasks/{task_id}/pipeline/episodes")
def list_pipeline_episodes(request: Request, task_id: str,
                           before: int | None = Query(None, ge=0),
                           limit: int = Query(50, ge=1, le=100)):
    rt = runtime(request)
    task = rt.repo.get_task(task_id, owner=principal(request).owner_id)
    return LIVE.page(rt, task, before=before, limit=limit)


@router.get("/tasks/{task_id}/pipeline/episodes/{index}")
def get_pipeline_episode(request: Request, task_id: str, index: int):
    if index < 0:
        raise ApiError("validation_failed", "episode 下标不能是负数")
    rt = runtime(request)
    task = rt.repo.get_task(task_id, owner=principal(request).owner_id)
    return LIVE.episode(rt, task, index)


@router.get("/tasks/{task_id}/episodes/{index}/sync-curves")
def get_episode_sync_curves(request: Request, task_id: str, index: int,
                            rev: int | None = Query(None, ge=1)):
    _index(index)
    _, _, revision = _task_and_revision(request, task_id, rev)
    return SC.sync_curves(revision, index)


@router.get("/tasks/{task_id}/perf")
def get_perf(request: Request, task_id: str, rev: int | None = Query(None, ge=1),
             scope: Literal["all", "main", "subtask"] = "all", subtask: str | None = None):
    if scope == "subtask" and not subtask:
        raise ApiError("validation_failed", "scope=subtask 要同时给出 subtask（子任务 id）",
                       details={"errors": [{"field": "subtask", "problem": "required"}]})
    if scope != "subtask" and subtask is not None:
        raise ApiError("validation_failed", "subtask 只在 scope=subtask 时使用",
                       details={"errors": [{"field": "subtask", "problem": "scope is not subtask"}]})
    rt, _, revision = _task_and_revision(request, task_id, rev)
    return F.perf(revision, rt.repo, scope=scope, subtask_id=subtask)
