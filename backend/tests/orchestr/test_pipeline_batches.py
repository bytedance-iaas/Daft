"""Real CLI checks interleave across layers and expose per-episode results."""
from __future__ import annotations

import collections
import glob
import json
import os
from pathlib import Path

import pytest

from daemon.orchestr import pipeline
from daemon.orchestr import episode_pipeline

pytestmark = pytest.mark.slow


def test_overlapping_cpu_layers_share_the_plan_limit():
    stages = [{"id": "numeric", "concurrency": 8},
              {"id": "frame", "concurrency": 8}, {"id": "vlm", "gates": {"episode": 16}}]
    shares = pipeline.cpu_shares_for(stages)
    assert shares == {"numeric": 2, "frame": 6}
    assert pipeline.cpu_shares_for([{**s, "concurrency": 1} for s in stages[:2]]) == {
        "numeric": 1, "frame": 1}
    assert pipeline.effective_batch_size(stages, 7) == 3
    assert pipeline.effective_batch_size(stages, 100_000) == 32
    assert pipeline.effective_batch_size(stages, 100_000, configured=2) == 2


def test_batches_overlap_and_episode_result_is_readable_before_revision(daemon, monkeypatch):
    worker_pids: dict[str, set[int]] = collections.defaultdict(set)
    original = episode_pipeline._start_worker

    def traced(run, layer, *args):
        original(run, layer, *args)
        worker_pids[layer.sid].add(layer.child.pid)

    monkeypatch.setattr(episode_pipeline, "_start_worker", traced)
    d = daemon()
    task_id = d.create(params={"start_now": True, "export": True,
                               "vlm_hedge": False, "batch_size": 2})["id"]
    assert d.get(task_id)["params"]["batch_size"] == 2

    def a_finished_episode():
        r = d.api("GET", f"/tasks/{task_id}/pipeline/episodes")
        assert r.status_code == 200, r.text
        return next((row for row in r.json()["items"] if row["verdict"] is not None), None)

    item = d.wait_for(a_finished_episode, what="a live episode verdict")
    assert d.get(task_id)["result_rev"] == 0
    detail = d.api("GET", f"/tasks/{task_id}/pipeline/episodes/{item['episode_index']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["verdict"] == item["verdict"]
    assert detail.json()["modules"]
    timing = detail.json()["stage_processing_s"]
    assert detail.json()["processing_s"] == round(sum(timing.values()), 3)

    task = d.wait(task_id)
    assert task["state"] == "succeeded", task
    assert len(worker_pids["numeric"]) == 1
    assert len(worker_pids["frame"]) == 1
    for stage in task["progress"]["stages"]:
        if stage["id"] in ("numeric", "frame", "vlm"):
            assert stage["pipeline"]["inflight"] == 0
            assert stage["pipeline"]["dispatches"] > 0
            assert stage["pipeline"]["processing"]["mean_s"] >= 0
    page = d.api("GET", f"/tasks/{task_id}/pipeline/episodes").json()
    assert page["started"] == page["finished"] == 8
    with (Path(d.run_dir(task_id)) / "revisions" / "r0001" / "verdicts.jsonl").open(
            encoding="utf-8") as fh:
        committed = {r["episode_index"]: r["verdict"] for r in map(json.loads, fh)}
    assert {r["episode_index"]: r["verdict"] for r in page["items"]} == committed


def test_resume_routes_each_episode_from_its_saved_stage(daemon, fake_vlm, monkeypatch):
    monkeypatch.setattr(pipeline, "batch_size_for", lambda stages: 2)
    fake_vlm.delay_s = 0.15
    d = daemon()
    task_id = d.create()["id"]

    def first_result():
        return d.api("GET", f"/tasks/{task_id}/pipeline/episodes").json()["finished"] > 0

    d.wait_for(first_result, what="the first persisted episode")
    assert d.action(task_id, "pause").status_code == 200
    d.wait(task_id, states=("paused",))
    saved = d.api("GET", f"/tasks/{task_id}/pipeline/episodes").json()
    assert 0 < saved["finished"] < 8

    fake_vlm.delay_s = 0.0
    assert d.action(task_id, "resume").status_code == 200
    task = d.wait(task_id)
    assert task["state"] == "succeeded", task
    assert d.api("GET", f"/tasks/{task_id}/pipeline/episodes").json()["finished"] == 8
    seen = collections.Counter()
    for path in glob.glob(f"{d.run_dir(task_id)}/checks/task_success/parts/*.jsonl"):
        with open(path, encoding="utf-8") as fh:
            seen.update(json.loads(line)["episode_index"] for line in fh if line.strip())
    assert seen and max(seen.values()) == 1, seen


def test_streaming_results_and_requests_match_batch_execution(daemon, monkeypatch):
    import subprocess
    import sys

    from .conftest import comparable, results

    d = daemon()
    params = {"start_now": True, "export": False, "vlm_hedge": False, "batch_size": 1}
    streamed = d.wait(d.create(params=params)["id"])
    assert streamed["state"] == "succeeded", streamed
    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "run_funnel", pipeline.run_batches)
        batched = d.wait(d.create(params=params)["id"])
    assert batched["state"] == "succeeded", batched
    left, right = d.run_dir(batched["id"]), d.run_dir(streamed["id"])
    for module in ("timestamp_check", "kinematic_limits", "motion_quality",
                   "visual_quality", "video_action_sync", "task_success"):
        assert {e: comparable(r) for e, r in results(left, module).items()} == {
            e: comparable(r) for e, r in results(right, module).items()}

    def usage(root):
        counts = collections.Counter()
        for row in map(json.loads, (Path(root) / "usage.jsonl").read_text().splitlines()):
            if row["ledger"] == "actual":
                counts[(row["module"], row["call_kind"])] += row["requests"]
        return counts

    assert usage(left) == usage(right)
    # the parity tool lives in tools/ (not installed): put it on the child's path
    tools = str(Path(__file__).resolve().parents[3] / "tools")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        p for p in (tools, os.environ.get("PYTHONPATH", "")) if p)}
    compared = subprocess.run([sys.executable, "-m", "parity", "compare",
                               "--golden", left, "--candidate", right, "--all-strict",
                               "--json"], capture_output=True, text=True, env=env)
    assert compared.returncode == 0, compared.stdout + compared.stderr


