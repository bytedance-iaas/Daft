"""``{base}/api/v1`` - the data visualizer (design doc 18, C4 2.4.0).

Dataset scope (the visualize page) reads a registration; task scope (the mini player of the report,
the adjudication cards and the live episodes) reads a task's frozen input. Both go through
:mod:`daemon.viz.service`; the routes only parse, page and answer. Readers do blocking I/O (TOS
reads, parquet), so every handler runs in the thread pool.

The task scope's ``.mp4`` route lives in ``routes/results.py`` (it existed for mcap cameras before
C4 declared it) and hands LeRobot cameras and transcodes to the same service.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from ..errors import ApiError
from ..pagination import keyset_page
from ..viz.service import viz_of
from . import datasets as _datasets  # noqa: F401 - registers the ds_id path convertor
from .common import idempotency_key, in_thread, principal, read_json_body, runtime, validate

router = APIRouter()

_CAMERA = r"[0-9A-Za-z_-]{1,96}"
_EP_Q = re.compile(r"^(?:ep\s*)?(\d+)$", re.I)


def _index(index: int) -> int:
    if index < 0:
        raise ApiError("validation_failed", "episode 下标不能是负数")
    return index


def _camera(camera: str) -> str:
    if not re.fullmatch(_CAMERA, camera or ""):
        raise ApiError("not_found", f"没有相机 {camera}")
    return camera


def _window(start: float | None, end: float | None) -> None:
    if start is not None and end is not None and end < start:
        raise ApiError("validation_failed", "to 不能早于 from",
                       details={"errors": [{"field": "to", "problem": "before from"}]})


# ---------------------------------------------------------------- dataset scope

@router.get("/datasets/{dataset_id:ds_id}/viz")
async def get_dataset_viz(request: Request, dataset_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    return await in_thread(lambda: svc.dataset(svc.dataset_source(dataset_id, owner)))


@router.get("/datasets/{dataset_id:ds_id}/viz/episodes")
async def list_dataset_viz_episodes(request: Request, dataset_id: str, q: str | None = Query(None, max_length=200),
                                    sort: Literal["index", "duration", "steps"] = "index",
                                    order: Literal["asc", "desc"] = "asc", cursor: str | None = None,
                                    limit: int = Query(50, ge=1, le=200)):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)

    def handler():
        src = svc.dataset_source(dataset_id, owner)
        items = svc.episode_items(src)
        text = (q or "").strip()
        if text:
            m = _EP_Q.match(text)
            items = [e for e in items if (e["index"] == int(m.group(1)) if m else text.lower() in e["task"].lower())]
        sign = -1 if order == "desc" else 1

        def sort_key(e: dict):
            if sort == "duration":
                return (sign * float(e["duration_s"] or 0.0), sign * e["index"])
            if sort == "steps":
                return (sign * (1 if e.get("steps") else 0), sign * e["index"])
            return (sign * e["index"],)

        items.sort(key=sort_key)
        page = keyset_page(items, key=sort_key, kind="viz-episodes",
                           scope={"dataset": dataset_id, "fp": src.fingerprint, "q": text, "sort": sort,
                                  "order": order}, cursor=cursor, limit=limit)
        return {"items": page.items, "next_cursor": page.next_cursor, "has_more": page.has_more,
                "total": len(items)}

    return await in_thread(handler)


@router.get("/datasets/{dataset_id:ds_id}/viz/meta")
async def get_dataset_viz_meta(request: Request, dataset_id: str, path: str = Query(..., min_length=1, max_length=512)):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    return await in_thread(lambda: svc.meta_file(svc.dataset_source(dataset_id, owner), path))


@router.get("/datasets/{dataset_id:ds_id}/episodes/{index}/viz")
async def get_dataset_episode_viz(request: Request, dataset_id: str, index: int):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    return await in_thread(lambda: svc.episode(svc.dataset_source(dataset_id, owner), index))


@router.get("/datasets/{dataset_id:ds_id}/episodes/{index}/series")
async def get_dataset_episode_series(request: Request, dataset_id: str, index: int, stream: str = Query(..., min_length=1, max_length=128),
                                     start: float | None = Query(None, alias="from", ge=0),
                                     end: float | None = Query(None, alias="to", ge=0),
                                     points: int = Query(2000, ge=100, le=20000)):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _window(start, end)
    return await in_thread(lambda: svc.series(svc.dataset_source(dataset_id, owner), index, stream, start, end, points))


@router.get("/datasets/{dataset_id:ds_id}/episodes/{index}/cameras/{camera}.mp4")
async def get_dataset_camera_video(request: Request, dataset_id: str, index: int, camera: str,
                                   transcode: bool = False):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _camera(camera)
    return await in_thread(lambda: svc.camera_video(svc.dataset_source(dataset_id, owner), index, camera,
                                                    transcode, request.headers))


@router.put("/datasets/{dataset_id:ds_id}/annotations")
async def put_dataset_annotations(request: Request, dataset_id: str):
    body = await read_json_body(request, required=True)
    validate("DatasetAnnotationsPut", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        upload_id = body["upload_id"]
        if upload_id is not None:
            from ..orchestr.service import orchestrator_of

            up = orchestrator_of(rt).uploads.get(who.owner_id, upload_id)
            if up.get("kind") != "viz_annotations":
                raise ApiError("validation_failed", "这个上传件不是外部标注文件（kind 应为 viz_annotations）",
                               details={"errors": [{"field": "upload_id", "problem": "wrong kind"}]})
        with rt.repo.transaction():
            rt.repo.get_dataset(dataset_id, owner=who.owner_id)
            updated = rt.repo.update_dataset(dataset_id, owner=who.owner_id, annotations_upload=upload_id)
            rt.repo.append_event(actor=who.display_name, action="dataset.update", resource=dataset_id,
                                 at=rt.clock(), owner=who.owner_id, detail={"fields": ["annotations_upload"]})
        from .datasets import _detail

        return JSONResponse(_detail(rt, updated, who.owner_id))

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="putDatasetAnnotations",
                           owner=who.owner_id, method="PUT", path=request.url.path, body=body, handler=handler)


# ---------------------------------------------------------------- task scope

@router.get("/tasks/{task_id}/viz")
async def get_task_viz(request: Request, task_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    return await in_thread(lambda: svc.dataset(svc.task_source(task_id, owner)))


@router.get("/tasks/{task_id}/episodes/{index}/viz")
async def get_task_episode_viz(request: Request, task_id: str, index: int):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    return await in_thread(lambda: svc.episode(svc.task_source(task_id, owner), index))


@router.get("/tasks/{task_id}/episodes/{index}/series")
async def get_task_episode_series(request: Request, task_id: str, index: int, stream: str = Query(..., min_length=1, max_length=128),
                                  start: float | None = Query(None, alias="from", ge=0),
                                  end: float | None = Query(None, alias="to", ge=0),
                                  points: int = Query(2000, ge=100, le=20000)):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _window(start, end)
    return await in_thread(lambda: svc.series(svc.task_source(task_id, owner), index, stream, start, end, points))
