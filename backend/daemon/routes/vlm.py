"""``{base}/api/v1/vlm-backends`` - VLM backends and their models (W8; 03 §2, 08 §4, D8).

A backend is a name, a kind (``ark`` / ``custom``), an OpenAI-compatible endpoint and an
API key. The key is sealed into a credential row of its own (kind ``ark`` / ``custom_vlm``,
name ``vlm-backend/<backend id>``) created and deleted together with the backend; that row's
``payload_meta`` also says whether a key is set and whether ``GET /models`` worked last time,
so the list never decrypts anything.

* Create: save, then try ``GET {endpoint}/models`` once. Listed models are saved with
  ``source: listed``; not listing is not an error (the backend stays ``unverified`` with the
  reason and the user adds models by hand).
* Add a model by hand: one minimal call first (``422 model_check_failed`` when it fails, and
  nothing is saved). The server's answer names the model it ran, which resolves an Ark
  endpoint ID (``ep-...``) to its model family for the reasoning-effort levels.
* A model's ``reasoning_effort`` must be one of its effective levels (08 §4.1) or null -
  null means the request carries no ``reasoning_effort`` field.
* Delete: 409 ``backend_in_use`` while an unfinished task uses one of its models; used only by
  finished tasks, deleting the backend needs ``?confirm=true``.
"""
from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from ..errors import ApiError
from ..repo import protocol as P
from ..secrets import views
from ..secrets import vlm as V
from ..secrets.effort import EffortNotAllowed
from ..secrets.http import audit, bad, secrets, validate_hiding, write
from ..secrets.service import BACKEND_KEY_PREFIX, VLM_SECRET_FIELDS, SecretsService
from ..util import new_id
from .common import principal, read_json_body, runtime

router = APIRouter()

_KEY_KIND = {"ark": "ark", "custom": "custom_vlm"}
_MAX_MODEL_NAME = 256


def _endpoint(raw) -> str:
    try:
        return V.normalize_endpoint(raw)
    except ValueError as err:
        raise bad(str(err), "endpoint") from None


def _name(raw) -> str:
    name = str(raw or "").strip()
    if not name:
        raise bad("名称不能为空", "name")
    return name


def _key_meta(endpoint: str, has_api_key: bool, models_listed: bool | None,
              backend_id: str) -> dict:
    meta = {"backend_id": backend_id, "endpoint": endpoint, "has_api_key": has_api_key}
    if models_listed is not None:
        meta["models_listed"] = models_listed
    return meta


def _key_metas(repo: P.Repository, owner: str) -> dict[str, dict]:
    return {c.id: (c.payload_meta or {}) for c in repo.list_credentials(owner=owner)
            if c.kind in ("ark", "custom_vlm")}


def _view(svc: SecretsService, backend: P.VlmBackend, meta: dict | None = None) -> dict:
    if meta is None:
        cred = svc.backend_credential(backend)
        meta = cred.payload_meta if cred is not None else {}
    return views.backend(backend, meta, svc.levels_for)


def _backend(repo: P.Repository, backend_id: str, owner: str) -> P.VlmBackend:
    return repo.get_vlm_backend(backend_id, owner=owner)


def _name_free(repo: P.Repository, name: str, owner: str, *, except_id: str | None = None) -> None:
    try:
        other = repo.get_vlm_backend_by_name(name, owner=owner)
    except P.NotFound:
        return
    if other.id != except_id:
        raise ApiError("name_taken", f"已经有叫「{name}」的模型服务了，请换一个名字",
                       details={"field": "name"})


def _check_effort(svc: SecretsService, model_name: str, effort: str | None) -> None:
    try:
        svc.effort.check(model_name, effort)
    except EffortNotAllowed as err:
        raise bad(err.message_zh, "reasoning_effort") from None


def _capabilities(svc: SecretsService, model_name: str, served: str | None = None) -> dict:
    caps = {"vision": None,
            "reasoning_effort_levels": list(svc.effort.levels_for(served or model_name))}
    if served and served != model_name:
        caps["served_model"] = served
    return caps


