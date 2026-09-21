"""Task logs: newest first, cursor pages without duplicates or gaps while files keep growing."""
from __future__ import annotations

import json

import pytest

from daemon import logs as logs_mod
from daemon.logs import ALL_RUNS, TaskLogs
from daemon.pagination import CursorError

from .conftest import T0, assert_error, assert_schema, seed_task

TASK = "task_01TEST"


def _line(ts, msg, level="info", **kw):
    return {"ts": ts, "kind": "log", "level": level, "msg": msg, **kw}


def _all_pages(store, **kw):
    out, cursor = [], None
    while True:
        page = store.page(TASK, cursor=cursor, **kw)
        out += page.items
        if not page.has_more:
            assert page.next_cursor is None
            return out
        cursor = page.next_cursor


@pytest.fixture
def store(tmp_path):
    s = TaskLogs(tmp_path / "runs")
    s.append(TASK, "numeric", _line(T0 + 1, "start numeric"))
    s.append(TASK, "numeric", {"ts": T0 + 2, "kind": "progress", "stage": "check:x", "done": 1,
                               "total": 50})
    s.append(TASK, "numeric", _line(T0 + 3, "episode 18 too short", "warn", episode_index=18))
    s.append(TASK, "frame", _line(T0 + 4, "start frame"))
    s.append(TASK, "system", _line(T0 + 5, "task paused (reason=system)", "warn"))
    s.append(TASK, "vlm", _line(T0 + 6, "retry arbitration", "error", episode_index=7),
             subtask_id="sub_01RETRY")
    s.append(TASK, "numeric", _line(T0 + 7, "done numeric"))
    return s


def test_layout(store, tmp_path):
    assert store.log_path(TASK, "vlm") == tmp_path / "runs" / TASK / "logs" / "vlm.jsonl"
    assert store.log_path(TASK, "vlm", "sub_1") == tmp_path / "runs" / TASK / "logs" / "sub_1" / "vlm.jsonl"
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            store.log_path(TASK, bad)


def test_newest_first_across_files_and_runs(store):
    items = _all_pages(store, limit=2)
    assert [i["msg"] for i in items] == ["done numeric", "retry arbitration",
                                         "task paused (reason=system)", "start frame",
                                         "episode 18 too short", "start numeric"]
    for item in items:
        assert_schema("LogLine", item)
    retry = items[1]
    assert (retry["stage"], retry["subtask_id"], retry["episode_index"]) == ("vlm", "sub_01RETRY", 7)
    assert items[0]["subtask_id"] is None and items[0]["episode_index"] is None


def test_filters(store):
    msgs = lambda **kw: [i["msg"] for i in _all_pages(store, **kw)]  # noqa: E731
    assert msgs(stage="numeric") == ["done numeric", "episode 18 too short", "start numeric"]
    assert msgs(subtask="") == ["done numeric", "task paused (reason=system)", "start frame",
                                "episode 18 too short", "start numeric"]
    assert msgs(subtask="sub_01RETRY") == ["retry arbitration"]
    assert msgs(min_level="warn") == ["retry arbitration", "task paused (reason=system)",
                                      "episode 18 too short"]
    assert msgs(min_level="error") == ["retry arbitration"]
    assert msgs(stage="nothing") == []
    assert TaskLogs(store.work_dir).page("task_other").items == []


def test_appends_while_paging_never_duplicate_or_skip(store):
    first = store.page(TASK, limit=3)
    store.append(TASK, "numeric", _line(T0 + 100, "newer than the first page"))
    store.append(TASK, "late", _line(T0 + 101, "a new stage file"))
    rest = []
    cursor = first.next_cursor
    while cursor:
        page = store.page(TASK, limit=2, cursor=cursor)
        rest += page.items
        cursor = page.next_cursor
    msgs = [i["msg"] for i in first.items + rest]
    assert len(msgs) == len(set(msgs)) == 6
    assert "newer than the first page" not in msgs                        # a fresh first page has it
    assert store.page(TASK, limit=1).items[0]["msg"] == "a new stage file"


