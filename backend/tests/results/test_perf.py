"""GET /tasks/{id}/perf: v1's latency buckets, split into all / main run / one subtask."""
from __future__ import annotations

import json

import pytest

from daemon.repo import protocol as P

from .conftest import MIN, T0, assert_error, assert_schema, latency_csv, main_latency, make_task

MAIN_PROBE = {"call_kind": "probe", "count": 4, "failed": 0, "hedged": 1, "p50_s": 3.0,
              "p90_s": 4.0, "p99_s": 4.0, "wall_s": 62.0}
MAIN_ENDSTATE = {"call_kind": "endstate", "count": 1, "failed": 0, "hedged": 0, "p50_s": 5.0,
                 "p90_s": 5.0, "p99_s": 5.0, "wall_s": 5.0}
MAIN_ARBITRATION = {"call_kind": "arbitration", "count": 1, "failed": 1, "hedged": 0,
                    "p50_s": 0.0, "p90_s": 0.0, "p99_s": 0.0, "wall_s": 60.0}


def _perf(world, **q):
    r = world.get("/perf", **q)
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("Perf", body)
    return body


def test_all_calls_of_the_first_revision(world):
    body = _perf(world)
    assert body["revision"] == 1 and body["scope"] == "all" and body["subtask_id"] is None
    assert body["latency"] == [MAIN_PROBE, MAIN_ENDSTATE, MAIN_ARBITRATION]
    assert body["effective_concurrency"] == 1.07
    assert body["stages"] == [{"id": "numeric", "wall_s": 10.0, "share": 0.1},
                              {"id": "frame", "wall_s": 30.0, "share": 0.3},
                              {"id": "vlm", "wall_s": 60.0, "share": 0.6}]
    assert body["merge"] == {"requests": 0, "estimated_unmerged": 0}
    assert body["redone_after_interruption"] == 0
    assert body["backend"] == {"name": "ark-prod", "kind": "ark",
                               "endpoint": "https://ark.example/api/v3", "model": "doubao-seed",
                               "reasoning_effort": None, "vlm_parallelism": 64}
    assert _perf(world, scope="main")["latency"] == body["latency"]


@pytest.fixture
def with_subtask(world):
    """An apply subtask ran 10-20 minutes after the start and made revision 2; revision 1 was
    committed six minutes in (the CLI stamps commit.json with the wall clock)."""
    commit = world.run_dir / "revisions" / "r0001" / "commit.json"
    doc = json.loads(commit.read_text(encoding="utf-8"))
    doc["created_at"] = T0 + 6 * MIN
    commit.write_text(json.dumps(doc), encoding="utf-8")
    world.decide((3, "task_verdict", "success"))
    sub = world.start_subtask(at=T0 + 10 * MIN)
    world.apply(sub)
    t = T0 / 1000.0
    rows = main_latency() + [("probe", 1.5, True, t + 660, "s1", 0, ""),
                             ("caption", 2.5, True, t + 700, "s2", 0, "")]
    (world.run_dir / "details" / "vlm_latency.csv").write_text(latency_csv(rows), encoding="utf-8")
    world.repo.set_subtask_progress(sub.id, {"stages": [
        {"id": "vlm", "state": "succeeded", "done": 1, "total": 1, "elapsed_s": 20.0}]})
    world.repo.add_usage([P.UsageDelta(task_id=world.task_id, ledger="actual",
                                       module_id="example_a+example_b", call_kind="merged",
                                       model_name="m", subtask_id=sub.id, requests=2,
                                       prompt_tokens=10)], at=T0 + 11 * MIN)
    world.revision(2, subtask_id=sub.id)
    world.finish_subtask(sub, at=T0 + 20 * MIN)
    world.switch(2)
    return sub