def _listed_models(svc: SecretsService, backend_id: str, names, existing=()) -> list[P.VlmModel]:
    """Rows for listed names; an existing row keeps its settings (effort, parallelism, source)."""
    by_name = {m.model_name: m for m in existing}
    out = []
    for name in names:
        old = by_name.get(name)
        caps = dict(old.capabilities or {}) if old else {}
        caps.update(_capabilities(svc, name, caps.get("served_model")))
        out.append(P.VlmModel(
            id=old.id if old else "", backend_id=backend_id, model_name=name,
            reasoning_effort=old.reasoning_effort if old else None,
            max_concurrency=old.max_concurrency if old else None,
            capabilities=caps, source=old.source if old else "listed"))
    return out


def _listing_verification(listing: V.Listing) -> tuple[str, str | None] | None:
    """What a listing says about the backend: ok, failed (bad key, no route) or nothing."""
    if listing.ok:
        return "ok", None
    failure = listing.failure
    if failure.kind in ("auth", "unreachable", "timeout"):
        return "failed", failure.reason
    return None


def _update_key_meta(svc: SecretsService, backend: P.VlmBackend, **changes) -> None:
    cred = svc.backend_credential(backend)
    if cred is None:
        return
    meta = dict(cred.payload_meta or {})
    meta.update(changes)
    if meta != cred.payload_meta:
        svc.repo.update_credential(cred.id, owner=cred.owner_id, payload_meta=meta)


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

@router.get("/vlm-backends")
def list_backends(request: Request):
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    metas = _key_metas(rt.repo, owner)
    return {"items": [_view(svc, b, metas.get(b.credential_id or "", {}))
                      for b in rt.repo.list_vlm_backends(owner=owner)]}


@router.post("/vlm-backends")
async def create_backend(request: Request):
    body = await read_json_body(request, required=True)
    validate_hiding("VlmBackendCreate", body, VLM_SECRET_FIELDS)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    name = _name(body["name"])
    kind = body["kind"]
    endpoint = _endpoint(body["endpoint"])
    api_key = str(body.get("api_key") or "").strip() or None
    if kind == "ark" and not api_key:
        raise bad("方舟的模型服务要填 API Key", "api_key")
    max_concurrency = int(body.get("max_concurrency") or 64)

    def handler() -> Response:
        _name_free(rt.repo, name, owner)
        backend_id, cred_id = new_id("vb"), new_id("cred")
        listing = svc.vlm.list_models(endpoint, api_key)   # network: outside any transaction
        now = rt.clock()
        verdict = _listing_verification(listing)
        if verdict is None:
            state, error = "unverified", listing.failure.reason
        else:
            state, error = verdict
        blob, version = svc.sealer.seal(cred_id, {"api_key": api_key or ""})
        key_row = P.Credential(
            id=cred_id, name=f"{BACKEND_KEY_PREFIX}{backend_id}", kind=_KEY_KIND[kind],
            payload_enc=blob, key_version=version, owner_id=owner,
            payload_meta=_key_meta(endpoint, bool(api_key), listing.ok, backend_id))
        backend = P.VlmBackend(
            id=backend_id, name=name, kind=kind, endpoint=endpoint, credential_id=cred_id,
            max_concurrency=max_concurrency, verify_state=state, last_verified_at=now,
            last_verify_error=error, owner_id=owner,
            models=_listed_models(svc, backend_id, listing.models))
        with rt.repo.transaction():
            created = rt.repo.create_vlm_backend(backend, key_row)
            audit(request, "vlm_backend.create", created.id,
                  {"name": name, "kind": kind, "endpoint": endpoint, "models_listed": listing.ok,
                   "models": len(listing.models), "verify_state": state})
        return JSONResponse(_view(svc, created, key_row.payload_meta), status_code=201)

    return await write(request, "createVlmBackend", handler, body=body,
                       secret_fields=VLM_SECRET_FIELDS)


