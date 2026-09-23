"""``{base}/api/v1/uploads``: input files of module parameters (C4 1.8.0 createUpload / getUpload).

Same write rules as every other route (JSON Content-Type, same-origin), but the body is a file of
up to 64 MiB instead of a small JSON document; it is validated before it is kept
(:mod:`daemon.uploads`). The store belongs to the orchestrator.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Query, Request
from starlette.responses import JSONResponse, Response

from ..errors import ApiError
from ..uploads import MAX_UPLOAD_BYTES
from .common import _is_json, check_write_origin, idempotency_key, in_thread, principal, runtime

router = APIRouter()


def _store(rt):
    from ..orchestr.service import orchestrator_of

    return orchestrator_of(rt).uploads


async def _read_file(request: Request) -> bytes:
    check_write_origin(request)
    if not _is_json(request.headers.get("content-type", "")):
        raise ApiError("validation_failed",
                       "上传接口也只接受 JSON：请带上 Content-Type: application/json（文件内容就是请求体）")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise ApiError("validation_failed", f"文件太大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB）")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_UPLOAD_BYTES:
            raise ApiError("validation_failed", f"文件太大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB）")
    if not bytes(buf).strip():
        raise ApiError("validation_failed", "请求体是空的：文件内容就是请求体")
    return bytes(buf)


@router.post("/uploads")
async def create_upload(request: Request, kind: str = Query(...), name: str = Query(..., min_length=1,
                                                                                    max_length=200)):
    data = await _read_file(request)
    rt, who = runtime(request), principal(request)

    def handler() -> Response:
        return JSONResponse(_store(rt).put(who.owner_id, kind, name, data), status_code=201)

    # the idempotency record fingerprints the body by its digest, not the bytes (up to 64 MiB)
    fingerprint = {"kind": kind, "name": name, "sha256": hashlib.sha256(data).hexdigest()}
    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation="createUpload",
                           owner=who.owner_id, method="POST", path=request.url.path, body=fingerprint,
                           handler=handler)


@router.get("/uploads/{upload_id}")
def get_upload(request: Request, upload_id: str):
    rt, who = runtime(request), principal(request)
    return _store(rt).get(who.owner_id, upload_id)
