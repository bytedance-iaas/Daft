"""The main run end to end: the real CLI, stage by stage, into a delivered batch (slow)."""
from __future__ import annotations

import json
import os

import pytest

from .conftest import assert_schema, read_jsonl, results

pytestmark = pytest.mark.slow


def test_a_task_runs_every_stage_and_publishes_a_complete_batch(daemon):
    d = daemon()
    created = d.create()
    assert created["state"] == "queued"
    task = d.wait(created["id"])
    assert task["state"] == "succeeded", json.dumps(task, ensure_ascii=False)[:3000]
    # the fixture: 2 captions, 2 killed by the numeric gates, 7 a copy of 3 -> 5 / 3 / 0 / 2
    assert task["summary"] == {"total": 8, "passed": 5, "rejected": 3, "held": 0, "review": 3,
                               "pass_rate": 0.625}
    assert task["result_rev"] == 1 and task["delivery_stale"] is False
    assert [s["id"] for s in task["progress"]["stages"]] == [
        "autolabel", "numeric", "frame", "vlm", "verdict", "dedup", "profile_vlm", "final", "report",
        "export", "verify"]
    assert all(s["state"] in ("succeeded", "completed_with_errors")
               for s in task["progress"]["stages"]), task["progress"]
    mods = {m["id"]: m for m in task["modules"]}
    assert mods["timestamp_check"]["state"] == "succeeded"
    assert mods["timestamp_check"]["episodes_total"] == 8
    assert mods["task_success"]["episodes_total"] == 6 and mods["task_success"]["episodes_error"] == 0
    assert task["usage"]["requests"] > 0 and task["usage"]["prompt_tokens"] > 0
    run_id = task["run_id"]
    batch = d.delivery(run_id)
    assert os.path.isfile(os.path.join(batch, "_COMPLETE"))
    assert os.path.isfile(os.path.join(batch, "revisions", "r0001", "commit.json"))
    assert os.path.isfile(os.path.join(batch, "export", "manifest.json"))
    with open(os.path.join(d.delivery(), "latest"), encoding="utf-8") as fh:
        assert fh.read().strip() == run_id
    plan = d.api("GET", f"/tasks/{task['id']}/plan")
    assert plan.status_code == 200
    assert_schema("./cli/plan.schema.json", plan.json())
    timeline = d.api("GET", f"/tasks/{task['id']}/timeline").json()["items"]
    kinds = [e["kind"] for e in timeline]
    assert kinds[:2] == ["created", "started"] and "revision" in kinds and kinds[-1] == "finished"
    logs = d.api("GET", f"/tasks/{task['id']}/logs", params={"limit": 200}).json()
    assert logs["items"] and {line["stage"] for line in logs["items"]} >= {"numeric", "vlm"}
    # the dataset got registered on the way and the start recorded its check
    ds = d.api("GET", f"/datasets/{task['dataset_id']}").json()
    assert_schema("DatasetDetail", ds)
    assert [c["trigger"] for c in ds["checks"]][-1] == "add"
    rd = d.run_dir(task["id"])
    assert set(results(rd, "task_success")) == {0, 1, 3, 4, 6, 7}
    assert read_jsonl(os.path.join(rd, "usage.jsonl"))


def test_episodes_the_listing_skips_are_left_out_of_the_plan_and_the_totals(daemon, monkeypatch):
    """D40: an episode whose source files are missing is never read, in no list, not in total."""
    from daemon.orchestr import datasets

    original = datasets.DatasetOps.listing

    def listing(self, src, owner, out):
        doc = original(self, src, owner, out)
        doc["skipped_episodes"] = [{"episode_index": 5, "missing": [
            "videos/chunk-000/observation.images.wrist/episode_000005.mp4"]}]
        return doc

    monkeypatch.setattr(datasets.DatasetOps, "listing", listing)
    d = daemon()
    task = d.wait(d.create(modules=["timestamp_check"])["id"])
    assert task["state"] == "succeeded", json.dumps(task, ensure_ascii=False)[:2000]
    assert task["summary"]["total"] == 7
    numeric = {s["id"]: s for s in task["progress"]["stages"]}["numeric"]
    assert (numeric["done"], numeric["total"]) == (7, 7)
    assert 5 not in results(d.run_dir(task["id"]), "timestamp_check")
    assert {m["id"]: m for m in task["modules"]}["timestamp_check"]["episodes_total"] == 7