@router.put("/vlm-backends/{backend_id}")
async def update_backend(request: Request, backend_id: str):
    body = await read_json_body(request, required=True)
    validate_hiding("VlmBackendUpdate", body, VLM_SECRET_FIELDS)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    new_name = _name(body["name"]) if "name" in body else None
    new_endpoint = _endpoint(body["endpoint"]) if "endpoint" in body else None
    new_key = str(body.get("api_key") or "").strip() or None      # empty = unchanged

    def handler() -> Response:
        backend = _backend(rt.repo, backend_id, owner)
        if new_name is not None and new_name != backend.name:
            _name_free(rt.repo, new_name, owner, except_id=backend.id)
        cred = svc.backend_credential(backend)
        old_key = svc.backend_api_key(backend)
        endpoint = new_endpoint or backend.endpoint
        api_key = new_key or old_key
        changed = endpoint != backend.endpoint or api_key != old_key
        verification = None
        if changed:
            verification = _verify(svc, dataclasses.replace(backend, endpoint=endpoint), api_key)
        fields: dict = {}
        if new_name is not None and new_name != backend.name:
            fields["name"] = new_name
        if endpoint != backend.endpoint:
            fields["endpoint"] = endpoint
        if "max_concurrency" in body:
            fields["max_concurrency"] = int(body["max_concurrency"])
        with rt.repo.transaction():
            key_id = cred.id if cred is not None else None
            meta = dict(cred.payload_meta or {}) if cred is not None else {}
            listed = verification.listed if verification is not None else meta.get("models_listed")
            meta.update(_key_meta(endpoint, bool(api_key), listed, backend.id))
            if cred is None and api_key:
                key_id = new_id("cred")
                blob, version = svc.sealer.seal(key_id, {"api_key": api_key})
                rt.repo.create_credential(P.Credential(
                    id=key_id, name=f"{BACKEND_KEY_PREFIX}{backend.id}",
                    kind=_KEY_KIND[backend.kind], payload_enc=blob, key_version=version,
                    payload_meta=meta, owner_id=owner))
                fields["credential_id"] = key_id
            elif cred is not None:
                sealed = {}
                if new_key and new_key != old_key:
                    sealed["payload_enc"], sealed["key_version"] = svc.sealer.seal(
                        cred.id, {"api_key": new_key})
                if meta != cred.payload_meta:
                    sealed["payload_meta"] = meta
                if sealed:
                    rt.repo.update_credential(cred.id, owner=cred.owner_id, **sealed)
            updated = rt.repo.update_vlm_backend(backend.id, owner=owner, **fields)
            if verification is not None:
                rt.repo.set_vlm_backend_verification(backend.id, verification.state,
                                                     verification.at, verification.error)
                updated = rt.repo.get_vlm_backend(backend.id, owner=owner)
            audit(request, "vlm_backend.update", backend.id,
                  {"name": updated.name, "fields": sorted(body)})
        return JSONResponse(_view(svc, updated))

    return await write(request, "updateVlmBackend", handler, body=body,
                       secret_fields=VLM_SECRET_FIELDS)


@router.delete("/vlm-backends/{backend_id}")
async def delete_backend(request: Request, backend_id: str, confirm: bool = Query(False)):
    await read_json_body(request, required=False)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)

    def handler() -> Response:
        backend = _backend(rt.repo, backend_id, owner)
        active, historical = svc.backend_references(backend, owner=owner)
        refs = {"active_tasks": active, "historical_tasks": historical}
        if active:
            raise ApiError("backend_in_use",
                           f"模型服务「{backend.name}」正被 {active} 个未结束的任务使用，不能删除",
                           details=refs)
        if historical and not confirm:
            raise ApiError("backend_in_use",
                           f"有 {historical} 个已结束的任务用过模型服务「{backend.name}」。删除后，"
                           "重试这些任务要先换一个模型服务；确认删除请带 confirm=true",
                           details={**refs, "confirm_required": True})
        with rt.repo.transaction():
            rt.repo.delete_vlm_backend(backend.id, owner=owner)
            audit(request, "vlm_backend.delete", backend.id,
                  {"name": backend.name, "historical_tasks": historical})
        return Response(status_code=204)

    return await write(request, "deleteVlmBackend", handler)


