"""One error body for every failure (C4 ``components/schemas/Error``; design doc 03, section 1).

``{"error": {"code": ..., "message": ..., "details": {...}}}`` - ``code`` is the
stable string agents branch on, ``message`` is Chinese for people and shown by
the UI as is. Repository exceptions, request validation, unknown routes and
unexpected crashes all end up here; nothing else reaches a client.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from .pagination import CursorError
from .repo.protocol import Conflict, NotFound, PreconditionFailed, StateConflict

log = logging.getLogger("daemon.errors")

#: code -> HTTP status, exactly the table of C4 ``components/responses/Error``.
STATUS: dict[str, int] = {
    "validation_failed": 400,
    "unauthorized": 401,
    "not_found": 404,
    "task_state_conflict": 409,
    "subtask_active": 409,
    "credential_in_use": 409,
    "backend_in_use": 409,
    "dataset_in_use": 409,
    "name_taken": 409,
    "preflight_expired": 409,
    "source_changed": 409,
    "result_changed": 409,
    "idempotency_conflict": 409,
    "precondition_failed": 412,
    "precheck_failed": 422,
    "confirm_path_mismatch": 422,
    "model_check_failed": 422,
    "internal": 500,
}

#: Default wording per code, used when the raiser has nothing more specific to say.
DEFAULT_MESSAGE: dict[str, str] = {
    "validation_failed": "请求内容不符合要求",
    "unauthorized": "需要登录：请输入质检平台的账号和密码",
    "not_found": "要找的对象不存在，可能已被删除",
    "task_state_conflict": "任务当前的状态不允许这个操作，请刷新后再试",
    "subtask_active": "这个任务还有子任务没结束，请等它结束后再试",
    "credential_in_use": "这个访问密钥正被未结束的任务使用，不能删除",
    "backend_in_use": "这个模型服务正被未结束的任务使用，不能删除",
    "dataset_in_use": "这个数据集正被未结束的任务使用，不能删除登记",
    "name_taken": "名称已被占用，请换一个",
    "preflight_expired": "预检结果已过期，请重新预检",
    "source_changed": "源数据在预检之后发生了变化，需要重新预检",
    "result_changed": "结果版本已更新，请从第一页重新加载",
    "idempotency_conflict": "同一个 Idempotency-Key 已用于内容不同的请求",
    "precondition_failed": "任务已被别人修改，请刷新后再改",
    "precheck_failed": "开始前的检查没有通过",
    "confirm_path_mismatch": "确认的路径与服务端计算的不一致",
    "model_check_failed": "模型调用检查没有通过",
    "internal": "服务内部出错，请稍后重试；如果一直出现，请联系管理员",
}


class ApiError(Exception):
    """Raise anywhere in a route; becomes the Error body with the code's status."""

    def __init__(self, code: str, message: str | None = None, *, details: dict | None = None,
                 status: int | None = None, headers: dict[str, str] | None = None):
        if code not in STATUS:
            raise ValueError(f"unknown error code {code!r}; the OpenAPI Error enum is closed")
        super().__init__(message or DEFAULT_MESSAGE[code])
        self.code = code
        self.message = message or DEFAULT_MESSAGE[code]
        self.details = details
        self.status = status or STATUS[code]
        self.headers = headers


def error_body(code: str, message: str | None = None, details: dict | None = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message or DEFAULT_MESSAGE[code]}
    if details:
        err["details"] = details
    return {"error": err}


def error_response(code: str, message: str | None = None, *, details: dict | None = None,
                   status: int | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status or STATUS[code],
                        headers=headers)


# ---------------------------------------------------------------------------
# validation messages people can read
# ---------------------------------------------------------------------------

def _field(path: Iterable[Any]) -> str:
    parts = [str(p) for p in path]
    return ".".join(parts) if parts else "请求体"


