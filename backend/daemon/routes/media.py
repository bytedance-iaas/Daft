"""Browser-facing TOS access with a task's keys (W8; design docs 03 §7, 08 §6; D16).

* ``GET /media/sign`` - a presigned GET URL for a video or evidence frame. The browser fetches
  it straight from TOS; the Daemon never relays the bytes. Two scopes, each with its own key
  and prefix: ``delivery`` = the task's run directory ``<delivery>/<run_id>/`` (output key),
  ``input`` = the task's input dataset (input key; the public cache bucket is not signed, the
  plain public URL is returned). ``path`` is relative to that prefix; it is normalized and a
  path that would leave the prefix (``..``, backslashes, control characters, encoded dots)
  is refused before anything is signed. Always signed on the public endpoint (an internal
  ``*.ivolces.com`` URL is dead in a browser); TTL 60-3600 s, default 30 minutes.
* ``POST /deliveries/probe`` - the new-task form's check when the delivery directory loses
  focus: a real write of a probe object with the named key, deleted right after.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import unquote

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from .. import taskspec
from ..errors import ApiError
from ..secrets import tos as T
from ..secrets.http import bad, secrets, unavailable, write
from ..secrets.prechecks import TOS_CODES
from ..secrets.service import Unavailable
from .common import principal, read_json_body, runtime, validate

router = APIRouter()

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NO_STORE = {"Cache-Control": "no-store"}


def relative_key(raw: str) -> str:
    """``path`` of ``/media/sign`` -> a clean relative object key, or ``validation_failed``."""
    text = str(raw or "")
    if not text.strip():
        raise bad("path 不能为空", "path")
    if "\\" in text or _CONTROL.search(text):
        raise bad("path 里不能有反斜杠或控制字符", "path")
    segments = []
    for seg in text.split("/"):
        if seg in ("", "."):
            continue
        decoded = unquote(seg)
        if seg == ".." or decoded in (".", "..") or "/" in decoded or "\\" in decoded:
            raise bad("path 只能指向这个任务目录之内的文件，不能有 ..", "path")
        segments.append(seg)
    if not segments:
        raise bad("path 没有指向任何文件", "path")
    return "/".join(segments)


def _inside(base: str, key: str) -> bool:
    return not base or key.startswith(base.rstrip("/") + "/")


@router.get("/media/sign")
def sign_media(request: Request, task: str = Query(...),
               scope: Literal["delivery", "input"] = Query(...), path: str = Query(...),
               ttl: int = Query(1800, ge=60, le=3600)):
    rt, owner = runtime(request), principal(request).owner_id
    svc = secrets(request)
    row = rt.repo.get_task(task, owner=owner)
    rel = relative_key(path)
    expires_at = rt.clock() + ttl * 1000
    try:
        if scope == "delivery":
            if not row.run_id:
                raise ApiError("not_found", "这个任务还没有开始，交付目录里还没有它的文件")
            key = svc.tos_key(row.output_cred_id, owner=owner, role="output")
            bucket, prefix = T.split_uri(row.output_uri)
            base = T.join_key(prefix, row.run_id)
            region = row.output_region or key.region
        else:
            if row.input_source == "local":
                raise bad("本地路径的数据集没法在浏览器里直接打开", "scope")
            bucket, base = T.split_uri(row.input_uri)
            if row.input_source == "public":          # anonymous bucket: the plain public URL
                object_key = T.join_key(base, rel)
                url = T.anonymous_url(bucket, object_key, row.input_region or T.DEFAULT_REGION)
                return JSONResponse({"url": url, "expires_at": expires_at}, headers=_NO_STORE)
            key = svc.tos_key(row.input_cred_id, owner=owner, role="input")
            region = row.input_region or key.region
    except Unavailable as err:
        raise unavailable(err) from None
    object_key = T.join_key(base, rel)
    if not _inside(base, object_key):
        raise bad("path 只能指向这个任务目录之内的文件", "path")
    try:
        with svc.tos(key, region, browser=True) as (client, _):
            url = T.presign_get(client, bucket, object_key, ttl)
    except Exception as exc:  # noqa: BLE001 - signing is local; whatever failed, say it cleanly
        failure = T.classify(exc, key.scrubber())
        raise ApiError("internal", f"签名失败：{failure.detail}") from None
    return JSONResponse({"url": url, "expires_at": expires_at}, headers=_NO_STORE)


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