@dataclasses.dataclass(frozen=True)
class _Check:
    state: str
    at: int
    error: str | None
    listed: bool                     # whether GET /models worked (kept as models_listed)


def _verify(svc: SecretsService, backend: P.VlmBackend, api_key: str | None) -> _Check:
    """GET /models; when that does not work, one minimal call with the first model."""
    listing = svc.vlm.list_models(backend.endpoint, api_key)
    now = svc.clock()
    if listing.ok:
        return _Check("ok", now, None, True)
    if backend.models:
        model = backend.models[0]
        call = svc.vlm.minimal_call(backend.endpoint, api_key, model.model_name)
        if call.ok:
            return _Check("ok", now, None, False)
        return _Check("failed", now, f"模型 {model.model_name} 调用失败：{call.failure.reason}",
                      False)
    verdict = _listing_verification(listing)
    if verdict is not None:
        return _Check(verdict[0], now, verdict[1], False)
    return _Check("unverified", now, f"{listing.failure.reason}；添加一个模型之后可以再验证", False)


@router.post("/vlm-backends/{backend_id}/verify")
async def verify_backend(request: Request, backend_id: str):
    await read_json_body(request, required=False)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)

    def handler() -> Response:
        backend = _backend(rt.repo, backend_id, owner)
        check = _verify(svc, backend, svc.backend_api_key(backend))
        with rt.repo.transaction():
            rt.repo.set_vlm_backend_verification(backend.id, check.state, check.at, check.error)
            _update_key_meta(svc, backend, models_listed=check.listed)
        return JSONResponse(views.verify_result(check.state, check.at, check.error))

    return await write(request, "verifyVlmBackend", handler)


@router.post("/vlm-backends/{backend_id}/refresh-models")
async def refresh_models(request: Request, backend_id: str):
    await read_json_body(request, required=False)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)

    def handler() -> Response:
        backend = _backend(rt.repo, backend_id, owner)
        listing = svc.vlm.list_models(backend.endpoint, svc.backend_api_key(backend))
        verdict = _listing_verification(listing)
        rows: list[P.VlmModel] = []
        with rt.repo.transaction():
            if listing.ok:
                for model in _listed_models(svc, backend.id, listing.models, backend.models):
                    rows.append(rt.repo.upsert_vlm_model(model))
            _update_key_meta(svc, backend, models_listed=listing.ok)
            if verdict is not None:
                rt.repo.set_vlm_backend_verification(backend.id, verdict[0], rt.clock(),
                                                     verdict[1])
            audit(request, "vlm_backend.refresh_models", backend.id,
                  {"listed": listing.ok, "models": len(rows)})
        out: dict = {"listed": listing.ok, "models": [views.model(m, svc.levels_for(m))
                                                      for m in rows]}
        if not listing.ok:
            out["note"] = listing.failure.reason
        elif listing.truncated:
            out["note"] = f"模型太多，只保存了前 {V.MAX_LISTED} 个"
        return JSONResponse(out)

    return await write(request, "refreshVlmModels", handler)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def _owned_model(repo: P.Repository, backend_id: str, model_id: str,
                 owner: str) -> tuple[P.VlmBackend, P.VlmModel]:
    backend = _backend(repo, backend_id, owner)
    model = next((m for m in backend.models if m.id == model_id), None)
    if model is None:
        raise P.NotFound(model_id)
    return backend, model


