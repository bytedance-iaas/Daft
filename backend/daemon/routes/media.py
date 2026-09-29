"""Browser-facing TOS access with a task's keys (W8; design docs 03 §7, 08 §6; D16).

* ``GET /media/sign`` - a presigned GET URL for a video or evidence frame. The browser fetches
  it straight from TOS; the Daemon never relays the bytes. Two scopes, each with its own key
  and prefix: ``delivery`` = the task's run directory ``<delivery>/<run_id>/`` (output key),
  ``input`` = the task's input dataset (input key; the public cache bucket is not signed, the
  plain public URL is returned). ``path`` is relative to that prefix and checked by
  :mod:`daemon.secrets.presign` before anything is signed. TTL 60-3600 s, default 30 minutes.
* ``POST /deliveries/probe`` - the new-task form's check when the delivery directory loses
  focus: a real write of a probe object with the named key, deleted right after.
* ``POST /datasets/{id}/sign`` - the ReRun web viewer's reads of a registered TOS dataset
  (design doc 15; D55): S3 SigV4 presigned URLs on the public S3-compatible endpoint, one per
  requested object read or listing page, with the key bound to the registration. Bucket, key
  and region come from the registration, never from the caller; object keys and listing
  prefixes must lie under the dataset's prefix and are checked, not rewritten. Only reads are
  signed and the secret key never leaves the Daemon. ``dataset.viewer_sign`` is audited at
  most once an hour per person and dataset: the viewer signs on every read.
"""
from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from .. import taskspec
from ..errors import ApiError
from ..repo import protocol as P
from ..secrets import sigv4
from ..secrets import tos as T
from ..secrets.http import audit, bad, secrets, unavailable, write
from ..secrets.prechecks import TOS_CODES
from ..secrets.presign import (
    BadPath,
    browser_url,
    list_prefix_under,
    object_key_under,
    relative_key,
)
from ..secrets.scrub import Scrubber
from ..secrets.service import Unavailable
from . import datasets as _datasets  # noqa: F401 - registers the ds_id path convertor
from .common import in_thread, principal, read_json_body, runtime, validate

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}

#: ``dataset.viewer_sign`` is written at most this often per person and dataset.
VIEWER_SIGN_AUDIT_MS = 3600 * 1000
_AUDIT_LOCK = threading.Lock()


@router.get("/media/sign")
def sign_media(request: Request, task: str = Query(...),
               scope: Literal["delivery", "input"] = Query(...), path: str = Query(...),
               ttl: int = Query(1800, ge=60, le=3600)):
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    row = rt.repo.get_task(task, owner=owner)
    try:
        rel = relative_key(path)                       # refuse a bad path before any lookup
    except BadPath as err:
        raise bad(err.message_zh, "path") from None
    key = None
    try:
        if scope == "delivery":
            if not row.run_id:
                raise ApiError("not_found", "这个任务还没有开始，交付目录里还没有它的文件")
            key = svc.tos_key(row.output_cred_id, owner=owner, role="output")
            uri, region = f"{row.output_uri}/{row.run_id}", row.output_region or key.region
        elif row.input_source == "local":
            raise bad("本地路径的数据集没法在浏览器里直接打开", "scope")
        elif row.input_source == "public":             # anonymous bucket: not signed
            uri, region = row.input_uri, row.input_region
        else:
            key = svc.tos_key(row.input_cred_id, owner=owner, role="input")
            uri, region = row.input_uri, row.input_region or key.region
    except Unavailable as err:
        raise unavailable(err) from None
    try:
        url = browser_url(svc, uri, rel, ttl_s=ttl, key=key, region=region)
    except BadPath as err:
        raise bad(err.message_zh, "path") from None
    except Exception as exc:  # noqa: BLE001 - signing is local; whatever failed, say it cleanly
        scrub = key.scrubber() if key is not None else Scrubber()
        raise ApiError("internal", f"签名失败：{T.classify(exc, scrub).detail}") from None
    return JSONResponse({"url": url, "expires_at": rt.clock() + ttl * 1000}, headers=_NO_STORE)


@router.post("/deliveries/probe")
async def probe_delivery(request: Request):
    body = await read_json_body(request, required=True)
    validate("DeliveryProbeRequest", body)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    uri = taskspec.normalize_tos_uri(body["uri"], field_name="uri")

    def handler() -> Response:
        cred_id = taskspec.credential_id(rt.repo, body["credential"], owner, "credential")
        key = svc.tos_key(cred_id, owner=owner, role="output")
        region = body.get("region") or key.region
        try:
            with svc.tos(key, region) as (client, ends):
                outcome = T.write_probe(client, uri, key=key, region=ends.region,
                                        endpoint=ends.server)
        except Exception as exc:  # noqa: BLE001 - e.g. the client could not be built
            ends = svc.tos_endpoints(key, region)
            failure = T.classify(exc, key.scrubber())
            outcome = T.ProbeOutcome(False, failure.kind,
                                     T.reason(failure, action="写入", uri=uri, key=key,
                                              region=ends.region, endpoint=ends.server), failure)
        out: dict = {"ok": outcome.ok}
        if outcome.kind != "ok":
            out["error"] = {"code": TOS_CODES.get(outcome.kind, outcome.kind),
                            "message": outcome.reason}
        return JSONResponse(out)

    return await write(request, "probeDelivery", handler, body=body)


