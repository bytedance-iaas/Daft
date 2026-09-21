"""Browser-facing TOS access with a task's keys (W8; design docs 03 §7, 08 §6; D16).

* ``GET /media/sign`` - a presigned GET URL for a video or evidence frame. The browser fetches
  it straight from TOS; the Daemon never relays the bytes. Two scopes, each with its own key
  and prefix: ``delivery`` = the task's run directory ``<delivery>/<run_id>/`` (output key),
  ``input`` = the task's input dataset (input key; the public cache bucket is not signed, the
  plain public URL is returned). ``path`` is relative to that prefix and checked by
  :mod:`daemon.secrets.presign` before anything is signed. TTL 60-3600 s, default 30 minutes.
* ``POST /deliveries/probe`` - the new-task form's check when the delivery directory loses
  focus: a real write of a probe object with the named key, deleted right after.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from .. import taskspec
from ..errors import ApiError
from ..secrets import tos as T
from ..secrets.http import bad, secrets, unavailable, write
from ..secrets.prechecks import TOS_CODES
from ..secrets.presign import BadPath, browser_url, relative_key
from ..secrets.scrub import Scrubber
from ..secrets.service import Unavailable
from .common import principal, read_json_body, runtime, validate

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}


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
