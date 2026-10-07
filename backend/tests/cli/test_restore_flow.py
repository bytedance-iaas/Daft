"""A restored appeal end to end: back into the delivery, never held (W3 fix).

On the fixture, task_success is made to reject episodes 0 (annotated) and 6
(captioned) alone - the fake model never rejects - so the first revision rejects
them; dedup judged them all the same, as every episode (design doc
17 §3, D57). A person restores both; the Daemon's adjudication sequence then runs
without dedup:

    -> aggregate final -> report -> verify

Both come back to passed with no model call (the profile filed them the first time),
and are delivered again.
"""
from __future__ import annotations

import json
import os

import pytest

from curation.pipeline import records

from .conftest import ENV_VARS
from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, read_jsonl

CAPTION = "All cameras show the SAME robot episode"


def _reject_by_task_success(run_dir: str, episodes: set[int]) -> None:
    """Turn the task_success records of ``episodes`` into a failure of the model: the record
    its answer would give, written as ``check`` writes it (records 2.0)."""
    part = os.path.join(run_dir, "checks", "task_success", "parts", "0001.jsonl")
    out = []
    for rec in read_jsonl(part):
        if rec["episode_index"] in episodes:
            details = dict(rec["details"], verdict="failure", reason="复核判未完成")
            rec = records.record_v2("task_success", rec["episode_index"], False, None, details,
                                    error=None, evidence=rec.get("evidence"),
                                    elapsed_s=rec.get("elapsed_s"))
        out.append(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(part, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    records.compact(run_dir, "task_success")


def _eps(run_dir: str, revision: int, name: str) -> list[int]:
    path = os.path.join(run_dir, "revisions", f"r{revision:04d}", f"{name}.json")
    with open(path, encoding="utf-8") as fh:
        return [e["episode_index"] for e in json.load(fh)["episodes"]]


@pytest.fixture(scope="module")
def flow(tmp_path_factory, mini_dataset):
    tmp = tmp_path_factory.mktemp("restore")
    delivery = str(tmp / "delivery")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer() as vlm:
            c = Chain(mini_dataset, str(tmp / "run"), vlm.url)
            c.front()
            c.funnel()
            _reject_by_task_success(c.rd, {0, 6})
            c.post()
            c.deliver(delivery)
            path = str(tmp / "decisions.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": "1.0", "decisions": [
                    {"id": i, "episode_index": ep, "line": "reject_appeal",
                     "decision": "restore", "new_label": None, "note": None,
                     "decided_by": "alice", "decided_at": 1790000000000 + i}
                    for i, ep in enumerate((0, 6), start=1)]}, fh)
            c.step("apply", "adjudicate-apply", "--run-dir", c.rd, "--decisions", path)
            c.step("final2", "aggregate", "--run-dir", c.rd, "--phase", "final",
                   "--revision", "2", "--episodes", "0-7", "--input", c.ds)
            c.step("report2", "report", "--run-dir", c.rd, "--revision", "2")
            c.deliver(delivery)
    c.delivery = delivery
    return c


def test_the_first_revision_rejects_them_and_every_module_saw_them(flow):
    assert _eps(flow.rd, 1, "passed") == [1, 3, 4]
    assert _eps(flow.rd, 1, "reject") == [0, 2, 5, 6, 7]
    assert _eps(flow.rd, 1, "held") == []
    assert sorted(records.latest_results(flow.rd, "dedup")) == list(range(8))           # the whole selection


def test_restored_appeals_come_back_without_dedup_and_without_a_model_call(flow):
    with open(flow.path("revisions", "r0002", "keep.txt"), encoding="utf-8") as fh:
        assert fh.read().split() == ["0", "1", "3", "4", "6", "7"]
    assert sorted(os.listdir(flow.path("checks", "dedup", "parts"))) == ["0001.jsonl"]


def test_the_second_revision_delivers_them_again(flow):
    assert _eps(flow.rd, 2, "passed") == [0, 1, 3, 4, 6]
    assert _eps(flow.rd, 2, "reject") == [2, 5, 7]
    assert _eps(flow.rd, 2, "held") == []
    assert flow.steps["verify"].doc["failed"] == []
    assert flow.steps["verify"].doc["complete_marker"] is True
