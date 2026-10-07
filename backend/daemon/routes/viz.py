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
from ..orchestr.service import orchestrator_of
from ..pagination import keyset_page
from ..repo.extras import dataset_format
from ..viz import service as S
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
                                   transcode: bool = False, segment: bool = False):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _camera(camera)
    return await in_thread(lambda: svc.camera_video(svc.dataset_source(dataset_id, owner), index, camera,
                                                    transcode, request.headers, segment=segment))


@router.get("/datasets/{dataset_id:ds_id}/episodes/{index}/cameras/{camera}.frames")
async def get_dataset_camera_frames(request: Request, dataset_id: str, index: int, camera: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _camera(camera)
    return await in_thread(lambda: svc.camera_frames(svc.dataset_source(dataset_id, owner), index, camera,
                                                     request.headers))


@router.get("/datasets/{dataset_id:ds_id}/episodes/{index}/cameras/{camera}.json")
async def get_dataset_camera_frame_index(request: Request, dataset_id: str, index: int, camera: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _camera(camera)
    return await in_thread(lambda: svc.camera_frame_index(svc.dataset_source(dataset_id, owner), index, camera))


@router.get("/datasets/{dataset_id:ds_id}/mapping")
async def get_dataset_mapping(request: Request, dataset_id: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    return await in_thread(lambda: S.mapping_doc(svc, rt.repo.get_dataset(dataset_id, owner=owner)))


@router.put("/datasets/{dataset_id:ds_id}/mapping")
async def put_dataset_mapping(request: Request, dataset_id: str):
    body = await read_json_body(request, required=True)
    validate("DatasetMappingPut", body)
    rt, who = runtime(request), principal(request)
    svc = viz_of(rt)

    def handler() -> Response:
        ds = S.put_mapping(svc, dataset_id, who.owner_id, body["mapping"])
        rt.repo.append_event(actor=who.display_name, action="dataset.update", resource=dataset_id, at=rt.clock(),
                             owner=who.owner_id, detail={"fields": ["viz_mapping"], "version": ds.viz_mapping_version})
        if dataset_format(ds.preflight if isinstance(ds.preflight, dict) else {}) == "unsupported":
            # the checks could not read it with the site's defaults: the mapping may be what makes
            # it readable, so the registration's preflight is taken again with it (D62)
            ds, _ = orchestrator_of(rt).datasets.repreflight(ds, who.owner_id)
            rt.repo.append_event(actor=who.display_name, action="dataset.repreflight", resource=ds.id,
                                 at=rt.clock(), owner=who.owner_id, detail={"after": "viz_mapping"})
        return JSONResponse(S.mapping_doc(svc, ds))

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="putDatasetMapping",
                           owner=who.owner_id, method="PUT", path=request.url.path, body=body, handler=handler)


@router.post("/viz/mcap-probe")
async def probe_mcap(request: Request):
    body = await read_json_body(request, required=True)
    validate("McapProbeRequest", body)
    rt, who = runtime(request), principal(request)
    svc = viz_of(rt)

    def handler() -> Response:
        return JSONResponse(S.probe(svc, body["input"], who.owner_id, file=body.get("file"),
                                    template=body.get("template")))

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="probeMcap",
                           owner=who.owner_id, method="POST", path=request.url.path, body=body, handler=handler)


@router.get("/viz/templates")
async def list_viz_templates(request: Request):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    return await in_thread(lambda: {"items": S.list_templates(svc, owner)})


@router.post("/viz/templates")
async def create_viz_template(request: Request):
    body = await read_json_body(request, required=True)
    validate("VizTemplateCreate", body)
    rt, who = runtime(request), principal(request)
    svc = viz_of(rt)

    def handler() -> Response:
        t = S.create_template(svc, who.owner_id, body["name"], body.get("description") or "", body["mapping"])
        rt.repo.append_event(actor=who.display_name, action="viz_template.create", resource=t["id"], at=rt.clock(),
                             owner=who.owner_id, detail={"name": t["name"]})
        return JSONResponse(t, status_code=201)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="createVizTemplate",
                           owner=who.owner_id, method="POST", path=request.url.path, body=body, handler=handler)


@router.delete("/viz/templates/{template_id}")
async def delete_viz_template(request: Request, template_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        if template_id.startswith("builtin:"):
            raise ApiError("validation_failed", "内置模版不能删除")
        with rt.repo.transaction():
            t = rt.repo.get_viz_template(template_id, owner=who.owner_id)
            rt.repo.delete_viz_template(template_id, owner=who.owner_id)
            rt.repo.append_event(actor=who.display_name, action="viz_template.delete", resource=template_id,
                                 at=rt.clock(), owner=who.owner_id, detail={"name": t.name})
        return Response(status_code=204)

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="deleteVizTemplate",
                           owner=who.owner_id, method="DELETE", path=request.url.path, body=None, handler=handler)


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


@router.get("/tasks/{task_id}/episodes/{index}/cameras/{camera}.frames")
async def get_task_camera_frames(request: Request, task_id: str, index: int, camera: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _camera(camera)
    return await in_thread(lambda: svc.camera_frames(svc.task_source(task_id, owner), index, camera, request.headers))


@router.get("/tasks/{task_id}/episodes/{index}/cameras/{camera}.json")
async def get_task_camera_frame_index(request: Request, task_id: str, index: int, camera: str):
    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    _camera(camera)
    return await in_thread(lambda: svc.camera_frame_index(svc.task_source(task_id, owner), index, camera))


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


@router.get("/tasks/{task_id}/episodes/{index}/eef-overlay")
async def get_task_episode_eef_overlay(request: Request, task_id: str, index: int):
    """The EEF opinion's marks, computed from the task's trajectory bundle (design doc 20)."""
    from ..viz.eef_overlay import episode_overlay

    rt, owner = runtime(request), principal(request).owner_id
    svc = viz_of(rt)
    _index(index)
    return await in_thread(lambda: episode_overlay(rt, svc, task_id, owner, index))
