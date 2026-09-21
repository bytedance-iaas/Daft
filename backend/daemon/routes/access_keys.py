"""``{base}/api/v1/credentials`` - TOS access keys (W8; design docs 03 §2, 08 §3-§5; D30).

* Saving checks **identity only** - one signed ``ListBuckets`` (``HeadBucket`` on the test
  bucket for keys that may not list). The result is a mark (``ok`` / ``failed`` /
  ``unverified`` with the reason), never a reason to refuse the save: users often add a key
  before the permissions are granted. Read and write permissions are checked against the
  task's own buckets right before it starts (:mod:`daemon.secrets.prechecks`).
* Secrets are sealed with the master key before they touch the database and are never in a
  response: the list shows region, endpoint, test bucket and the last four characters of the
  access key id. On update an empty secret means "unchanged".
* Delete: 409 ``credential_in_use`` while an unfinished task uses the key; used only by
  finished tasks, it needs ``?confirm=true`` (those tasks keep their reports and can be given
  another key with ``rebind-credentials``).

VLM API keys are stored with their backend (:mod:`.vlm`), not here: rows of other kinds are
invisible to these routes.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from ..errors import ApiError
from ..repo import protocol as P
from ..secrets import tos as T
from ..secrets import views
from ..secrets.http import audit, bad, secrets, validate_hiding, write
from ..secrets.service import BACKEND_KEY_PREFIX, TOS_SECRET_FIELDS, tos_meta, tos_payload
from ..util import new_id
from .common import principal, read_json_body, runtime

router = APIRouter()


def _name(raw) -> str:
    name = str(raw or "").strip()
    if not name:
        raise bad("名称不能为空", "name")
    if name.startswith(BACKEND_KEY_PREFIX):
        raise bad(f"名称不能以 {BACKEND_KEY_PREFIX} 开头（这是模型服务密钥的保留前缀）", "name")
    return name


def _endpoint(raw) -> str | None:
    try:
        return T.normalize_endpoint(raw)
    except ValueError as err:
        raise bad(str(err), "endpoint") from None


def _test_bucket(raw) -> str | None:
    bucket = str(raw or "").strip()
    if not bucket:
        return None
    if not T.valid_bucket(bucket):
        raise bad(f"测试用存储桶的名字不合法：{bucket}（3-63 位小写字母、数字、中划线）", "test_bucket")
    return bucket


def _tos_key_row(repo: P.Repository, cred_id: str, owner: str) -> P.Credential:
    cred = repo.get_credential(cred_id, owner=owner)
    if cred.kind != "tos":
        raise P.NotFound(cred_id)            # a backend's API key: managed with the backend
    return cred


def _name_free(repo: P.Repository, name: str, owner: str, *, except_id: str | None = None) -> None:
    try:
        other = repo.get_credential_by_name(name, owner=owner)
    except P.NotFound:
        return
    if other.id != except_id:
        raise ApiError("name_taken", f"已经有叫「{name}」的访问密钥了，请换一个名字",
                       details={"field": "name"})


def _view(repo: P.Repository, cred: P.Credential) -> dict:
    return views.credential(cred, repo.credential_references(cred.id))


@router.get("/credentials")
def list_access_keys(request: Request):
    rt, owner = runtime(request), principal(request).owner_id
    return {"items": [_view(rt.repo, c) for c in rt.repo.list_credentials(owner=owner, kind="tos")]}


@router.post("/credentials")
async def create_access_key(request: Request):
    body = await read_json_body(request, required=True)
    validate_hiding("CredentialCreate", body, TOS_SECRET_FIELDS)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    name = _name(body["name"])
    endpoint = _endpoint(body.get("endpoint"))
    test_bucket = _test_bucket(body.get("test_bucket"))
    access_key_id = str(body["access_key_id"]).strip()
    secret_key = str(body["secret_access_key"]).strip()
    if not access_key_id or not secret_key:
        raise bad("Access Key ID 和 Secret Access Key 都要填", "secret_access_key")

    def handler() -> Response:
        _name_free(rt.repo, name, owner)
        cred_id = new_id("cred")
        key = T.TosKey(access_key_id, secret_key, None, cred_id, name, body["region"], endpoint,
                       test_bucket)
        verification = svc.verify_tos(key)            # a network call: outside any transaction
        blob, version = svc.sealer.seal(cred_id, tos_payload(access_key_id, secret_key))
        with rt.repo.transaction():
            cred = rt.repo.create_credential(P.Credential(
                id=cred_id, name=name, kind="tos", payload_enc=blob, key_version=version,
                payload_meta=tos_meta(key), verify_state=verification.state,
                last_verified_at=verification.at, last_verify_error=verification.error,
                owner_id=owner))
            audit(request, "credential.create", cred.id,
                  {"name": name, "kind": "tos", "verify_state": verification.state})
        return JSONResponse(views.credential(cred, (0, 0)), status_code=201)

    return await write(request, "createCredential", handler, body=body,
                       secret_fields=TOS_SECRET_FIELDS)


@router.put("/credentials/{cred_id}")
async def update_access_key(request: Request, cred_id: str):
    body = await read_json_body(request, required=True)
    validate_hiding("CredentialUpdate", body, TOS_SECRET_FIELDS)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    new_name = _name(body["name"]) if "name" in body else None
    endpoint = _endpoint(body["endpoint"]) if "endpoint" in body else None
    test_bucket = _test_bucket(body["test_bucket"]) if "test_bucket" in body else None
    new_ak = str(body.get("access_key_id") or "").strip()
    new_sk = str(body.get("secret_access_key") or "").strip()

    def handler() -> Response:
        cred = _tos_key_row(rt.repo, cred_id, owner)
        current = svc.tos_key_from(cred)
        if new_name is not None and new_name != cred.name:
            _name_free(rt.repo, new_name, owner, except_id=cred.id)
        ak = new_ak or current.access_key_id              # empty = unchanged (never echoed)
        if ak != current.access_key_id and not new_sk:
            raise bad("换了 Access Key ID，请同时填写与它配对的 Secret Access Key",
                      "secret_access_key")
        sk = new_sk or current.secret_access_key
        key = T.TosKey(
            ak, sk, current.session_token, cred.id, new_name or cred.name,
            body.get("region", current.region),
            endpoint if "endpoint" in body else current.endpoint,
            test_bucket if "test_bucket" in body else current.test_bucket)
        secret_changed = (ak, sk) != (current.access_key_id, current.secret_access_key)
        identity_changed = secret_changed or (key.region, key.endpoint, key.test_bucket) != \
            (current.region, current.endpoint, current.test_bucket)
        verification = svc.verify_tos(key) if identity_changed else None
        fields: dict = {}
        if new_name is not None and new_name != cred.name:
            fields["name"] = new_name
        if secret_changed:
            fields["payload_enc"], fields["key_version"] = svc.sealer.seal(
                cred.id, tos_payload(ak, sk, current.session_token))
        meta = tos_meta(key)
        if meta != cred.payload_meta:
            fields["payload_meta"] = meta
        with rt.repo.transaction():
            rt.repo.update_credential(cred.id, owner=owner, **fields)
            if verification is not None:
                rt.repo.set_credential_verification(cred.id, verification.state,
                                                    verification.at, verification.error)
            audit(request, "credential.update", cred.id,
                  {"name": key.name, "fields": sorted(body),
                   **({"verify_state": verification.state} if verification else {})})
            updated = rt.repo.get_credential(cred.id, owner=owner)
        return JSONResponse(_view(rt.repo, updated))

    return await write(request, "updateCredential", handler, body=body,
                       secret_fields=TOS_SECRET_FIELDS)


@router.delete("/credentials/{cred_id}")
async def delete_access_key(request: Request, cred_id: str, confirm: bool = Query(False)):
    await read_json_body(request, required=False)
    rt, owner = runtime(request), principal(request).owner_id

    def handler() -> Response:
        cred = _tos_key_row(rt.repo, cred_id, owner)
        active, historical = rt.repo.credential_references(cred.id)
        refs = {"active_tasks": active, "historical_tasks": historical}
        if active:
            raise ApiError("credential_in_use",
                           f"访问密钥「{cred.name}」正被 {active} 个未结束的任务使用，不能删除；"
                           "等这些任务结束（或停止）后再删",
                           details=refs)
        if historical and not confirm:
            raise ApiError("credential_in_use",
                           f"有 {historical} 个已结束的任务用过访问密钥「{cred.name}」。删除后，"
                           "这些任务的报告和视频要重新绑定一个访问密钥才能查看；确认删除请带 confirm=true",
                           details={**refs, "confirm_required": True})
        with rt.repo.transaction():
            rt.repo.delete_credential(cred.id, owner=owner)
            audit(request, "credential.delete", cred.id,
                  {"name": cred.name, "historical_tasks": historical})
        return Response(status_code=204)

    return await write(request, "deleteCredential", handler)


@router.post("/credentials/{cred_id}/verify")
async def verify_access_key(request: Request, cred_id: str):
    await read_json_body(request, required=False)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)

    def handler() -> Response:
        cred = _tos_key_row(rt.repo, cred_id, owner)
        result = svc.verify_tos(svc.tos_key_from(cred))
        rt.repo.set_credential_verification(cred.id, result.state, result.at, result.error)
        return JSONResponse(views.verify_result(result.state, result.at, result.error))

    return await write(request, "verifyCredential", handler)
