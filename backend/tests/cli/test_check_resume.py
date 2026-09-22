"""``check --resume`` after SIGTERM and after SIGKILL gives the uninterrupted results (W3).

The VLM stage runs as a child process against a slowed-down fake endpoint; once
it has written its first episode it is stopped, then the same command with
``--resume`` finishes the rest. The records must equal those of a run that was
never interrupted (timing fields aside). Also: SIGTERM finishes the episode in
flight and exits 5; SIGKILL leaves ``inflight.json`` naming it; an episode found
twice in a dead process's hands is recorded as an error and skipped (P14).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import time

from curation.pipeline.records import latest_results

from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, comparable, results, run, subprocess_cli


def _vlm_args(p, run_dir, url):
    return ["check", "--modules", "task_success", "--input", p["dataset"], "--run-dir", run_dir,
            "--episodes", p["episodes"], "--vlm-endpoint", url, "--vlm-model", "fake-vlm"]


def _wait_for_first_record(run_dir: str, proc, timeout: float = 120.0) -> None:
    part = os.path.join(run_dir, "checks", "task_success", "parts", "0001.jsonl")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"the check ended early: {proc.communicate()}")
        if os.path.isfile(part):
            with open(part, encoding="utf-8") as fh:
                if fh.read().count("\n") >= 1:
                    return
        time.sleep(0.05)
    raise AssertionError("no record written in time")


def _interrupt(p, name: str, sig: int):
    run_dir = str(p["tmp"] / name)
    shutil.copytree(p["base"], run_dir)
    with FakeVlmServer(delay_s=0.08) as slow:
        proc = subprocess_cli(*_vlm_args(p, run_dir, slow.url))
        try:
            _wait_for_first_record(run_dir, proc)
            proc.send_signal(sig)
            out, err = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
    return run_dir, proc.returncode, out, err


def _resume(p, run_dir: str) -> dict:
    with FakeVlmServer() as vlm:
        res = run(*_vlm_args(p, run_dir, vlm.url), "--resume")
    assert res.rc == 0, res.doc
    return res.doc


def test_sigterm_finishes_the_episode_in_flight_then_resume_completes(vlm_stage):
    run_dir, rc, out, _err = _interrupt(vlm_stage, "term", signal.SIGTERM)
    assert rc == 5
    assert json.loads(out.strip().splitlines()[-1])["error"]["code"] == "terminated"
    done = results(run_dir, "task_success")
    assert 1 <= len(done) < len(vlm_stage["reference"])
    assert all(r["verdict"] != "error" for r in done.values())       # finished, not cut
    doc = _resume(vlm_stage, run_dir)
    entry = doc["modules"]["task_success"]
    assert entry["skipped_existing"] == len(done)
    assert entry["part"] == "0002"
    got = results(run_dir, "task_success")
    ref = vlm_stage["reference"]
    assert sorted(got) == sorted(ref)
    assert {e: comparable(r) for e, r in got.items()} == \
        {e: comparable(r) for e, r in ref.items()}


def test_sigkill_leaves_inflight_and_resume_gives_the_same_results(vlm_stage):
    run_dir, rc, _out, _err = _interrupt(vlm_stage, "kill", signal.SIGKILL)
    assert rc == -signal.SIGKILL
    with open(os.path.join(run_dir, "checks", "task_success", "inflight.json"),
              encoding="utf-8") as fh:
        inflight = json.load(fh)
    assert inflight["episodes"] and inflight["part"] == "0001"
    done_before = set(latest_results(run_dir, "task_success"))     # the parts: no compaction
    assert done_before and not (set(inflight["episodes"]) & done_before)
    assert not os.path.exists(os.path.join(run_dir, "checks", "task_success", "results.jsonl"))
    _resume(vlm_stage, run_dir)
    got = results(run_dir, "task_success")
    ref = vlm_stage["reference"]
    assert {e: comparable(r) for e, r in got.items()} == \
        {e: comparable(r) for e, r in ref.items()}
    assert not os.path.exists(os.path.join(run_dir, "checks", "task_success",
                                           "inflight.json"))


def test_an_episode_that_crashed_the_process_twice_is_skipped(vlm_stage, tmp_path):
    run_dir = str(tmp_path / "crash")
    shutil.copytree(vlm_stage["base"], run_dir)
    numeric = ("timestamp_check", "kinematic_limits", "motion_quality")
    before = results(run_dir, "timestamp_check")[3]

    def crashed_with(ep: int) -> None:
        """What a call killed while working on ``ep`` leaves: inflight.json, no record."""
        for m in numeric:
            with open(os.path.join(run_dir, "checks", m, "inflight.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"pid": 999999, "part": "0009", "episodes": [ep]}, fh)
            parts = os.path.join(run_dir, "checks", m, "parts")
            for name in os.listdir(parts):
                path = os.path.join(parts, name)
                with open(path, encoding="utf-8") as fh:
                    lines = [ln for ln in fh if json.loads(ln)["episode_index"] != ep]
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines)

    args = ["check", "--modules", Chain.NUMERIC, "--input", vlm_stage["dataset"],
            "--run-dir", run_dir, "--episodes", "0-7", "--resume"]
    crashed_with(3)
    first = run(*args)                                  # first crash: tried again
    assert first.rc == 0, first.doc
    assert comparable(results(run_dir, "timestamp_check")[3]) == comparable(before)
    assert first.doc["modules"]["timestamp_check"]["skipped_existing"] == 7
    crashed_with(3)
    second = run(*args)                                 # second crash: recorded, skipped
    assert second.rc == 0, second.doc
    for m in numeric:
        rec = results(run_dir, m)[3]
        assert rec["verdict"] == "error"
        assert rec["error"] == {"kind": "execution", "incidents": [
            {"step": "crash", "cause": "the process died twice while working on this episode"}]}
        assert second.doc["modules"][m]["error_episodes"] == [3]
        assert not os.path.exists(os.path.join(run_dir, "checks", m, "inflight.json"))
    with open(os.path.join(run_dir, "checks", "timestamp_check", "crashes.json"),
              encoding="utf-8") as fh:
        assert json.load(fh) == {"3": 2}
