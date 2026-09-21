"""``Idempotency-Key`` for write endpoints (design doc 03, section 8; P6).

The same key within 24 hours returns the first successful response instead of
doing the work again - agents retry on timeouts, and a retried ``POST /tasks``
must not create a second task. Rules:

* the key is 8-128 characters (C4 ``components/parameters/IdempotencyKey``),
  scoped by owner and operation (``operationId``);
* a replay must be the same request: method, path and body (as canonical JSON)
  are fingerprinted, and a different request under a used key is
  ``409 idempotency_conflict``;
* only 2xx responses are recorded - a rejected request changed nothing, so
  running it again is safe and lets a fixed request go through;
* concurrent requests with one key are serialized in-process (single replica,
  D2): the second waits and then replays the first;
* replays carry the header ``Idempotent-Replayed: true``.

W5 wraps ``createTask``, ``createTasksBatch``, ``taskAction``, ``retryTask``,
``continueTask``, ``reexportTask`` and ``applyAdjudication`` with :meth:`Idempotency.run`;
the W4 write endpoints use it too.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from starlette.responses import JSONResponse, Response

from .errors import ApiError
from .repo import protocol as P
from .util import canonical_json, sha256_hex

HEADER = "idempotency-key"
TTL_MS = 24 * 60 * 60 * 1000
MIN_LEN, MAX_LEN = 8, 128


class Idempotency:
    def __init__(self, repo: P.Repository, clock: Callable[[], int]):
        self._repo = repo
        self._clock = clock
        self._guard = threading.Lock()
        self._locks: dict[tuple, list] = {}     # key -> [lock, users]

    def _lock_for(self, ident: tuple) -> threading.Lock:
        with self._guard:
            entry = self._locks.setdefault(ident, [threading.Lock(), 0])
            entry[1] += 1
            return entry[0]

    def _release(self, ident: tuple) -> None:
        with self._guard:
            entry = self._locks.get(ident)
            if entry is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del self._locks[ident]

    @staticmethod
    def fingerprint(method: str, path: str, body: Any) -> str:
        return "sha256:" + sha256_hex(canonical_json({"m": method.upper(), "p": path, "b": body}))

    def run(self, *, key: str | None, operation: str, owner: str, method: str, path: str,
            body: Any, handler: Callable[[], Response]) -> Response:
        """Run ``handler`` once per key; call from a worker thread (it touches the repository)."""
        if key is None:
            return handler()
        key = key.strip()
        if not MIN_LEN <= len(key) <= MAX_LEN:
            raise ApiError("validation_failed",
                           f"Idempotency-Key 的长度要在 {MIN_LEN} 到 {MAX_LEN} 个字符之间",
                           details={"errors": [{"field": "Idempotency-Key",
                                                "problem": "length out of range"}]})
        fp = self.fingerprint(method, path, body)
        ident = (owner, operation, key)
        lock = self._lock_for(ident)
        try:
            with lock:
                now = self._clock()
                record = self._repo.get_idempotent(key=key, route=operation, owner=owner)
                if record is not None and now - record.created_at < TTL_MS:
                    stored = record.response or {}
                    if stored.get("fingerprint") != fp:
                        raise ApiError("idempotency_conflict",
                                       details={"operation": operation})
                    return _replay(stored)
                response = handler()
                if 200 <= response.status_code < 300:
                    self._repo.put_idempotent(P.IdempotencyRecord(
                        key=key, route=operation, owner_id=owner, created_at=self._clock(),
                        response={"fingerprint": fp, "status": response.status_code,
                                  "body": _json_body(response)}))
                return response
        finally:
            self._release(ident)


def _json_body(response: Response) -> Any:
    import json

    raw = getattr(response, "body", b"") or b""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _replay(stored: dict) -> Response:
    headers = {"Idempotent-Replayed": "true"}
    status = int(stored.get("status", 200))
    body = stored.get("body")
    if body is None:
        return Response(status_code=status, headers=headers)
    return JSONResponse(body, status_code=status, headers=headers)
