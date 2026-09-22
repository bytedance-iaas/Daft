"""GET /tasks/{id}/report/tables/{table}: Parquet slices, the C1 sort whitelist, cursors."""
from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from daemon.pagination import encode_cursor

from .conftest import T0, all_pages, assert_error, assert_schema

TABLE = ("openapi.yaml#/paths/~1tasks~1{id}~1report~1tables~1{table}/get/responses/200/content/"
         "application~1json/schema")


def _file_rows(world, table: str, rev: int = 1) -> list[dict]:
    path = world.run_dir / "revisions" / f"r{rev:04d}" / "tables" / f"{table}.parquet"
    return pq.read_table(path).to_pylist()


def _get(world, table):
    return lambda **q: world.get(f"/report/tables/{table}", **q)


def test_default_order_pages_through_the_file_without_gaps_or_duplicates(world):
    rows = _file_rows(world, "visual_quality")
    assert len(rows) == 16                                 # 8 episodes x 2 cameras
    items, bodies = all_pages(_get(world, "visual_quality"), limit=3)
    for body in bodies:
        assert_schema(TABLE, body)
        assert body["revision"] == 1
        assert body["columns"][0] == "episode_index" and "camera" in body["columns"]
        assert len(body["items"]) <= 3
    assert len(bodies) == 6
    assert items == [{k: (None if v != v else v) for k, v in r.items()} for r in rows]
    # the file is sorted by episode index; the whole page is too
    assert [i["episode_index"] for i in items] == sorted(i["episode_index"] for i in items)


def test_a_single_page_of_a_hundred_rows_by_default(world):
    r = world.get("/report/tables/motion_quality")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema(TABLE, body)
    assert len(body["items"]) == 9 and body["has_more"] is False and body["next_cursor"] is None
    assert body["items"][8] == {"episode_index": 8, "verdict": "abstain", "score": None,
                                "fluency": None, "active_ratio": None, "stuck": False}


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_whitelisted_sort_keeps_missing_values_last_and_ties_in_file_order(world, order):
    items, bodies = all_pages(_get(world, "visual_quality"), limit=4, sort="score", order=order)
    for body in bodies:
        assert_schema(TABLE, body)
    scores = [i["score"] for i in items]
    known = [s for s in scores if s is not None]
    assert scores[-1] is None and scores.count(None) == 1          # ep 3's second camera
    assert known == sorted(known, reverse=(order == "desc"))
    assert len(items) == 16
    assert sorted((i["episode_index"], i["camera"]) for i in items) == sorted(
        (r["episode_index"], r["camera"]) for r in _file_rows(world, "visual_quality"))


def test_sort_outside_the_whitelist_and_unknown_tables(world):
    body = assert_error(world.get("/report/tables/visual_quality", sort="sharpness"),
                        "validation_failed")
    assert body["error"]["details"]["sortable"] == ["episode_index", "score", "camera"]
    assert_error(world.get("/report/tables/visual_quality", order="up"), "validation_failed")
    assert_error(world.get("/report/tables/visual_quality", limit=501), "validation_failed")
    assert_error(world.get("/report/tables/visual_quality", limit=0), "validation_failed")
    body = assert_error(world.get("/report/tables/nope"), "not_found")
    assert "kinematic_violations" in body["error"]["details"]["known"]


def test_a_registry_table_the_revision_does_not_have(world):
    # a revision whose report lists no table for the module (module not selected)
    import json

    rev = world.run_dir / "revisions" / "r0001" / "report.json"
    doc = json.loads(rev.read_text(encoding="utf-8"))
    for sec in doc["modules"]:
        if sec["id"] == "dedup":
            sec["tables"] = []
    rev.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    body = assert_error(world.get("/report/tables/dedup_groups"), "not_found")
    assert body["error"]["details"] == {"table": "dedup_groups", "revision": 1}


def test_empty_and_small_tables(world):
    r = world.get("/report/tables/kinematic_violations")
    assert r.status_code == 200
    body = r.json()
    assert_schema(TABLE, body)
    assert [i["episode_index"] for i in body["items"]] == [0]
    r = world.get("/report/tables/dedup_groups")
    assert r.json()["items"] == [{"episode_index": 7, "verdict": "fail", "duplicate_of": 0}]


def test_cursor_is_bound_to_the_revision_the_table_and_the_sort(world):
    first = world.get("/report/tables/visual_quality", limit=5, sort="score", order="desc").json()
    cursor = first["next_cursor"]
    # another sort, another table, another task: a client bug (400)
    assert_error(world.get("/report/tables/visual_quality", limit=5, sort="score", cursor=cursor),
                 "validation_failed")
    assert_error(world.get("/report/tables/video_action_sync", limit=5, cursor=cursor),
                 "validation_failed")
    assert_error(world.get("/report/tables/visual_quality", cursor="not-a-cursor"),
                 "validation_failed")
    forged = encode_cursor("report_table", [1, 99], scope={
        "task": world.task_id, "table": "visual_quality", "sort": "score", "order": "desc"})
    assert_error(world.get("/report/tables/visual_quality", sort="score", order="desc",
                           cursor=forged), "validation_failed")

    # a new revision is switched in while the client pages: result_changed (409)
    world.decide((3, "task_verdict", "success"))
    sub = world.start_subtask(at=T0 + 10 * 60_000)
    world.apply(sub)
    world.revision(2, subtask_id=sub.id)
    world.switch(2)
    body = assert_error(world.get("/report/tables/visual_quality", limit=5, sort="score",
                                  order="desc", cursor=cursor), "result_changed")
    assert body["error"]["details"] == {"cursor_revision": 1, "revision": 2}
    # asking for revision 1 explicitly keeps paging it
    r = world.get("/report/tables/visual_quality", limit=5, sort="score", order="desc",
                  cursor=cursor, rev=1)
    assert r.status_code == 200 and r.json()["revision"] == 1
    # and a cursor of revision 1 with ?rev=2 is a revision switch too
    assert_error(world.get("/report/tables/visual_quality", limit=5, sort="score", order="desc",
                           cursor=cursor, rev=2), "result_changed")


def test_pages_across_many_row_groups(world):
    """Only the row groups holding a page's rows are read; the answer is the same."""
    path = world.run_dir / "revisions" / "r0001" / "tables" / "visual_quality.parquet"
    table = pq.read_table(path)
    pq.write_table(table, path, row_group_size=3)
    assert pq.ParquetFile(path).metadata.num_row_groups == 6
    expected = sorted(table.to_pylist(), key=lambda r: (-(r["score"] or -1), ))
    items, _ = all_pages(_get(world, "visual_quality"), limit=5, sort="score", order="desc")
    assert [(i["episode_index"], i["camera"]) for i in items] == \
        [(r["episode_index"], r["camera"]) for r in expected]


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_nan_cells_come_back_as_null_and_sort_last(world, order):
    """A NaN in a Parquet float column is a missing reading: null in JSON, last in any order."""
    import pyarrow as pa

    path = world.run_dir / "revisions" / "r0001" / "tables" / "motion_quality.parquet"
    pq.write_table(pa.table({"episode_index": [0, 1, 2, 3], "verdict": ["scored"] * 4,
                             "score": [0.5, float("nan"), None, 0.9]}), path)
    r = world.get("/report/tables/motion_quality", sort="score", order=order)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema(TABLE, body)
    assert [i["score"] for i in body["items"]] == \
        ([0.5, 0.9, None, None] if order == "asc" else [0.9, 0.5, None, None])
    assert [i["episode_index"] for i in body["items"]][2:] == [1, 2]      # ties: file order
