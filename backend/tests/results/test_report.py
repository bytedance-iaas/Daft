"""GET /tasks/{id}/report: revision resolution (``?rev=N``, commit.json only) and links (D22)."""
from __future__ import annotations

import shutil

from daemon.results import store_of

from .conftest import API, JSON, T0, assert_error, assert_schema, make_dataset, make_task

REPORT = "openapi.yaml#/paths/~1tasks~1{id}~1report/get/responses/200/content/application~1json/schema"


def test_current_report_is_the_committed_report_json_plus_links(world):
    r = world.get("/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema(REPORT, body)
    assert_schema("cli/report.schema.json", body["report"])
    assert body["revision"] == 1 and body["report"]["revision"] == 1
    counts = body["report"]["overview"]["counts"]
    assert counts == {"total": 9, "passed": 5, "rejected": 3, "held": 1, "review": 5}
    assert [m["id"] for m in body["report"]["modules"]] == [
        "timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
        "video_action_sync", "task_success", "dedup", "skill_profile"]
    links = {(ln["rel"], ln["url"]) for ln in body["links"]}
    tid = world.task_id
    assert ("task", f"/curation/tasks/{tid}") in links
    assert ("report", f"/curation/tasks/{tid}/report") in links
    # one adjudication link per source module that still has pending cards
    assert ("adjudication", f"/curation/tasks/{tid}/adjudication?source=task_success") in links
    assert ("adjudication", f"/curation/tasks/{tid}/adjudication?source=skill_profile") in links
    assert all(ln.get("absolute") is False for ln in body["links"])


def test_links_are_absolute_with_a_public_base_url(client_for, tmp_path):
    from .conftest import build_run_dir, finish_main_run, World

    c = client_for(base_path="/curation", public_base_url="https://curator.example.com")
    rt = c.app.state.runtime
    ds = make_dataset(tmp_path / "ds")
    task = make_task(rt.repo, ds)
    finish_main_run(rt.repo, task.id)
    run_dir = rt.settings.work_dir / task.id
    build_run_dir(run_dir, ds)
    w = World(client=c, rt=rt, task_id=task.id, run_dir=run_dir, dataset=ds, clock=None)
    w.revision(1)
    w.switch(1)
    body = w.get("/report").json()
    assert_schema(REPORT, body)
    assert all(ln["url"].startswith("https://curator.example.com/curation/tasks/")
               and "absolute" not in ln for ln in body["links"])


def test_old_revisions_stay_readable_and_newer_ones_are_404(world):
    world.decide((3, "task_verdict", "success"))
    sub = world.start_subtask(at=T0 + 10 * 60_000)
    world.apply(sub)
    world.revision(2, subtask_id=sub.id)
    world.finish_subtask(sub, at=T0 + 20 * 60_000)
    world.switch(2)

    cur = world.get("/report").json()
    assert cur["revision"] == 2 and cur["report"]["revision"] == 2
    old = world.get("/report", rev=1)
    assert old.status_code == 200
    body = old.json()
    assert_schema(REPORT, body)
    assert body["revision"] == 1 and body["report"]["overview"]["counts"]["review"] == 5
    report_links = [ln for ln in body["links"] if ln["rel"] == "report"]
    assert report_links[0]["url"].endswith("/report?rev=1")
    assert not [ln for ln in body["links"] if ln["rel"] == "adjudication"]   # not adjudicable

    r = world.get("/report", rev=3)
    assert assert_error(r, "not_found")["error"]["details"] == {"revision": 3, "result_rev": 2}
    assert_error(world.get("/report", rev=0), "validation_failed")
    assert_error(world.get("/report", rev="x"), "validation_failed")


def test_a_committed_revision_not_switched_to_is_not_served(world):
    """r2 is committed in the run directory but result_rev is still 1 (upload/verify pending)."""
    world.decide((3, "task_verdict", "success"))
    sub = world.start_subtask(at=T0 + 10 * 60_000)
    world.apply(sub)
    world.revision(2, subtask_id=sub.id)
    assert world.get("/report").json()["revision"] == 1
    assert_error(world.get("/report", rev=2), "not_found")


def test_no_result_yet_is_404(client_for, tmp_path):
    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    task = make_task(rt.repo, make_dataset(tmp_path / "ds"))
    for rel in ("/report", "/perf", "/episodes/0", "/report/tables/motion_quality"):
        body = assert_error(c.get(f"{API}/tasks/{task.id}{rel}"), "not_found")
        assert body["error"]["details"] == {"result_rev": 0}
    r = c.get(f"{API}/tasks/{task.id}/adjudication")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None, "has_more": False,
                        "counts": {"decided": 0, "pending": 0, "unapplied": 0}}


def test_missing_run_directory_and_uncommitted_revision_are_reported(world):
    # the revision directory lost its commit.json: not trusted
    (world.run_dir / "revisions" / "r0001" / "commit.json").unlink()
    body = assert_error(world.get("/report"), "not_found")
    assert body["error"]["details"] == {"revision": 1, "reason": "revision_missing"}
    # the whole run directory is gone (cleaned locally; W5a owns backfill)
    shutil.rmtree(world.run_dir)
    for rel in ("/report", "/perf", "/episodes/0", "/report/tables/motion_quality", "/adjudication"):
        body = assert_error(world.get(rel), "not_found")
        assert body["error"]["details"] == {"revision": 1, "reason": "run_dir_missing"}, rel
        assert "交付目录" in body["error"]["message"]
    r = world.decide((3, "task_verdict", "success"))
    assert assert_error(r, "not_found")["error"]["details"]["reason"] == "run_dir_missing"


def test_backfill_hook_brings_the_run_directory_back(world, tmp_path):
    saved = tmp_path / "delivery-copy"
    shutil.copytree(world.run_dir, saved)
    shutil.rmtree(world.run_dir)
    calls = []

    def backfill(task, missing):
        calls.append((task.id, missing))
        shutil.copytree(saved, world.run_dir)
        return True

    store_of(world.rt).backfill = backfill
    r = world.get("/report")
    assert r.status_code == 200, r.text
    assert calls == [(world.task_id, "revisions/r0001/commit.json")]


def test_other_methods_answer_405_with_allow(world):
    for method, rel, allow in (("PUT", "/report", "GET"), ("DELETE", "/adjudication", "GET, POST"),
                               ("POST", "/episodes/3", "GET"), ("PATCH", "/perf", "GET")):
        r = world.client.request(method, f"{API}/tasks/{world.task_id}{rel}", headers=JSON)
        body = assert_error(r, "method_not_allowed")
        assert set(r.headers["allow"].split(", ")) >= set(allow.split(", ")), (rel, r.headers)
        assert body["error"]["details"]["allow"]


def test_foreign_and_unknown_tasks_are_404(world, tmp_path):
    other = make_task(world.repo, world.dataset, owner="someone-else")
    for tid in (other.id, "task_nope", "..", "task_x%2F.."):
        for rel in ("/report", "/report/tables/motion_quality", "/episodes/0", "/perf",
                    "/adjudication"):
            r = world.client.get(f"{API}/tasks/{tid}{rel}")
            assert r.status_code == 404, (tid, rel, r.text)
            assert_schema("Error", r.json())
        r = world.client.post(f"{API}/tasks/{tid}/adjudication", headers=JSON, json={
            "decisions": [{"episode_index": 3, "line": "task_verdict", "decision": "success"}]})
        assert r.status_code == 404, (tid, r.text)
