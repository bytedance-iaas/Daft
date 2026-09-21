"""Route helpers shared by the W8 routers (credentials, VLM backends, media).

* :func:`validate_hiding` - schema validation that cannot echo a secret: jsonschema puts the
  offending value into its messages (``12345 is not of type 'string'``), so secret fields
  are swapped for neutral stand-ins of the same shape before validating.
* :func:`write` - every write goes through ``Idempotency-Key`` handling on a worker thread
  (network calls happen there, never on the event loop and never inside a transaction);
  secrets in the body are fingerprinted with a keyed hash before they reach the
  idempotency table.
* :func:`audit` - the audit event of a key or backend change (08 §7): names and field
  names, never values.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from fastapi import Request
from starlette.responses import Response

from ..errors import ApiError
from ..routes.common import idempotency_key, in_thread, principal, runtime, validate
from .service import SecretsService, Unavailable, service_of


def _stand_in(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return "x" if value else ""          # keeps minLength / "empty = unchanged" semantics
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return None


def validate_hiding(ref: str, body: Any, secret_fields: Iterable[str]) -> None:
    fields = set(secret_fields)
    shadow = body
    if isinstance(body, dict):
        shadow = {k: (_stand_in(v) if k in fields else v) for k, v in body.items()}
    validate(ref, shadow)


def bad(message: str, field: str | None = None) -> ApiError:
    details = {"errors": [{"field": field, "problem": message}]} if field else None
    return ApiError("validation_failed", message, details=details)


def unavailable(err: Unavailable) -> ApiError:
    """A lookup that failed on the server's side of things."""
    if err.code == "secret_unreadable":
        return ApiError("internal", err.message_zh, details={"reason": err.code})
    return ApiError("not_found", err.message_zh, details={"reason": err.code})


def secrets(request: Request) -> SecretsService:
    return service_of(runtime(request))


async def write(request: Request, operation: str, handler: Callable[[], Response], *,
                body: Any = None, secret_fields: Iterable[str] = ()) -> Response:
    rt, who = runtime(request), principal(request)
    fp_body = service_of(rt).fingerprint_body(body, secret_fields) if secret_fields else body

    def guarded() -> Response:
        try:
            return handler()
        except Unavailable as err:
            raise unavailable(err) from None

    return await in_thread(rt.idempotency.run, key=idempotency_key(request), operation=operation,
                           owner=who.owner_id, method=request.method, path=request.url.path,
                           body=fp_body, handler=guarded)


def audit(request: Request, action: str, resource: str, detail: dict | None = None) -> None:
    rt, who = runtime(request), principal(request)
    rt.repo.append_event(actor=who.display_name, action=action, resource=resource,
                         at=rt.clock(), detail=detail, owner=who.owner_id)
