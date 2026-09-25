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


def _in_vlm_stage(d, task_id: str, *, results: int = 0):
    """The VLM stage is running with at least ``results`` records, and the stages before it
    are done. The funnel is pipelined: the VLM stage can write its first result while the
    numeric and frame stages still work on later episodes, and these tests are about
    interrupting the VLM stage alone (a slow CI runner paused or stopped the frame stage too,
    2026-09-24)."""
    def ready():
        task = d.get(task_id)
        stages = {s["id"]: s for s in task["progress"]["stages"]}
        if stages.get("vlm", {}).get("state") != "running":
            return False
        if any(stages.get(sid, {}).get("state") != "succeeded" for sid in ("numeric", "frame")):
            return False
        return len(_part_lines(d.run_dir(task_id), "task_success")) >= results
    return ready


def _redone(run_dir: str, module: str, before: dict[int, dict]) -> set[int]:
    """Episodes of ``before`` (records written before the interruption) judged again in the
    second part. The pipelined stage hands an episode it finished on again with its record as
    it was (carried over, not judged): only a different record is finished work repeated."""
    after = {r["episode_index"]: r for r in _part_lines(run_dir, module, "0002")}
    return {e for e in after.keys() & before.keys() if after[e] != before[e]}


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


#: model parallelism 2: the VLM stage judges one episode at a time (episode gate N // 2)
ONE_AT_A_TIME = {"start_now": True, "export": True, "vlm_hedge": False,
                 "limits": {"vlm_parallelism": 2}}


def test_pause_then_resume_gives_the_results_of_an_uninterrupted_run(daemon, fake_vlm):
    d = daemon()
    reference = d.wait(d.create(params=ONE_AT_A_TIME)["id"])
    assert reference["state"] == "succeeded"

    # Half way by construction, not by timing: the model gives no judgement until the pause
    # is asked for, so the one episode in flight is still being judged and the others
    # wait their turn (the video judgement makes four calls an episode; a delay alone
    # let a slow CI runner finish the stage before the pause landed, 2026-09-24).
    judge = "Assess the robot manipulation task"
    asked, hold = fake_vlm.count(judge), fake_vlm.hold(judge)
    try:
        task_id = d.create(params=ONE_AT_A_TIME)["id"]
        in_vlm_stage = _in_vlm_stage(d, task_id)
        d.wait_for(lambda: fake_vlm.count(judge) > asked and in_vlm_stage(),
                   what="the VLM stage to ask about its first episode")
        r = d.action(task_id, "pause")
        assert r.status_code == 200, r.text
        assert r.json()["state"] in ("pausing", "paused")
    finally:
        hold.set()                                  # the episode in flight finishes
    paused = d.wait(task_id, ("paused",), timeout=120)
    assert paused["pause_reason"] == "user"
    assert d.orch.executor.live() == []             # the command finished its episodes and left
    done_before = {r["episode_index"]: r for r in _part_lines(d.run_dir(task_id), "task_success")}
    assert done_before and all(r["verdict"] != "error" for r in done_before.values())
    # the episodes that were waiting their turn are left for the resume
    assert len(done_before) < len(results(d.run_dir(reference["id"]), "task_success"))
    stages = {s["id"]: s["state"] for s in paused["progress"]["stages"]}
    assert stages["numeric"] == "succeeded" and stages["vlm"] != "succeeded"
    assert {m["id"]: m["state"] for m in paused["modules"]}["task_success"] != "running"
    batch = d.delivery(paused["run_id"])                   # finished stages are delivered
    assert os.path.isfile(os.path.join(batch, "checks", "timestamp_check", "results.jsonl"))
    assert not os.path.exists(os.path.join(batch, "_COMPLETE"))

    r = d.action(task_id, "resume")
    assert r.status_code == 200 and r.json()["state"] in ("queued", "running"), r.text
    task = d.wait(task_id)
    assert task["state"] == "succeeded" and task["summary"] == reference["summary"]
    # the resumed stage skipped what was done: nothing written before the pause is judged again
    assert not _redone(d.run_dir(task_id), "task_success", done_before)
    assert _snapshot(d.run_dir(task_id)) == _snapshot(d.run_dir(reference["id"]))
    kinds = [e["kind"] for e in d.api("GET", f"/tasks/{task_id}/timeline").json()["items"]]
    assert "user_pause" in kinds and "user_resume" in kinds


def test_stop_leaves_no_child_and_continue_repeats_no_finished_work(daemon, fake_vlm):
    d = daemon()
    fake_vlm.delay_s = 0.15
    task_id = d.create()["id"]
    d.wait_for(_in_vlm_stage(d, task_id, results=1), what="the VLM stage to write a result")
    r = d.action(task_id, "stop")
    assert r.status_code == 200 and r.json()["state"] in ("stopping", "stopped"), r.text
    stopped = d.wait(task_id, ("stopped",), timeout=60)
    assert stopped["state_reason"] == "用户停止"
    assert d.orch.executor.live() == []
    assert _children_of_run(d.run_dir(task_id)) == []
    kept = {r["episode_index"]: r for r in _part_lines(d.run_dir(task_id), "task_success")}
    assert kept
    parts_before = {module: set(os.listdir(os.path.join(d.run_dir(task_id), "checks",
                                                          module, "parts")))
                    for module in ("timestamp_check", "visual_quality")}

    fake_vlm.delay_s = 0.0
    r = d.api("POST", f"/tasks/{task_id}/continue")
    assert r.status_code == 202, r.text
    assert r.json()["subtask"]["kind"] == "resume"
    task = d.wait(task_id)
    assert task["state"] == "succeeded", task
    assert task["summary"]["total"] == 8 and task["summary"]["passed"] == 5
    run_dir = d.run_dir(task_id)
    for module in ("timestamp_check", "visual_quality"):     # finished stages were not re-run
        assert set(os.listdir(os.path.join(run_dir, "checks", module, "parts"))) == \
            parts_before[module]
    assert not _redone(run_dir, "task_success", kept)
    subs = d.api("GET", f"/tasks/{task_id}/subtasks").json()["items"]
    assert [(s["kind"], s["state"]) for s in subs] == [("resume", "succeeded")]
    kinds = [e["kind"] for e in d.api("GET", f"/tasks/{task_id}/timeline").json()["items"]]
    assert "stopped" in kinds and "subtask_finished" in kinds and kinds[-1] in (
        "finished", "subtask_finished")
