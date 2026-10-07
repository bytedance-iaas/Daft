"""A second result revision after human decisions, exported incrementally (W3).

The Daemon's adjudication sequence (doc 02 §3.9, doc 06 §5) on the fixture,
after a complete first run:

    adjudicate-apply -> check task_success (the relabelled episodes, a new part)
    -> aggregate funnel
    -> aggregate final -> report -> verify

all on revision 2, with revision 1 left as it was. Dedup is not run again: its
groups stand, and aggregate picks each group's keeper after the human decisions (design
doc 17 §4.5) - 3 judged failed, its byte copy 7 is delivered in its place.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from .conftest import ENV_VARS
from .fakevlm_server import FakeVlmServer
from .pipeline import Chain, read_jsonl, run

NEW_LABEL = "wipe the table"


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def _eps(rev_dir: str, name: str) -> list[int]:
    return [e["episode_index"] for e in _json(rev_dir, f"{name}.json")["episodes"]]


@pytest.fixture(scope="module")
def flow(tmp_path_factory, mini_dataset):
    tmp = tmp_path_factory.mktemp("revision")
    dataset = str(tmp / "mini")
    shutil.copytree(mini_dataset, dataset)          # the last test changes it
    delivery = str(tmp / "delivery")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer() as vlm:
            c = Chain(dataset, str(tmp / "run"), vlm.url)
            c.front()
            c.funnel()
            c.post()
            c.deliver(delivery)
            decisions = str(tmp / "decisions.json")
            with open(decisions, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": "1.0", "decisions": [
                    {"id": 1, "episode_index": 3, "line": "task_verdict", "decision": "failure",
                     "new_label": None, "note": "the block never reaches the bin",
                     "decided_by": "alice", "decided_at": 1790000000000},
                    {"id": 2, "episode_index": 4, "line": "task_verdict", "decision": "unsure",
                     "new_label": NEW_LABEL, "note": None, "decided_by": "alice",
                     "decided_at": 1790000000001}]}, fh)
            applied = c.step("apply", "adjudicate-apply", "--run-dir", c.rd,
                             "--decisions", decisions)
            rerun = applied.doc["rerun_task_success"]
            c.step("rejudge", "check", "--modules", "task_success", *c.common(),
                   "--episodes", ",".join(map(str, rerun)), *c.vlm)
            c.step("funnel2", "aggregate", "--run-dir", c.rd, "--phase", "funnel",
                   "--revision", "2", "--episodes", "0-7")
            c.step("final2", "aggregate", "--run-dir", c.rd, "--phase", "final",
                   "--revision", "2", "--episodes", "0-7", "--input", c.ds)
            c.step("report2", "report", "--run-dir", c.rd, "--revision", "2")
            c.deliver(delivery)
    c.delivery = delivery
    return c


def test_decisions_name_what_runs_next(flow):
    doc = flow.steps["apply"].doc
    assert doc["applied"] == 2 and doc["rerun_task_success"] == [4]
    assert doc["label_changes"] == [{"episode_index": 4, "new_label": NEW_LABEL}]
    rejudged = flow.steps["rejudge"].doc["modules"]["task_success"]
    assert rejudged["part"] == "0002" and rejudged["episodes"]["total"] == 1
    rec = read_jsonl(flow.path("checks", "task_success", "parts", "0002.jsonl"))[0]
    assert rec["episode_index"] == 4
    assert rec["details"]["task_desc"] == NEW_LABEL
    assert rec["details"]["task_desc_source"] == "人工改标"


def test_the_first_dedup_stands_after_the_decisions(flow):
    """keep.txt of revision 2 drops the episode a person judged failed; dedup is not
    run again, its group {3, 7} stands and keeps 7 now that 3 is gone (D58); the
    the kept set loses 3."""
    with open(flow.path("revisions", "r0002", "keep.txt"), encoding="utf-8") as fh:
        assert fh.read().split() == ["0", "1", "4", "6", "7"]
    counts = flow.steps["funnel2"].doc["counts"]
    assert counts["keep"] == 6 and counts["decided_out"] == 1 and counts["decided_in"] == 0
    assert sorted(os.listdir(flow.path("checks", "dedup", "parts"))) == ["0001.jsonl"]


def test_revision_2_carries_the_decisions_and_revision_1_is_untouched(flow):
    r1, r2 = flow.path("revisions", "r0001"), flow.path("revisions", "r0002")
    assert _eps(r1, "passed") == [0, 1, 3, 4, 6]
    assert _eps(r2, "passed") == [0, 1, 4, 6, 7]
    assert _eps(r2, "reject") == [2, 3, 5] and _eps(r2, "held") == []
    # 3 was decided; 0 is still asked, 7 - delivered in 3's place - is asked whether its
    # task succeeded instead of whether its reject stands; v1's two layers (D39) abstain
    # on 4's new label
    assert _eps(r2, "review") == [0, 4, 7]
    review = {e["episode_index"]: e for e in _json(r2, "review.json")["episodes"]}
    assert [i["line"] for i in review[7]["review"]] == ["task_verdict"]
    reject = {e["episode_index"]: e for e in _json(r2, "reject.json")["episodes"]}
    assert reject[3]["reasons"] == [{"module": "task_success", "kind": "human", "code": "failure",
                                     "item": "TASK-5", "appealable": False,
                                     "text": "人工裁决判失败（任务未完成）"}]
    passed = {e["episode_index"]: e for e in _json(r2, "passed.json")["episodes"]}
    assert passed[4]["task_text"] == {"text": NEW_LABEL, "source": "人工改标"}
    assert _json(r2, "adjudications.json") == {"applied": [1, 2]}
    commit = _json(r2, "commit.json")
    assert commit["parts"]["task_success"] == ["0001", "0002"]
    report = _json(r2, "report.json")
    assert report["overview"]["counts"] == {"total": 8, "passed": 5, "rejected": 3,
                                            "held": 0, "review": 3, "skipped": 0}
