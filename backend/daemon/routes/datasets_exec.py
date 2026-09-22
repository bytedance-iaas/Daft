"""``{base}/api/v1`` dataset routes that read the dataset itself (W5a; D36, D37; 03 §10, §12).

preflight, browseDatasets, listDatasetEpisodes (cursor paging), createDataset,
recheckDataset and repreflightDataset. Preflight, the file listing and the
registration run the CLI (:class:`daemon.orchestr.datasets.DatasetOps`); browsing
and the episode preview read metadata in the Daemon (:mod:`daemon.orchestr.browse`).
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from .. import taskspec, views
from ..errors import ApiError
from ..orchestr.datasets import Source
from ..orchestr.service import orchestrator_of
from ..repo import protocol as P
from . import datasets as dataset_routes          # registers the ds_id path convertor
from .common import idempotency_key, in_thread, principal, read_json_body, runtime, validate

router = APIRouter()

_SOURCES = ("tos", "public", "local")


def _detail(rt, ds: P.Dataset, owner: str) -> dict:
    return dataset_routes._detail(rt, ds, owner)


def _idem(request: Request, rt, who, operation: str, body, handler):
    return in_thread(rt.idempotency.run, key=idempotency_key(request), operation=operation,
                     owner=who.owner_id, method=request.method, path=request.url.path, body=body,
                     handler=handler)


@router.post("/preflight")
async def preflight(request: Request):
    body = await read_json_body(request, required=True)
    validate("PreflightRequest", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        return JSONResponse(orchestrator_of(rt).preflight(body, who))

    return await _idem(request, rt, who, "preflight", body, handler)


def _source(source: str | None) -> str:
    if source not in _SOURCES:
        raise ApiError("validation_failed", "source 只能是 tos、public 或 local",
                       details={"errors": [{"field": "source", "problem": "unknown source"}]})
    return source


def _credential(rt, source: str, name: str | None, owner: str) -> str | None:
    if source != "tos":
        return None
    if not name:
        raise ApiError("validation_failed", "私有 TOS 要给访问密钥（credential）",
                       details={"errors": [{"field": "credential", "problem": "missing"}]})
    return taskspec.credential_id(rt.repo, name, owner, "credential")


@router.get("/datasets/browse")
def browse(request: Request, source: str | None = None, uri: str | None = None,
           region: str | None = None, credential: str | None = None,
           cursor: str | None = None, limit: int = Query(50, ge=1, le=200)):
    rt, who = runtime(request), principal(request)
    src = _source(source)
    cred_id = _credential(rt, src, credential, who.owner_id)
    if src == "tos" and uri:
        uri = taskspec.normalize_tos_uri(uri, field_name="uri")
    return orchestrator_of(rt).browser.browse(source=src, uri=uri, region=region,
                                              cred_id=cred_id, owner=who.owner_id,
                                              cursor=cursor, limit=limit)


@router.get("/datasets/episodes")
def list_episodes(request: Request, dataset_id: str | None = None, source: str | None = None,
                  uri: str | None = None, region: str | None = None,
                  credential: str | None = None, cursor: str | None = None,
                  limit: int = Query(50, ge=1, le=200)):
    rt, who = runtime(request), principal(request)
    if dataset_id:
        ds = rt.repo.get_dataset(dataset_id, owner=who.owner_id)
        src = Source.of_dataset(ds)
        if src.source == "local":
            src = Source("local", taskspec.local_path(rt.settings, ds.uri, "dataset_id"))
    else:
        if not uri:
            raise ApiError("validation_failed", "给 dataset_id，或者给 source 和 uri",
                           details={"errors": [{"field": "uri", "problem": "missing"}]})
        spec = {"source": _source(source), "uri": uri}
        if region:
            spec["region"] = region
        if credential:
            spec["credential"] = credential
        validate("InputRef", spec)
        fields = taskspec.resolve_input(rt.repo, rt.settings, spec, who.owner_id)
        src = Source(fields["input_source"], fields["input_uri"], fields.get("input_region"),
                     fields.get("input_cred_id"))
    return orchestrator_of(rt).browser.episodes(source=src.source, uri=src.uri, region=src.region,
                                                cred_id=src.credential_id, owner=who.owner_id,
                                                cursor=cursor, limit=limit)


@router.post("/datasets")
async def create_dataset(request: Request):
    body = await read_json_body(request, required=True)
    validate("DatasetCreate", body)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        fields = taskspec.resolve_input(rt.repo, rt.settings, body["input"], who.owner_id)
        src = Source(fields["input_source"], fields["input_uri"], fields.get("input_region"),
                     fields.get("input_cred_id"))
        ds, created, _ = orchestrator_of(rt).datasets.register(
            src, who.owner_id, name=body.get("name"), note=body.get("note"))
        if created:
            rt.repo.append_event(actor=who.display_name, action="dataset.create", resource=ds.id,
                                 at=rt.clock(), owner=who.owner_id,
                                 detail={"name": ds.name, "source": ds.source, "uri": ds.uri})
        return JSONResponse(_detail(rt, ds, who.owner_id), status_code=201 if created else 200)

    return await _idem(request, rt, who, "createDataset", body, handler)


@router.post("/datasets/{dataset_id:ds_id}/recheck")
async def recheck_dataset(request: Request, dataset_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        ds = rt.repo.get_dataset(dataset_id, owner=who.owner_id)
        check, _ = orchestrator_of(rt).datasets.check(ds, who.owner_id, trigger="recheck")
        rt.repo.append_event(actor=who.display_name, action="dataset.recheck", resource=ds.id,
                             at=rt.clock(), owner=who.owner_id, detail={"result": check.result})
        return JSONResponse(views.dataset_check(check))

    return await _idem(request, rt, who, "recheckDataset", None, handler)


@router.post("/datasets/{dataset_id:ds_id}/repreflight")
async def repreflight_dataset(request: Request, dataset_id: str):
    await read_json_body(request, required=False)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        ds = rt.repo.get_dataset(dataset_id, owner=who.owner_id)
        updated, _ = orchestrator_of(rt).datasets.repreflight(ds, who.owner_id)
        rt.repo.append_event(actor=who.display_name, action="dataset.repreflight",
                             resource=ds.id, at=rt.clock(), owner=who.owner_id)
        return JSONResponse(_detail(rt, updated, who.owner_id))

    return await _idem(request, rt, who, "repreflightDataset", None, handler)