def test_main_run_and_subtask_split_by_the_run_windows(world, with_subtask):
    sub = with_subtask
    everything = _perf(world)
    assert everything["revision"] == 2
    kinds = {x["call_kind"]: x for x in everything["latency"]}
    assert [x["call_kind"] for x in everything["latency"]] == [
        "probe", "endstate", "arbitration", "caption"]
    assert kinds["probe"]["count"] == 5 and kinds["caption"]["count"] == 1
    assert everything["stages"] == [{"id": "numeric", "wall_s": 10.0, "share": 0.0833},
                                    {"id": "frame", "wall_s": 30.0, "share": 0.25},
                                    {"id": "vlm", "wall_s": 80.0, "share": 0.6667}]
    assert everything["merge"] == {"requests": 2, "estimated_unmerged": 4}

    main = _perf(world, scope="main")
    assert main["latency"] == [MAIN_PROBE, MAIN_ENDSTATE, MAIN_ARBITRATION]
    assert main["merge"] == {"requests": 0, "estimated_unmerged": 0}
    assert main["stages"][2] == {"id": "vlm", "wall_s": 60.0, "share": 0.6}
    assert "redone_after_interruption" not in main

    one = _perf(world, scope="subtask", subtask=sub.id)
    assert one["subtask_id"] == sub.id
    assert one["latency"] == [
        {"call_kind": "probe", "count": 1, "failed": 0, "hedged": 0, "p50_s": 1.5, "p90_s": 1.5,
         "p99_s": 1.5, "wall_s": 1.5},
        {"call_kind": "caption", "count": 1, "failed": 0, "hedged": 0, "p50_s": 2.5, "p90_s": 2.5,
         "p99_s": 2.5, "wall_s": 2.5}]
    assert one["effective_concurrency"] == 1.0
    assert one["stages"] == [{"id": "vlm", "wall_s": 20.0, "share": 1.0}]
    assert one["merge"] == {"requests": 2, "estimated_unmerged": 4}


def test_an_older_revision_does_not_see_later_subtasks(world, with_subtask):
    sub = with_subtask
    old = _perf(world, rev=1)
    assert old["revision"] == 1
    assert old["latency"] == [MAIN_PROBE, MAIN_ENDSTATE, MAIN_ARBITRATION]
    assert old["merge"] == {"requests": 0, "estimated_unmerged": 0}
    assert old["stages"][2] == {"id": "vlm", "wall_s": 60.0, "share": 0.6}
    later = _perf(world, rev=1, scope="subtask", subtask=sub.id)
    assert later["latency"] == [] and later["stages"] == []
    assert later["merge"] == {"requests": 0, "estimated_unmerged": 0}
    assert _perf(world, rev=1, scope="main")["latency"] == old["latency"]


def test_bad_scopes_and_foreign_subtasks(world, with_subtask, tmp_path):
    assert_error(world.get("/perf", scope="subtask"), "validation_failed")
    assert_error(world.get("/perf", subtask=with_subtask.id), "validation_failed")
    assert_error(world.get("/perf", scope="everything"), "validation_failed")
    body = assert_error(world.get("/perf", scope="subtask", subtask="sub_nope"), "not_found")
    assert body["error"]["details"] == {"subtask_id": "sub_nope"}
    # a subtask of another task is not this task's
    other = make_task(world.repo, world.dataset, name="another")
    assert world.repo.update_task_state(other.id, {"queued"}, "running", at=T0)
    assert world.repo.update_task_state(other.id, {"running"}, "succeeded", at=T0 + MIN)
    foreign = world.repo.create_subtask(P.Subtask(id="", task_id=other.id, kind="reexport",
                                                  scope={}, state="queued"))
    assert_error(world.get("/perf", scope="subtask", subtask=foreign.id), "not_found")


def test_a_run_without_latency_file_or_vlm(world):
    (world.run_dir / "details" / "vlm_latency.csv").unlink()
    body = _perf(world, scope="main")
    assert body["latency"] == [] and body["effective_concurrency"] is None
    world.repo.freeze_task_inputs(world.task_id, run_id="r", preflight={}, source_fingerprint={},
                                  vlm_snapshot=None)
    assert "backend" not in _perf(world)
