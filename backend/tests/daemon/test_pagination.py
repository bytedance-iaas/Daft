"""Opaque cursors: bound to their listing and filters; keyset pages never repeat or skip."""
from __future__ import annotations

import pytest

from daemon.pagination import CursorError, decode_cursor, encode_cursor, keyset_page


def test_cursor_round_trip_and_binding():
    c = encode_cursor("logs", {"/vlm": 120}, scope={"task": "t1"})
    assert decode_cursor(c, "logs", scope={"task": "t1"}) == {"/vlm": 120}
    assert "=" not in c and "/" not in c and "+" not in c                  # URL safe
    with pytest.raises(CursorError):
        decode_cursor(c, "events", scope={"task": "t1"})                  # another listing
    with pytest.raises(CursorError):
        decode_cursor(c, "logs", scope={"task": "t2"})                    # other filters
    for junk in ("", "!!!", "e30", "x" * 9000):
        with pytest.raises(CursorError):
            decode_cursor(junk, "logs")


def _walk(items, *, limit, mutate=None, **kw):
    out, cursor, rounds = [], None, 0
    while True:
        page = keyset_page(items, lambda e: e["episode_index"], kind="adjudication",
                           cursor=cursor, limit=limit, **kw)
        out += [e["episode_index"] for e in page.items]
        if mutate:
            mutate(items, rounds)
        rounds += 1
        if not page.has_more:
            assert page.next_cursor is None
            return out
        cursor = page.next_cursor


def test_keyset_pages_survive_inserts_and_removals():
    items = [{"episode_index": i} for i in range(0, 40, 2)]
    original = [e["episode_index"] for e in items]

    def mutate(items, rounds):
        if rounds == 1:                    # a new card appears before the cursor, one after it
            items.insert(0, {"episode_index": -1})
            items.append({"episode_index": 99})
            items.sort(key=lambda e: e["episode_index"])
        if rounds == 2:
            items[:] = [e for e in items if e["episode_index"] != 30]

    seen = _walk(items, limit=7, mutate=mutate)
    assert len(seen) == len(set(seen))
    assert set(original) - {30} <= set(seen) and seen == sorted(seen)


def test_keyset_scope_carries_the_revision():
    items = [{"episode_index": i} for i in range(10)]
    page = keyset_page(items, lambda e: e["episode_index"], kind="adjudication",
                       scope={"rev": 1}, limit=3)
    with pytest.raises(CursorError):
        keyset_page(items, lambda e: e["episode_index"], kind="adjudication", scope={"rev": 2},
                    cursor=page.next_cursor, limit=3)


def test_tuple_keys():
    items = [(ep, line) for ep in range(3) for line in ("label", "task_verdict")]
    got, cursor = [], None
    while True:
        page = keyset_page(items, lambda x: x, kind="q", cursor=cursor, limit=4)
        got += page.items
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert got == items