# ---------------------------------------------------------------------------
# the ReRun viewer's reads of a registered dataset (design doc 15)
# ---------------------------------------------------------------------------

def _viewer_scope(ds: P.Dataset) -> tuple[str, str]:
    """(bucket, the dataset's prefix in it) - or why this dataset cannot be signed for."""
    if ds.source == "public":
        raise bad("HuggingFace 缓存桶是公开桶，ReRun 直接读，不用签名", "source")
    if ds.source != "tos":
        raise bad("本地路径的数据集 ReRun 读不到，没法签名", "source")
    bucket, base = T.split_uri(ds.uri)
    if not base:
        raise bad("数据集登记的是整个存储桶，签名范围没法限定在一个目录之内", "uri")
    return bucket, base


def _checked_requests(items: list[dict], base: str) -> list[tuple[str, dict]]:
    """(object key, query) per request, in order; every bad item is reported, none signed."""
    out, errors = [], []
    for i, item in enumerate(items):
        field = "key" if item["op"] == "get" else "prefix"
        try:
            if item["op"] == "get":
                out.append((object_key_under(item["key"], base), {}))
                continue
            query = {"list-type": "2", "max-keys": str(item.get("max_keys", 1000)),
                     "prefix": list_prefix_under(item["prefix"], base)}
            if "delimiter" in item:
                query["delimiter"] = item["delimiter"]
            if "continuation_token" in item:
                query["continuation-token"] = item["continuation_token"]
            out.append(("", query))
        except BadPath as err:
            errors.append((i, field, err.message_zh))
    if errors:
        i, _, first = errors[0]
        message = f"第 {i + 1} 项：{first}"
        if len(errors) > 1:
            message += f"（另有 {len(errors) - 1} 项有问题）"
        raise ApiError("validation_failed", message, details={"errors": [
            {"field": f"requests.{i}.{field}", "problem": text} for i, field, text in errors[:20]]})
    return out


def _dataset_key(svc, ds: P.Dataset, owner: str) -> T.TosKey:
    gone = ("数据集绑定的访问密钥已被删除：到质检台的「数据集」页重新添加这个数据集，"
            "选一个能读它的访问密钥")
    try:
        key = svc.tos_key(ds.credential_id, owner=owner, role="input")
    except Unavailable as err:
        if err.code == "credential_missing":
            raise ApiError("not_found", gone, details={"reason": err.code}) from None
        raise unavailable(err) from None
    if key.endpoint:
        raise bad(f"数据集绑定的{key.label}设了自定义 endpoint，暂不支持在 ReRun 里打开",
                  "credential")
    return key


def _audit_once(request: Request, rt, ds: P.Dataset, detail: dict) -> None:
    """``dataset.viewer_sign`` once an hour per person and dataset (design doc 15 §2.6)."""
    who, now = principal(request), rt.clock()
    mark = (who.owner_id, who.display_name, ds.id)
    with _AUDIT_LOCK:
        seen = getattr(rt, "viewer_sign_seen", None)
        if seen is None:
            seen = rt.viewer_sign_seen = {}
        last = seen.get(mark)
        if last is not None and now - last < VIEWER_SIGN_AUDIT_MS:
            return
        if len(seen) >= 4096:                      # forget what is past the window anyway
            for old in [m for m, at in seen.items() if now - at >= VIEWER_SIGN_AUDIT_MS]:
                del seen[old]
        seen[mark] = now
    audit(request, "dataset.viewer_sign", ds.id, detail)


@router.post("/datasets/{dataset_id:ds_id}/sign")
async def sign_dataset(request: Request, dataset_id: str):
    body = await read_json_body(request, required=True)
    validate("DatasetSignRequest", body)
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    ttl = int(body.get("ttl", 1800))

    def handler() -> Response:
        ds = rt.repo.get_dataset(dataset_id, owner=owner)
        bucket, base = _viewer_scope(ds)
        todo = _checked_requests(body["requests"], base)
        key = _dataset_key(svc, ds, owner)
        region = ds.region or key.region or T.DEFAULT_REGION
        at_ms = rt.clock() // 1000 * 1000           # SigV4 counts whole seconds
        try:
            urls = [sigv4.presign("GET", T.s3_public_endpoint(region), bucket, object_key, query,
                                  access_key_id=key.access_key_id,
                                  secret_access_key=key.secret_access_key,
                                  session_token=key.session_token, region=region, ttl_s=ttl,
                                  at_ms=at_ms)
                    for object_key, query in todo]
        except ValueError as exc:                    # local arithmetic; says nothing secret
            raise ApiError("internal", f"签名失败：{key.scrubber()(str(exc))}") from None
        _audit_once(request, rt, ds, {"name": ds.name, "credential": key.name})
        return JSONResponse({"expires_at": at_ms + ttl * 1000, "urls": urls},
                            headers=_NO_STORE)

    return await in_thread(handler)