def test_half_written_and_broken_lines_are_skipped(tmp_path):
    s = TaskLogs(tmp_path)
    path = s.log_path(TASK, "vlm")
    path.parent.mkdir(parents=True)
    path.write_bytes(json.dumps(_line(T0, "complete")).encode() + b"\nnot json\n\n"
                     + json.dumps(_line(T0 + 1, "partial")).encode()[:20])
    assert [i["msg"] for i in s.page(TASK).items] == ["complete"]


def test_cursor_is_bound_to_its_filters(store):
    from daemon.pagination import encode_cursor

    page = store.page(TASK, limit=1)
    with pytest.raises(CursorError):
        store.page(TASK, limit=1, cursor=page.next_cursor, min_level="warn")
    with pytest.raises(CursorError):
        store.page(TASK, limit=1, cursor="garbage")
    scope = {"task": TASK, "stage": None, "subtask": "*", "level": None}
    for payload in (["not", "offsets"], {"/numeric": "12"}, {"/numeric": -1}, {"/numeric": True}):
        forged = encode_cursor("logs", payload, scope=scope)        # right listing, wrong shape
        with pytest.raises(CursorError):
            store.page(TASK, cursor=forged)


def test_scan_budget_still_makes_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(logs_mod, "_SCAN_BUDGET", 4096)
    monkeypatch.setattr(logs_mod, "_CHUNK", 1024)
    s = TaskLogs(tmp_path)
    s.append(TASK, "vlm", _line(T0, "the only error", "error"))
    for i in range(400):
        s.append(TASK, "vlm", _line(T0 + 1 + i, f"noise {i:04d}", "debug"))
    found, cursor, rounds = [], None, 0
    while True:
        page = s.page(TASK, min_level="error", limit=5, cursor=cursor)
        found += page.items
        rounds += 1
        if not page.has_more:
            break
        cursor = page.next_cursor
    assert [i["msg"] for i in found] == ["the only error"] and rounds > 1


def test_logs_endpoint(client_for):
    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    t = seed_task(rt.repo)
    for i in range(5):
        rt.logs.append(t.id, "vlm", _line(T0 + i, f"line {i}", "warn" if i % 2 else "info"))
    r = c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"limit": 2})
    body = r.json()
    assert_schema("openapi.yaml#/paths/~1tasks~1{id}~1logs/get/responses/200/content/"
                  "application~1json/schema", body)
    assert [i["msg"] for i in body["items"]] == ["line 4", "line 3"] and body["has_more"]
    nxt = c.get(f"/curation/api/v1/tasks/{t.id}/logs",
                params={"limit": 2, "cursor": body["next_cursor"]}).json()
    assert [i["msg"] for i in nxt["items"]] == ["line 2", "line 1"]
    warn = c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"level": "warn"}).json()
    assert [i["msg"] for i in warn["items"]] == ["line 3", "line 1"] and not warn["has_more"]
    empty = c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"subtask": ""}).json()
    assert len(empty["items"]) == 5
    assert_error(c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"level": "loud"}),
                 "validation_failed")
    assert_error(c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"limit": 500}),
                 "validation_failed")
    assert_error(c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"stage": "../x"}),
                 "validation_failed")
    assert_error(c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"cursor": "zzz"}),
                 "validation_failed")
    from daemon.pagination import encode_cursor
    forged = encode_cursor("logs", [1, 2], scope={"task": t.id, "stage": None, "subtask": "*",
                                                  "level": None})
    assert_error(c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"cursor": forged}),
                 "validation_failed")                              # not a 500
    assert_error(c.get(f"/curation/api/v1/tasks/{t.id}/logs", params={"subtask": "sub_nope"}),
                 "not_found")
    assert ALL_RUNS is logs_mod.ALL_RUNS
