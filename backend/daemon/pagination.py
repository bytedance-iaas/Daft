"""Opaque cursors for "scroll down" lists (design doc 01, section 4; D21).

A cursor carries the sort key of the last row a page returned, plus the listing
it belongs to and a digest of the filters it was issued under. Handing a cursor
to another listing, or to the same listing with different filters, is a client
bug and is rejected (-> 400 ``validation_failed``) instead of silently
returning rows from somewhere else.

Cursors are not secret and not signed: a forged one can only move the start of
a page inside rows the caller may already read (every query is owner scoped).
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from .util import canonical_json, sha256_hex

CURSOR_VERSION = 1


class CursorError(ValueError):
    """A cursor that is malformed or belongs to another listing or filter set."""


def _scope_digest(scope: Any) -> str:
    return sha256_hex(canonical_json(scope))[:16]


def encode_cursor(kind: str, key: Any, *, scope: Any = None) -> str:
    """``kind`` names the listing (``events``, ``logs``...), ``key`` is JSON-able."""
    payload = {"v": CURSOR_VERSION, "k": kind, "s": _scope_digest(scope), "p": key}
    raw = canonical_json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str, kind: str, *, scope: Any = None) -> Any:
    """The key stored by :func:`encode_cursor`; raises :class:`CursorError`."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
        raise CursorError("cursor is empty or too long")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeError) as err:
        raise CursorError(f"cursor is not readable: {err}") from None
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise CursorError("cursor has an unknown version")
    if payload.get("k") != kind:
        raise CursorError(f"cursor belongs to another listing ({payload.get('k')!r})")
    if payload.get("s") != _scope_digest(scope):
        raise CursorError("cursor was issued for different filters")
    if "p" not in payload:
        raise CursorError("cursor has no position")
    return payload["p"]