def test_persistent_worker_crash_preserves_completed_episodes(daemon, monkeypatch):
    from .conftest import results
    from daemon.orchestr.stage_worker import StageWorker

    original = StageWorker.poll
    killed = []

    def crash(self):
        event = original(self)
        if not killed and self.cmd.stage == "numeric" and event and event[0] == "episode":
            killed.append(self.pid)
            self.kill()
            self.process.join(timeout=5)
            # Lose the acknowledgement too; recovery must use the durable state.
            raise EOFError("injected crash after commit")
        return event

    monkeypatch.setattr(StageWorker, "poll", crash)
    d = daemon()
    task = d.wait(d.create(modules=["timestamp_check", "motion_quality"],
                           params={"batch_size": 1, "export": False,
                                   "limits": {"cpu_concurrency": 1}})["id"])
    assert task["state"] == "succeeded", task
    assert len(killed) == 1
    assert len(results(d.run_dir(task["id"]), "timestamp_check")) == 8
    counts = collections.Counter()
    for path in Path(d.run_dir(task["id"])).glob("checks/timestamp_check/parts/*.jsonl"):
        counts.update(json.loads(line)["episode_index"] for line in path.read_text().splitlines())
    assert counts == {e: 1 for e in range(8)}


def test_the_data_integrity_layer_goes_first_and_its_episodes_are_readable_live(daemon):
    """design doc 14 §2.2: a task with the module (as the console selects it by default) runs it as the
    first streaming layer, with the plan's concurrency; the live episode view reads its records next
    to v1's (v2's own gates never enter v1's check configuration); its suspects are asked."""
    from .conftest import ALL_MODULES

    d = daemon()
    task_id = d.create(modules=["data_integrity", *ALL_MODULES])["id"]
    task = d.wait(task_id)
    assert task["state"] == "succeeded", task
    stages = [s["id"] for s in task["progress"]["stages"]]
    assert stages.index("integrity") < stages.index("numeric")
    [integ] = [s for s in task["progress"]["stages"] if s["id"] == "integrity"]
    assert integ["pipeline"]["dispatches"] > 0 and integ["total"] == 8
    page = d.api("GET", f"/tasks/{task_id}/pipeline/episodes")
    assert page.status_code == 200, page.text
    assert page.json()["finished"] == 8 and all(r["verdict"] for r in page.json()["items"])
    detail = d.api("GET", f"/tasks/{task_id}/pipeline/episodes/3")
    assert detail.status_code == 200, detail.text
    assert detail.json()["modules"]["data_integrity"]["verdict"] == "abstain"     # the fixture's byte copy
    assert "integrity" in detail.json()["stage_processing_s"]
    queue = d.api("GET", f"/tasks/{task_id}/adjudication").json()
    asked = {(card["episode_index"], q["line"]) for card in queue["items"] for q in card["questions"]}
    assert (3, "integrity_check") in asked                                        # 7, its copy, is dedup's reject
    assert all(line != "integrity_check" for ep, line in asked if ep != 3)
    assert queue["counts"]["pending"] >= 1                                        # it counts as pending
