"""A restored appeal end to end: back into the delivery, never held (W3 fix).

On the fixture, task_success is made to reject episodes 0 (annotated) and 6
(captioned) alone - the fake model never rejects - so the first revision rejects
them and dedup and skill_profile never see them. A person restores both; the
Daemon's adjudication sequence then runs without dedup:

    adjudicate-apply -> aggregate funnel -> check skill_profile --incremental on
    keep.txt -> aggregate final -> report -> export --incremental -> verify

Both come back to passed, are filed into the profile from their text with no
model caption (v1's ``_sync_profile``), and are delivered again.
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
    """Turn the task_success records of ``episodes`` into a failure of the model."""
    part = os.path.join(run_dir, "checks", "task_success", "parts", "0001.jsonl")
    out = []
    for rec in read_jsonl(part):
        if rec["episode_index"] in episodes:
            rec.update(verdict="fail", passed=False)
            rec["details"] = dict(rec["details"], verdict="failure",
                                  reason="复核判未完成")
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
            c.first_export = c.steps["export"].doc
            path = str(tmp / "decisions.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": "1.0", "decisions": [
                    {"id": i, "episode_index": ep, "line": "reject_appeal",
                     "decision": "restore", "new_label": None, "note": None,
                     "decided_by": "alice", "decided_at": 1790000000000 + i}
                    for i, ep in enumerate((0, 6), start=1)]}, fh)
            c.step("apply", "adjudicate-apply", "--run-dir", c.rd, "--decisions", path)
            c.step("funnel2", "aggregate", "--run-dir", c.rd, "--phase", "funnel",
                   "--revision", "2", "--episodes", "0-7")
            captions_before = vlm.count(CAPTION)
            c.step("profile2", "check", "--modules", "skill_profile", *c.common(),
                   "--episodes", "@" + c.path("revisions", "r0002", "keep.txt"),
                   "--incremental", *c.vlm)
            c.captions_in_profile2 = vlm.count(CAPTION) - captions_before
            c.step("final2", "aggregate", "--run-dir", c.rd, "--phase", "final",
                   "--revision", "2", "--episodes", "0-7", "--input", c.ds)
            c.step("report2", "report", "--run-dir", c.rd, "--revision", "2")
            c.deliver(delivery, "--revision", "2", "--incremental")
    c.delivery = delivery
    return c


def test_the_first_revision_rejects_them_and_nothing_else_saw_them(flow):
    assert _eps(flow.rd, 1, "passed") == [1, 3, 4]
    assert _eps(flow.rd, 1, "reject") == [0, 2, 5, 6, 7]
    assert _eps(flow.rd, 1, "held") == []
    dedup = records.latest_results(flow.rd, "dedup")
    assert sorted(dedup) == [1, 3, 4, 7]                  # the first keep.txt
    assert flow.first_export["episodes"] == 3


def test_restored_appeals_come_back_without_dedup_and_are_filed_from_their_text(flow):
    assert flow.steps["apply"].doc["profile_resync"] == [0, 6]
    counts = flow.steps["funnel2"].doc["counts"]
    assert (counts["keep"], counts["decided_in"], counts["decided_out"]) == (4, 2, 0)
    with open(flow.path("revisions", "r0002", "keep.txt"), encoding="utf-8") as fh:
        assert fh.read().split() == ["0", "1", "3", "4", "6", "7"]
    assert sorted(os.listdir(flow.path("checks", "dedup", "parts"))) == ["0001.jsonl"]
    profile = flow.steps["profile2"].doc["modules"]["skill_profile"]
    assert profile["episodes"]["total"] == 5 and profile["error_episodes"] == []
    assert flow.captions_in_profile2 == 0                  # no model caption for them
    rows = {r["episode_id"]: r for r in
            read_jsonl(flow.path("checks", "skill_profile", "assignments.jsonl"))}
    assert sorted(rows) == ["ep000000", "ep000001", "ep000003", "ep000004", "ep000006"]
    assert rows["ep000000"]["grouping_text_source"] == "原始标注"
    assert rows["ep000006"]["grouping_text_source"] == "自产caption"
    auto = {line["episode_index"]: line["caption"]
            for line in read_jsonl(flow.path("autolabel", "captions.jsonl"))}
    assert rows["ep000006"]["grouping_text"] == auto[6]


def test_the_second_revision_delivers_them_again(flow):
    assert _eps(flow.rd, 2, "passed") == [0, 1, 3, 4, 6]
    assert _eps(flow.rd, 2, "reject") == [2, 5, 7]
    assert _eps(flow.rd, 2, "held") == []
    second = flow.steps["export"].doc
    assert second["incremental"] is True and second["episodes"] == 5
    assert second["diff"]["add"] == 2 and second["diff"]["drop"] == 0
    assert flow.steps["verify"].doc["failed"] == []
    assert flow.steps["verify"].doc["complete_marker"] is True
