"""Pause, resume, stop and continue on real CLI processes (slow).

* pausing sends SIGTERM: the episodes in flight finish and are kept, the task is
  ``paused``; resuming re-runs the stage with ``--resume`` - the results equal an
  uninterrupted run's;
* stopping sends SIGINT: nothing of the command is left running; ``continue``
  finishes the main run without redoing any finished stage or episode.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from .conftest import comparable, read_jsonl, results

pytestmark = pytest.mark.slow

MODULES = ["timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
           "video_action_sync", "task_success", "dedup", "skill_profile"]


def _part_lines(run_dir: str, module: str, part: str = "0001") -> list[dict]:
    path = os.path.join(run_dir, "checks", module, "parts", f"{part}.jsonl")
    return read_jsonl(path) if os.path.isfile(path) else []


def _in_vlm_stage_with_a_result(d, task_id: str):
    def ready():
        task = d.get(task_id)
        stages = {s["id"]: s for s in task["progress"]["stages"]}
        if stages.get("vlm", {}).get("state") != "running":
            return False
        return len(_part_lines(d.run_dir(task_id), "task_success")) >= 1
    return ready


def _children_of_run(run_dir: str) -> list[str]:
    out = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True,
                         text=True).stdout
    return [line for line in out.splitlines() if "curation.cli" in line and run_dir in line]


def _snapshot(run_dir: str) -> dict:
    out = {m: {e: comparable(r) for e, r in results(run_dir, m).items()} for m in MODULES}
    rev = os.path.join(run_dir, "revisions", "r0001")
    for name in ("passed", "reject", "held", "review"):
        with open(os.path.join(rev, f"{name}.json"), encoding="utf-8") as fh:
            out[name] = json.load(fh)["episodes"]
    return out


def test_pause_then_resume_gives_the_results_of_an_uninterrupted_run(daemon, fake_vlm):
    d = daemon()
    reference = d.wait(d.create()["id"])
    assert reference["state"] == "succeeded"

    fake_vlm.delay_s = 0.15                         # slow enough to pause half way
    task_id = d.create()["id"]
    d.wait_for(_in_vlm_stage_with_a_result(d, task_id), what="the VLM stage to write a result")
    r = d.action(task_id, "pause")
    assert r.status_code == 200, r.text
    assert r.json()["state"] in ("pausing", "paused")
    paused = d.wait(task_id, ("paused",), timeout=120)
    assert paused["pause_reason"] == "user"
    assert d.orch.executor.live() == []             # the command finished its episodes and left
    done_before = {r["episode_index"] for r in _part_lines(d.run_dir(task_id), "task_success")}
    assert done_before and all(r["verdict"] != "error"
                               for r in _part_lines(d.run_dir(task_id), "task_success"))
    stages = {s["id"]: s["state"] for s in paused["progress"]["stages"]}
    assert stages["numeric"] == "succeeded" and stages["vlm"] != "succeeded"
    assert {m["id"]: m["state"] for m in paused["modules"]}["task_success"] != "running"
    batch = d.delivery(paused["run_id"])                   # finished stages are delivered
    assert os.path.isfile(os.path.join(batch, "checks", "timestamp_check", "results.jsonl"))
    assert not os.path.exists(os.path.join(batch, "_COMPLETE"))

    fake_vlm.delay_s = 0.0
    r = d.action(task_id, "resume")
    assert r.status_code == 200 and r.json()["state"] in ("queued", "running"), r.text
    task = d.wait(task_id)
    assert task["state"] == "succeeded" and task["summary"] == reference["summary"]
    # the resumed stage skipped what was done: those episodes are not in its second part
    again = {r["episode_index"] for r in _part_lines(d.run_dir(task_id), "task_success", "0002")}
    assert not (again & done_before)
    assert _snapshot(d.run_dir(task_id)) == _snapshot(d.run_dir(reference["id"]))
    kinds = [e["kind"] for e in d.api("GET", f"/tasks/{task_id}/timeline").json()["items"]]
    assert "user_pause" in kinds and "user_resume" in kinds


def test_stop_leaves_no_child_and_continue_repeats_no_finished_work(daemon, fake_vlm):
    d = daemon()
    fake_vlm.delay_s = 0.15
    task_id = d.create()["id"]
    d.wait_for(_in_vlm_stage_with_a_result(d, task_id), what="the VLM stage to write a result")
    r = d.action(task_id, "stop")
    assert r.status_code == 200 and r.json()["state"] in ("stopping", "stopped"), r.text
    stopped = d.wait(task_id, ("stopped",), timeout=60)
    assert stopped["state_reason"] == "用户停止"
    assert d.orch.executor.live() == []
    assert _children_of_run(d.run_dir(task_id)) == []
    kept = {r["episode_index"] for r in _part_lines(d.run_dir(task_id), "task_success")}
    assert kept

    fake_vlm.delay_s = 0.0
    r = d.api("POST", f"/tasks/{task_id}/continue")
    assert r.status_code == 202, r.text
    assert r.json()["subtask"]["kind"] == "resume"
    task = d.wait(task_id)
    assert task["state"] == "succeeded", task
    assert task["summary"]["total"] == 8 and task["summary"]["passed"] == 5
    run_dir = d.run_dir(task_id)
    for module in ("timestamp_check", "visual_quality"):     # finished stages were not re-run
        assert os.listdir(os.path.join(run_dir, "checks", module, "parts")) == ["0001.jsonl"]
    again = {r["episode_index"] for r in _part_lines(run_dir, "task_success", "0002")}
    assert not (again & kept)
    subs = d.api("GET", f"/tasks/{task_id}/subtasks").json()["items"]
    assert [(s["kind"], s["state"]) for s in subs] == [("resume", "succeeded")]
    kinds = [e["kind"] for e in d.api("GET", f"/tasks/{task_id}/timeline").json()["items"]]
    assert "stopped" in kinds and "subtask_finished" in kinds and kinds[-1] in (
        "finished", "subtask_finished")
