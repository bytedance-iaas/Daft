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
import bisect
import json
from typing import Any, Callable, Sequence, TypeVar

from .util import canonical_json, sha256_hex

T = TypeVar("T")

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


def keyset_page(items: Sequence[T], key: Callable[[T], Any], *, kind: str, scope: Any = None,
                cursor: str | None = None, limit: int = 50):
    """One page of an in-memory listing, e.g. the adjudication queue or a dataset's episodes.

    ``items`` must be sorted by ``key`` with unique keys (an episode index, or a
    tuple). The cursor holds the last key returned, so rows inserted or removed
    between two requests never make a page repeat or skip an existing row.
    Put whatever the listing depends on into ``scope`` - e.g. the result revision
    (``{"task": id, "rev": 3, "source": ...}``) - so a cursor from an older
    revision is refused (-> ``validation_failed``, or ``result_changed`` if the
    caller checks the revision first) instead of mixing two versions.
    """
    from .repo.protocol import CursorPage

    if limit < 1:
        raise ValueError("limit starts at 1")
    keys = [key(it) for it in items]
    start = 0
    if cursor:
        last = decode_cursor(cursor, kind, scope=scope)
        last = tuple(last) if isinstance(last, list) else last
        start = bisect.bisect_right(keys, last)
    page = list(items[start:start + limit])
    more = start + limit < len(items)
    last_key = key(page[-1]) if page else None
    next_cursor = encode_cursor(kind, list(last_key) if isinstance(last_key, tuple) else last_key,
                                scope=scope) if more else None
    return CursorPage(items=page, next_cursor=next_cursor, has_more=more)