@router.post("/vlm-backends/{backend_id}/models")
async def add_model(request: Request, backend_id: str):
    body = await read_json_body(request, required=True)
    validate_hiding("VlmModelCreate", body, ())
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    model_name = str(body["model_name"]).strip()
    if not model_name or len(model_name) > _MAX_MODEL_NAME or any(c.isspace() for c in model_name):
        raise bad("Model ID 或推理接入点 ID 不能为空，也不能有空格", "model_name")
    effort = body.get("reasoning_effort")
    _check_effort(svc, model_name, effort)

    def handler() -> Response:
        backend = _backend(rt.repo, backend_id, owner)
        if any(m.model_name == model_name for m in backend.models):
            raise ApiError("name_taken", f"模型服务「{backend.name}」下已经有 {model_name} 了",
                           details={"field": "model_name"})
        result = svc.vlm.minimal_call(backend.endpoint, svc.backend_api_key(backend), model_name,
                                      effort)
        now = rt.clock()
        if not result.ok:
            failure = result.failure
            if failure.kind in ("auth", "unreachable"):
                rt.repo.set_vlm_backend_verification(backend.id, "failed", now, failure.reason)
            raise ApiError("model_check_failed",
                           f"模型 {model_name} 没有通过调用检查，没有保存：{failure.reason}",
                           details=failure.to_json())
        if result.served_model and effort is not None:
            _check_effort(svc, result.served_model, effort)   # an ep-... resolved to its family
        with rt.repo.transaction():
            saved = rt.repo.upsert_vlm_model(P.VlmModel(
                id="", backend_id=backend.id, model_name=model_name, reasoning_effort=effort,
                max_concurrency=body.get("max_concurrency"),
                capabilities=_capabilities(svc, model_name, result.served_model),
                source="manual"))
            rt.repo.set_vlm_backend_verification(backend.id, "ok", now, None)
            audit(request, "vlm_model.add", backend.id,
                  {"model": model_name, "model_id": saved.id, "reasoning_effort": effort})
        return JSONResponse(views.model(saved, svc.levels_for(saved)), status_code=201)

    return await write(request, "addVlmModel", handler, body=body)


@router.patch("/vlm-backends/{backend_id}/models/{model_id}")
async def update_model(request: Request, backend_id: str, model_id: str):
    body = await read_json_body(request, required=True)
    validate_hiding("VlmModelPatch", body, ())
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)

    def handler() -> Response:
        backend, model = _owned_model(rt.repo, backend_id, model_id, owner)
        fields: dict = {}
        if "reasoning_effort" in body:
            caps = model.capabilities or {}
            _check_effort(svc, caps.get("served_model") or model.model_name,
                          body["reasoning_effort"])
            fields["reasoning_effort"] = body["reasoning_effort"]
        if "max_concurrency" in body:
            fields["max_concurrency"] = body["max_concurrency"]
        with rt.repo.transaction():
            updated = rt.repo.update_vlm_model(model.id, **fields)
            audit(request, "vlm_model.update", backend.id,
                  {"model": model.model_name, "model_id": model.id, **fields})
        return JSONResponse(views.model(updated, svc.levels_for(updated)))

    return await write(request, "updateVlmModel", handler, body=body)


@router.delete("/vlm-backends/{backend_id}/models/{model_id}")
async def delete_model(request: Request, backend_id: str, model_id: str):
    await read_json_body(request, required=False)
    rt, owner = runtime(request), principal(request).owner_id

    def handler() -> Response:
        backend, model = _owned_model(rt.repo, backend_id, model_id, owner)
        try:
            with rt.repo.transaction():
                rt.repo.delete_vlm_model(model.id)
                audit(request, "vlm_model.delete", backend.id,
                      {"model": model.model_name, "model_id": model.id})
        except P.Conflict as err:
            if err.code != "backend_in_use":
                raise
            raise ApiError("backend_in_use",
                           f"模型 {model.model_name} 正被未结束的任务使用，不能移除") from None
        return Response(status_code=204)

    return await write(request, "deleteVlmModel", handler)
