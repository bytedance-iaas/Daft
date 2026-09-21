"""A second result revision after human decisions, exported incrementally (W3).

The Daemon's adjudication sequence (doc 02 §3.9, doc 06 §5) on the fixture,
after a complete first run:

    adjudicate-apply -> check task_success (the relabelled episodes, a new part)
    -> aggregate funnel -> check dedup -> check skill_profile --incremental
    -> aggregate final -> report -> export --incremental -> verify

all on revision 2, with revision 1 left as it was.
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
            first = {"export": c.steps["export"],
                     "manifest": _json(c.rd, "export", "manifest.json")}
            decisions = str(tmp / "decisions.json")
            with open(decisions, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": "1.0", "decisions": [
                    {"id": 1, "episode_index": 3, "line": "task_verdict", "decision": "failure",
                     "new_label": None, "note": "the block never reaches the bin",
                     "decided_by": "alice", "decided_at": 1790000000000},
                    {"id": 2, "episode_index": 4, "line": "label", "decision": "custom_label",
                     "new_label": NEW_LABEL, "note": None, "decided_by": "alice",
                     "decided_at": 1790000000001}]}, fh)
            applied = c.step("apply", "adjudicate-apply", "--run-dir", c.rd,
                             "--decisions", decisions)
            rerun = applied.doc["rerun_task_success"]
            c.step("rejudge", "check", "--modules", "task_success", *c.common(),
                   "--episodes", ",".join(map(str, rerun)), *c.vlm)
            c.step("funnel2", "aggregate", "--run-dir", c.rd, "--phase", "funnel",
                   "--revision", "2", "--episodes", "0-7")
            keep = c.path("revisions", "r0002", "keep.txt")
            c.step("dedup2", "check", "--modules", "dedup", *c.common(), "--episodes",
                   "@" + keep, "--survivors-out", c.path("stages", "dedup2.txt"))
            c.step("profile2", "check", "--modules", "skill_profile", *c.common(),
                   "--episodes", "@" + c.path("stages", "dedup2.txt"), "--incremental",
                   *c.vlm)
            c.step("final2", "aggregate", "--run-dir", c.rd, "--phase", "final",
                   "--revision", "2", "--episodes", "0-7", "--input", c.ds)
            c.step("report2", "report", "--run-dir", c.rd, "--revision", "2")
            c.deliver(delivery, "--revision", "2", "--incremental")
    c.first, c.delivery = first, delivery
    return c


def test_decisions_name_what_runs_next(flow):
    doc = flow.steps["apply"].doc
    assert doc["applied"] == 2 and doc["rerun_task_success"] == [4]
    assert doc["profile_resync"] == [3, 4]
    assert doc["label_changes"] == [{"episode_index": 4, "new_label": NEW_LABEL}]
    rejudged = flow.steps["rejudge"].doc["modules"]["task_success"]
    assert rejudged["part"] == "0002" and rejudged["episodes"]["total"] == 1
    rec = read_jsonl(flow.path("checks", "task_success", "parts", "0002.jsonl"))[0]
    assert rec["episode_index"] == 4
    assert rec["details"]["task_desc"] == NEW_LABEL
    assert rec["details"]["task_desc_source"] == "人工改标"


def test_revision_2_carries_the_decisions_and_revision_1_is_untouched(flow):
    r1, r2 = flow.path("revisions", "r0001"), flow.path("revisions", "r0002")
    assert _eps(r1, "passed") == [0, 1, 3, 4, 6]
    assert _eps(r2, "passed") == [0, 1, 4, 6]
    assert _eps(r2, "reject") == [2, 3, 5, 7] and _eps(r2, "held") == []
    assert _eps(r2, "review") == [7]                  # 3 was decided; 7's question stays
    reject = {e["episode_index"]: e for e in _json(r2, "reject.json")["episodes"]}
    assert reject[3]["reasons"] == [{"module": "task_success", "kind": "human",
                                     "text": "人工裁决判失败(任务未完成)"}]
    passed = {e["episode_index"]: e for e in _json(r2, "passed.json")["episodes"]}
    assert passed[4]["task_text"] == {"text": NEW_LABEL, "source": "人工改标"}
    assert _json(r2, "adjudications.json") == {"applied": [1, 2]}
    commit = _json(r2, "commit.json")
    assert commit["parts"]["task_success"] == ["0001", "0002"]
    report = _json(r2, "report.json")
    assert report["overview"]["counts"] == {"total": 8, "passed": 4, "rejected": 4,
                                            "held": 0, "review": 1}


def test_the_second_export_is_incremental(flow):
    first, second = flow.first["export"].doc, flow.steps["export"].doc
    assert first["incremental"] is False and first["episodes"] == 5
    assert second["incremental"] is True and second["full_reason"] is None
    assert second["episodes"] == 4
    # 3 leaves; 4 (relabelled) and 6 move up one slot: renumbered, videos renamed
    assert second["diff"] == {"keep": 2, "relabel": 0, "renumber": 2, "add": 0, "drop": 1}
    assert second["videos_copied"] == 0 and second["videos_renamed"] == 4
    man = _json(flow.rd, "export", "manifest.json")
    assert [e["episode_index"] for e in man["episodes"]] == [0, 1, 4, 6]
    by_ep = {e["episode_index"]: e for e in man["episodes"]}
    assert by_ep[4]["task"] == {"text": NEW_LABEL, "source": "人工改标"}
    tasks = read_jsonl(flow.path("export", "lerobot_curated", "meta", "episodes.jsonl"))
    assert tasks[by_ep[4]["new_index"]]["tasks"] == [NEW_LABEL]
    assert flow.steps["verify"].doc["failed"] == []
    assert flow.steps["verify"].doc["complete_marker"] is True
    assert _json(flow.delivery, "export", "manifest.json")["fingerprint"] == man["fingerprint"]


def test_export_syncs_to_tos_and_verify_completes_it(flow, tmp_path, cloud, monkeypatch):
    """--output tos://: _COMPLETE goes first, stale files go, the two manifests come last,
    and only the output key set is used; verify then reads it back and completes it."""
    rd = str(tmp_path / "run")
    shutil.copytree(flow.rd, rd)
    prefix = "deliveries/droid-50/run1"
    bucket = cloud.bucket("dst-bucket", readers={"out-ak"})
    bucket[f"{prefix}/_COMPLETE"] = b""                                  # a verified old state
    stale = f"{prefix}/export/lerobot_curated/data/chunk-000/episode_000009.parquet"
    bucket[stale] = b"old"
    monkeypatch.setenv("CURATION_OUTPUT_TOS_ACCESS_KEY", "out-ak")
    monkeypatch.setenv("CURATION_OUTPUT_TOS_SECRET_KEY", "out-sk")
    url = f"tos://dst-bucket/{prefix}"
    res = run("export", "--run-dir", rd, "--input", flow.ds, "--revision", "2", "--output", url)
    assert res.rc == 0, res.doc
    assert f"{prefix}/_COMPLETE" not in bucket and stale not in bucket
    man = _json(rd, "export", "manifest.json")
    for rel, info in man["files"].items():
        assert len(bucket[f"{prefix}/export/lerobot_curated/{rel}"]) == info["size"], rel
    puts = [c[2] for c in cloud.calls if c[0] == "put"]
    assert puts[-2:] == [f"{prefix}/export/manifest.detail.json",
                         f"{prefix}/export/manifest.json"]
    assert {c["access_key"] for c in cloud.clients} == {"out-ak"}

    # what the Daemon uploads as the run goes, then the read-back
    for dirpath, dirs, files in os.walk(rd):
        rel_dir = os.path.relpath(dirpath, rd).replace(os.sep, "/")
        if rel_dir == "export/lerobot_curated" or rel_dir.startswith("export/lerobot_curated/"):
            continue
        for name in files:
            if name == "inflight.json":
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            with open(os.path.join(dirpath, name), "rb") as fh:
                bucket[f"{prefix}/{rel}"] = fh.read()
    res = run("verify", "--run-dir", rd, "--output", url, "--visibility-timeout", "0")
    assert res.rc == 0 and res.doc["failed"] == [] and res.doc["complete_marker"] is True
    assert f"{prefix}/_COMPLETE" in bucket


def test_export_stops_when_the_source_changed(flow, tmp_path):
    rd = str(tmp_path / "run")
    shutil.copytree(flow.rd, rd)
    parquet = os.path.join(flow.ds, "data", "chunk-000", "episode_000001.parquet")
    with open(parquet, "rb") as fh:
        original = fh.read()
    try:
        with open(parquet, "ab") as fh:
            fh.write(b"\0")
        res = run("export", "--run-dir", rd, "--input", flow.ds, "--revision", "2")
        assert res.rc == 6 and res.doc["error"]["code"] == "source_changed", res.doc
    finally:
        with open(parquet, "wb") as fh:
            fh.write(original)
