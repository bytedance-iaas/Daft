"""Helpers every route module shares: the runtime, the principal, JSON bodies, schemas.

Write endpoints accept only ``application/json`` and there is no CORS (design doc
03, section 1): with Basic auth the browser sends credentials by itself, so these
two rules are what keeps other sites from issuing writes. Browsers also label
cross-site requests with ``Sec-Fetch-Site: cross-site``; those writes are refused
outright (agents and the CLI send no such header).
"""
from __future__ import annotations

import functools
import json
from typing import Any, Callable

import anyio
from fastapi import Request

from curation.contracts import schemas

from ..auth import Principal, principal_of
from ..errors import ApiError, validation_error

OPENAPI = "openapi.yaml#/components/schemas/"

#: Write bodies are small JSON documents; anything bigger is refused before parsing.
MAX_BODY_BYTES = 1024 * 1024


def runtime(request: Request):
    return request.app.state.runtime


def principal(request: Request) -> Principal:
    return principal_of(request.scope)


def _is_json(content_type: str) -> bool:
    main = content_type.split(";", 1)[0].strip().lower()
    return main == "application/json" or (main.startswith("application/") and main.endswith("+json"))


def check_write_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise ApiError("validation_failed", "不接受来自其它站点的写请求")


async def read_json_body(request: Request, *, required: bool) -> Any:
    """The parsed JSON body of a write request (``None`` when optional and empty)."""
    check_write_origin(request)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ApiError("validation_failed", f"请求体太大（上限 {MAX_BODY_BYTES // 1024} KiB）")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY_BYTES:
            raise ApiError("validation_failed", f"请求体太大（上限 {MAX_BODY_BYTES // 1024} KiB）")
    raw = bytes(buf)
    content_type = request.headers.get("content-type", "")
    if not raw.strip():
        if required:
            raise ApiError("validation_failed", "请求体不能为空，请提交 JSON")
        if content_type and not _is_json(content_type):
            raise ApiError("validation_failed", "写接口只接受 JSON（Content-Type: application/json）")
        return None
    if not _is_json(content_type):
        raise ApiError("validation_failed", "写接口只接受 JSON（Content-Type: application/json）")
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError("validation_failed", "请求体不是合法的 JSON") from None


@functools.lru_cache(maxsize=None)
def _validator(ref: str):
    return schemas.validator(ref)


def validate(ref: str, instance: Any) -> None:
    """Raise ``validation_failed`` when ``instance`` does not fit a C4 schema (``ref`` as in
    :func:`curation.contracts.schemas.validate`, or a bare component name)."""
    if "#" not in ref and "/" not in ref:
        ref = OPENAPI + ref
    errors = list(_validator(ref).iter_errors(instance))
    if errors:
        raise validation_error(errors)


async def in_thread(fn: Callable, *args, **kwargs):
    """Run blocking work (the repository, files) off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def idempotency_key(request: Request) -> str | None:
    value = request.headers.get("idempotency-key")
    return value if value is not None else None
