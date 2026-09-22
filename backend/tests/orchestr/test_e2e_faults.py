"""Faults in the middle of a run, through a CLI with injected faults (slow).

* an episode that crashes the process twice is named, recorded as an error and
  skipped; the task finishes (P14);
* an episode a module erred on is held: it goes to no later stage and is not
  delivered; a retry re-runs it alone and the terminal state is recomputed (D24, D25);
* a module that fails as a whole is ``failed``, the other modules go on; a retry runs
  it in full (04 §7).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from .conftest import read_jsonl, results

pytestmark = pytest.mark.slow

FAULTY = os.path.join(os.path.dirname(__file__), "faultycli.py")
NUMERIC = ["timestamp_check", "kinematic_limits", "motion_quality"]


@pytest.fixture
def faulty(monkeypatch, tmp_path):
    switch = tmp_path / "fault-on"
    switch.write_text("1")
    monkeypatch.setenv("CURATOR_CLI", f"{sys.executable} {FAULTY}")
    monkeypatch.setenv("FAKE_SWITCH", str(switch))

    def set_fault(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)

    set_fault.switch = switch
    return set_fault


def _passed(run_dir: str, rev: int) -> set[int]:
    with open(os.path.join(run_dir, "revisions", f"r{rev:04d}", "passed.json"),
              encoding="utf-8") as fh:
        return {e["episode_index"] for e in json.load(fh)["episodes"]}


def test_an_episode_that_crashes_the_process_twice_is_named_and_skipped(daemon, faulty):
    faulty("FAKE_CRASH", "timestamp_check:3")
    d = daemon()
    body = d.task_body(modules=NUMERIC)
    body["params"]["limits"] = {"cpu_concurrency": 1}      # one episode in flight at a time
    body.pop("vlm")
    r = d.api("POST", "/tasks", json=body)
    assert r.status_code == 201, r.text
    task = d.wait(r.json()["id"], timeout=180)
    assert task["state"] == "completed_with_errors", json.dumps(task)[:2000]
    assert task["summary"]["held"] == 1 and task["summary"]["total"] == 8
    rd = d.run_dir(task["id"])
    rec = results(rd, "timestamp_check")[3]
    assert rec["verdict"] == "error" and rec["error"]["incidents"][0]["step"] == "crash"
    assert {e for e, r in results(rd, "timestamp_check").items() if r["verdict"] == "error"} == {3}
    logs = d.api("GET", f"/tasks/{task['id']}/logs",
                 params={"stage": "numeric", "level": "warn", "limit": 50}).json()["items"]
    named = [line["msg"] for line in logs if "当时在处理 episode 3" in line["msg"]]
    assert len(named) == 2                                  # both crashes named the episode
    assert any("crashed the process twice" in line["msg"] for line in logs)
    assert 3 not in _passed(rd, 1)


def test_an_errored_episode_is_held_until_a_retry_and_the_state_is_recomputed(daemon, faulty):
    faulty("FAKE_ERROR", "task_success:0")
    d = daemon()
    first = d.wait(d.create()["id"])
    assert first["state"] == "completed_with_errors", json.dumps(first)[:2000]
    assert first["summary"]["held"] == 1 and first["delivery_stale"] is False
    ts = {m["id"]: m for m in first["modules"]}["task_success"]
    assert (ts["state"], ts["episodes_error"]) == ("completed_with_errors", 1)
    rd = d.run_dir(first["id"])
    assert 0 not in _passed(rd, 1)                          # not delivered ...
    assert 0 not in results(rd, "dedup")                    # ... and in no later stage
    manifest = json.load(open(os.path.join(rd, "export", "manifest.json"), encoding="utf-8"))
    assert 0 not in {e["episode_index"] for e in manifest["episodes"]}
    batch = d.delivery(first["run_id"])
    assert os.path.isfile(os.path.join(batch, "_COMPLETE"))
    assert not os.path.exists(os.path.join(d.delivery(), "latest"))   # not a complete success

    faulty.switch.unlink()                                  # the model answers again
    r = d.api("POST", f"/tasks/{first['id']}/retry", json={})
    assert r.status_code == 202, r.text
    assert r.json()["subtask"]["scope"] == {"modules": ["task_success"], "episodes": "errors"}
    done = d.wait(first["id"])
    assert done["state"] == "succeeded" and done["result_rev"] == 2, json.dumps(done)[:2000]
    assert done["summary"] == {"total": 8, "passed": 5, "rejected": 3, "held": 0, "review": 2,
                               "pass_rate": 0.625}
    assert 0 in _passed(rd, 2) and 0 not in _passed(rd, 1)  # r0001 is kept as it was
    part2 = read_jsonl(os.path.join(rd, "checks", "task_success", "parts", "0002.jsonl"))
    assert [r["episode_index"] for r in part2] == [0]       # only the held one ran again
    assert done["delivery_stale"] is True                   # the verdicts changed: re-export
    timeline = d.api("GET", f"/tasks/{first['id']}/timeline").json()["items"]
    assert [e["revision"] for e in timeline if e["kind"] == "revision"] == [1, 2]
    subs = d.api("GET", f"/tasks/{first['id']}/subtasks").json()["items"]
    assert [(s["kind"], s["state"], s["result_rev"]) for s in subs] == [("retry", "succeeded", 2)]

    r = d.api("POST", f"/tasks/{first['id']}/reexport")
    assert r.status_code == 202, r.text
    exported = d.wait(first["id"])
    assert exported["delivery_stale"] is False and exported["state"] == "succeeded"
    with open(os.path.join(d.delivery(), "latest"), encoding="utf-8") as fh:
        assert fh.read().strip() == first["run_id"]     # now complete: latest moves


def test_a_module_that_fails_as_a_whole_leaves_the_rest_running_and_a_retry_runs_it(
        daemon, faulty):
    faulty("FAKE_FAIL_MODULE", "task_success")
    d = daemon()
    first = d.wait(d.create()["id"])
    assert first["state"] == "completed_with_errors", json.dumps(first)[:2000]
    mods = {m["id"]: m for m in first["modules"]}
    assert mods["task_success"]["state"] == "failed" and "injected" in mods["task_success"]["error"]
    assert mods["visual_quality"]["state"] == "succeeded"
    stages = {s["id"]: s["state"] for s in first["progress"]["stages"]}
    assert stages["vlm"] == "failed" and stages["final"] == "succeeded"
    assert first["summary"]["passed"] == 0 and first["summary"]["held"] == 6

    faulty.switch.unlink()
    r = d.api("POST", f"/tasks/{first['id']}/retry", json={"modules": ["task_success"]})
    assert r.status_code == 202, r.text
    done = d.wait(first["id"])
    assert done["state"] == "succeeded", json.dumps(done)[:2000]
    assert done["summary"]["passed"] == 5 and done["summary"]["held"] == 0
    assert {m["id"]: m["state"] for m in done["modules"]}["task_success"] == "succeeded"