def describe_schema_error(err) -> str:
    """One jsonschema ``ValidationError`` in Chinese (the English original goes to details)."""
    field = _field(err.absolute_path)
    validator = err.validator
    if validator == "required":
        missing = err.message.split("'")[1] if "'" in err.message else ""
        where = "" if field == "请求体" else f"{field} 里"
        return f"{where}缺少必填字段 {missing}".strip()
    if validator == "additionalProperties":
        extra = err.message.split("'")[1] if "'" in err.message else ""
        return f"{field} 里有不认识的字段 {extra}"
    if validator == "type":
        return f"{field} 的类型不对"
    if validator in ("enum", "const"):
        return f"{field} 的取值不在允许范围内"
    if validator in ("minLength", "maxLength"):
        return f"{field} 的长度不符合要求"
    if validator in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        return f"{field} 的数值超出允许范围"
    if validator in ("minItems", "maxItems"):
        return f"{field} 的条目数不符合要求"
    if validator == "minProperties":
        return f"{field} 至少要给一个字段"
    if validator == "pattern":
        return f"{field} 的写法不对"
    if validator in ("oneOf", "anyOf"):
        return f"{field} 的格式不对"
    return f"{field} 不符合要求"


def validation_error(errors: list, *, summary: str | None = None) -> ApiError:
    """``errors`` are jsonschema errors; the first one becomes the message."""
    errors = sorted(errors, key=lambda e: (len(list(e.absolute_path)), str(e.absolute_path)))
    first = describe_schema_error(errors[0]) if errors else ""
    message = summary or (f"请求内容不符合要求：{first}" if first else DEFAULT_MESSAGE["validation_failed"])
    if len(errors) > 1:
        message += f"（另有 {len(errors) - 1} 处问题）"
    details = {"errors": [{"field": _field(e.absolute_path), "problem": e.message}
                          for e in errors[:20]]}
    return ApiError("validation_failed", message, details=details)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

_LOCATION = {"query": "查询参数 ", "path": "路径参数 ", "header": "请求头 ", "body": "请求体 "}


def _from_request_validation(exc: RequestValidationError) -> JSONResponse:
    errors = list(exc.errors())
    problems = [{"field": ".".join(str(x) for x in e.get("loc", ())),
                 "problem": str(e.get("msg", ""))} for e in errors[:20]]
    loc = [str(x) for x in (errors[0].get("loc", ()) if errors else ())]
    kind = _LOCATION.get(loc[0], "") if loc else ""
    name = ".".join(loc[1:] if kind else loc) or "请求"
    message = f"请求参数不合法：{kind}{name} 的取值不对"
    if len(errors) > 1:
        message += f"（另有 {len(errors) - 1} 处问题）"
    return error_response("validation_failed", message, details={"errors": problems})


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc.code, exc.message, details=exc.details, status=exc.status,
                              headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError):
        return _from_request_validation(exc)

    @app.exception_handler(CursorError)
    async def _cursor(request: Request, exc: CursorError):
        return error_response("validation_failed", "翻页游标无效：请从第一页重新加载",
                              details={"errors": [{"field": "cursor", "problem": str(exc)}]})

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return error_response("not_found")

    @app.exception_handler(StateConflict)
    async def _state_conflict(request: Request, exc: StateConflict):
        return error_response("task_state_conflict")

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict):
        code = exc.code if STATUS.get(exc.code) == 409 else "task_state_conflict"
        return error_response(code)

    @app.exception_handler(PreconditionFailed)
    async def _precondition(request: Request, exc: PreconditionFailed):
        return error_response("precondition_failed")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return error_response("not_found", "这个地址不存在")
        if exc.status_code == 405:
            # the Error enum has no method_not_allowed: keep the HTTP status, say not_found
            return error_response("not_found", "这个地址不支持该请求方法", status=405,
                                  headers=dict(exc.headers or {}))
        if exc.status_code == 401:
            return error_response("unauthorized")
        if 400 <= exc.status_code < 500:
            return error_response("validation_failed", str(exc.detail or "") or None,
                                  status=exc.status_code)
        return error_response("internal", status=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.error("unhandled error on %s %s: %s", request.method, request.url.path,
                  type(exc).__name__, exc_info=exc)
        return error_response("internal")
