"""Adjudication, the D37 fingerprint flow, the queue and one publisher per delivery (slow)."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from .conftest import assert_schema, read_jsonl

pytestmark = pytest.mark.slow


def test_applying_decisions_builds_a_new_revision_without_exporting(daemon):
    from daemon.repo import protocol as P

    from curation.contracts import modules as registry

    d = daemon()
    first = d.wait(d.create()["id"])
    task_id, rd = first["id"], d.run_dir(first["id"])
    with open(os.path.join(rd, "revisions", "r0001", "review.json"), encoding="utf-8") as fh:
        review = json.load(fh)["episodes"]
    must_decide = [e["episode_index"] for e in review if any(
        registry.review_line_of_kind(i["kind"]).counts_as_pending for i in e["review"])]
    assert 3 in must_decide                                  # task_success abstained on it
    assert first["state"] == "succeeded"
    assert first["pending_adjudication"] == len(must_decide)  # appeals are not pending (D42)
    r = d.api("POST", f"/tasks/{task_id}/adjudication/apply")
    assert r.status_code == 409 and "没有待执行的裁决" in r.json()["error"]["message"]
    r = d.api("POST", f"/tasks/{task_id}/adjudication", json={"decisions": [   # W5b records
        {"episode_index": 3, "line": "task_verdict", "decision": "failure"}]})
    assert r.status_code == 200, r.text
    # the fixture raises no label conflict to answer on the page; a relabel recorded all the
    # same keeps the re-judging path covered (the CLI executes it as v1 does)
    d.rt.repo.append_adjudication([
        P.AdjudicationCreate(task_id=task_id, episode_index=4, line="label",
                             decision="custom_label", new_label="wipe the table",
                             decided_by="tester")], at=d.rt.clock())
    r = d.api("POST", f"/tasks/{task_id}/adjudication/apply", json={"relabel_rerun": "nope"})
    assert r.status_code == 400
    r = d.api("POST", f"/tasks/{task_id}/adjudication/apply", json={})
    assert r.status_code == 202, r.text
    assert_schema("SubtaskCreated", r.json())
    assert r.json()["subtask"]["scope"] == {"relabel_rerun": "v1"}
    done = d.wait(task_id)
    assert done["state"] == "succeeded" and done["result_rev"] == 2, json.dumps(done)[:2000]
    assert (done["summary"]["passed"], done["summary"]["rejected"]) == (4, 4)
    assert done["delivery_stale"] is True                  # D9: no export, it is stale now
    assert d.rt.repo.latest_adjudications(task_id, unapplied_only=True) == []
    queue = d.api("GET", f"/tasks/{task_id}/adjudication", params={"status": "all"}).json()
    assert {c["episode_index"]: c["status"] for c in queue["items"]}.get(3) == "applied"
    assert done["pending_adjudication"] == queue["counts"]["pending"]    # one way of counting
    for where in (rd, d.delivery(done["run_id"])):          # every decision, and delivered
        with open(os.path.join(where, "human-decisions", "task_verdicts.csv"),
                  encoding="utf-8") as fh:
            assert "ep000003" in fh.read()
        with open(os.path.join(where, "human-decisions", "label_decisions.csv"),
                  encoding="utf-8") as fh:
            assert "wipe the table" in fh.read()
    sub_id = d.api("GET", f"/tasks/{task_id}/subtasks").json()["items"][0]["id"]
    with open(os.path.join(rd, ".orchestr", sub_id, "decisions.json"), encoding="utf-8") as fh:
        decisions = json.load(fh)
    assert_schema("./cli/decisions.schema.json", decisions)
    assert decisions["relabel_rerun"] == "v1" and len(decisions["decisions"]) == 2
    part2 = read_jsonl(os.path.join(rd, "checks", "task_success", "parts", "0002.jsonl"))
    assert [r["episode_index"] for r in part2] == [4]       # the relabelled one, re-judged
    assert os.listdir(os.path.join(rd, "checks", "dedup", "parts")) == ["0001.jsonl"]
    export_before = os.path.getmtime(os.path.join(rd, "export", "manifest.json"))
    r = d.api("POST", f"/tasks/{task_id}/reexport")
    assert r.status_code == 202
    exported = d.wait(task_id)
    assert exported["delivery_stale"] is False
    assert os.path.getmtime(os.path.join(rd, "export", "manifest.json")) > export_before


def test_a_dataset_changed_since_registration_stops_the_start_until_repreflight(daemon):
    d = daemon()
    created = d.create(start_now=False)
    assert created["state"] == "created"
    task = d.get(created["id"])
    assert task["dataset_id"], "a full address is registered when the task is created"
    info = os.path.join(d.dataset, "meta", "episodes.jsonl")
    st = os.stat(info)
    os.utime(info, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    r = d.action(created["id"], "start")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "source_changed"
    change = body["error"]["details"]
    assert_schema("SourceChange", change)
    assert change["meta_changed"] is True and change["modified"] == 1
    assert change["sample_keys"] == ["meta/episodes.jsonl"]
    ds = d.api("GET", f"/datasets/{task['dataset_id']}").json()
    assert ds["check_state"] == "changed" and ds["checks"][0]["trigger"] == "task_start"
    assert d.get(created["id"])["state"] == "created"
    r = d.api("POST", f"/tasks/{created['id']}/repreflight")
    assert r.status_code == 200, r.text
    assert_schema("RepreflightResult", r.json())
    assert r.json()["compatible"] is True and r.json()["task"]["state"] in ("queued", "running")
    assert d.api("GET", f"/datasets/{task['dataset_id']}").json()["check_state"] == "ok"
    d.action(created["id"], "stop")
    assert d.wait(created["id"], ("stopped",))["state"] == "stopped"


def test_repreflight_says_what_no_longer_fits(daemon):
    d = daemon()
    created = d.create(start_now=False, episodes={"mode": "explicit", "expr": "6-7"})
    meta = os.path.join(d.dataset, "meta")
    lines = open(os.path.join(meta, "episodes.jsonl"), encoding="utf-8").read().splitlines()
    with open(os.path.join(meta, "episodes.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines[:-1]) + "\n")                 # episode 7 is gone
    info = json.load(open(os.path.join(meta, "info.json"), encoding="utf-8"))
    info["total_episodes"] = 7
    json.dump(info, open(os.path.join(meta, "info.json"), "w", encoding="utf-8"))
    assert d.action(created["id"], "start").status_code == 409
    r = d.api("POST", f"/tasks/{created['id']}/repreflight")
    assert r.status_code == 200, r.text
    out = r.json()
    assert_schema("RepreflightResult", out)
    assert out["compatible"] is False and out["task"]["state"] == "created"
    assert [i["field"] for i in out["incompatibilities"]] == ["episodes"]
    assert out["incompatibilities"][0]["reason_code"] == "episodes_out_of_range"


def test_publishing_into_one_delivery_is_serial(daemon, monkeypatch):
    from daemon.orchestr import runbase

    calls, lock = [], threading.Lock()

    def timed(name):
        original = getattr(runbase.Run, name)

        def wrapper(self, *args, **kw):
            t0 = time.monotonic()
            try:
                return original(self, *args, **kw)
            finally:
                with lock:
                    calls.append((self.task_id, t0, time.monotonic()))
        monkeypatch.setattr(runbase.Run, name, wrapper)

    for name in ("export", "sync_and_verify"):       # both run under the delivery's lock
        timed(name)
    monkeypatch.setenv("CURATOR_MAX_RUNNING_TASKS", "2")
    d = daemon()
    ids = [d.create(modules=["timestamp_check"])["id"] for _ in range(3)]
    done = [d.wait(i) for i in ids]
    assert all(t["state"] == "succeeded" for t in done)
    assert len({t["run_id"] for t in done}) == 3                 # three batches, none shared
    windows = {}
    for task_id, start, end in calls:
        first, last = windows.get(task_id, (start, end))
        windows[task_id] = (min(first, start), max(last, end))
    spans = sorted(((t, a, b) for t, (a, b) in windows.items()), key=lambda w: w[1])
    assert len(spans) == 3
    for (_, _, end), (_, start, _) in zip(spans, spans[1:]):
        assert start >= end - 1e-3, f"two tasks published into one delivery at once: {spans}"
    for t in done:
        assert os.path.isfile(os.path.join(d.delivery(t["run_id"]), "_COMPLETE"))
    run_of = {t["id"]: t["run_id"] for t in done}
    last = max(spans, key=lambda w: w[2])[0]
    with open(os.path.join(d.delivery(), "latest"), encoding="utf-8") as fh:
        assert fh.read().strip() == run_of[last]              # the last complete publish


def test_one_task_runs_at_a_time_by_default(daemon):
    d = daemon()
    ids = [d.create(modules=["timestamp_check"])["id"] for _ in range(2)]
    second = d.get(ids[1])
    assert second["state"] == "queued"                          # waits for the only slot
    done = [d.wait(i) for i in ids]
    assert all(t["state"] == "succeeded" for t in done)
    first, second = sorted(done, key=lambda t: t["started_at"])
    assert second["started_at"] >= first["finished_at"]
