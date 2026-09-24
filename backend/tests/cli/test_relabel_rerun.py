"""How a relabelled episode is judged again (D39, contract 1.4).

``decisions.json`` carries ``relabel_rerun``: ``v1`` (default) is v1's rejudge
itself - multi-view scoring and the per-camera end-state vote, nothing else -
so the requests are v1's byte for byte; ``full`` is the first run's whole
task_success flow. adjudicate-apply records the choice with every relabel it
applies, and check judges each relabel the way it was recorded.
"""
from __future__ import annotations

import json
import os
import shutil

from curation.pipeline.records import latest_results, read_jsonl

from .fakevlm_server import FakeVlmServer
from .pipeline import run

LABEL = "stack the cups"
ORIGINAL = "pick up the red block and place it in the bin"
ARBITRATION = ("You are preparing a verification checklist", "locate two things",
               "You are verifying whether a robot manipulation task succeeded",
               "Re-examine the action and object trajectory")
BOOKKEEPING = ("task_desc", "task_desc_source", "relabel_rerun")


def _decisions(path: str, items, relabel_rerun: str | None = None, first_id: int = 1) -> str:
    doc = {"schema_version": "1.0", "decisions": [
        {"id": i, "episode_index": ep, "line": line, "decision": decision,
         "new_label": label, "note": None, "decided_by": "alice",
         "decided_at": 1790000000000 + i}
        for i, (ep, line, decision, label) in enumerate(items, start=first_id)]}
    if relabel_rerun is not None:
        doc["relabel_rerun"] = relabel_rerun
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def _labels(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "adjudication", "labels.json"), encoding="utf-8") as fh:
        return json.load(fh)["labels"]


def _posts(server: FakeVlmServer) -> list[str]:
    return sorted(json.dumps(c["payload"], sort_keys=True, ensure_ascii=False)
                  for c in server.calls if c["path"].endswith("/chat/completions"))


def test_the_choice_is_recorded_with_every_relabel(vlm_stage, tmp_path):
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    first = run("adjudicate-apply", "--run-dir", rd, "--decisions",
                _decisions(str(tmp_path / "d1.json"),
                           [(1, "label", "custom_label", LABEL),
                            (3, "task_verdict", "success", None)]))
    assert first.rc == 0 and first.doc["relabel_rerun"] == "v1"      # the default
    lines = {d["id"]: d for d in read_jsonl(os.path.join(rd, "adjudication",
                                                          "applied.jsonl"))}
    assert lines[1]["relabel_rerun"] == "v1" and "relabel_rerun" not in lines[2]
    assert _labels(rd)["1"]["relabel_rerun"] == "v1"
    # a later application with the other choice leaves the earlier relabel as it was
    second = run("adjudicate-apply", "--run-dir", rd, "--decisions",
                 _decisions(str(tmp_path / "d2.json"),
                            [(4, "label", "adopt_suggestion", "wipe the table")],
                            relabel_rerun="full", first_id=3))
    assert second.rc == 0 and second.doc["relabel_rerun"] == "full"
    assert second.doc["rerun_task_success"] == [4]
    assert {k: v["relabel_rerun"] for k, v in _labels(rd).items()} == {"1": "v1", "4": "full"}
    bad = run("adjudicate-apply", "--run-dir", rd, "--decisions",
              _decisions(str(tmp_path / "d3.json"), [(6, "label", "custom_label", "x")],
                         relabel_rerun="all", first_id=4))
    assert bad.rc == 2 and "relabel_rerun" in bad.doc["error"]["message"]


def _v1_rerun(vlm_stage, url: str, episode: int, label: str) -> dict:
    """v1's own re-judge (``rejudge._build_rerun``) against the fake model at ``url``."""
    from curation.pipeline.config import apply_vlm_direct, load_config
    from curation.pipeline.rejudge import _build_rerun

    cfg = load_config(None)
    apply_vlm_direct(cfg, endpoint=url, model="fake-vlm", api_key_env=None)
    return _build_rerun(cfg)(vlm_stage["dataset"], f"ep{episode:06d}", label)


def _relabel_and_check(vlm_stage, tmp_path, name: str, episode: int, label: str,
                       relabel_rerun: str):
    rd = str(tmp_path / name)
    shutil.copytree(vlm_stage["reference_dir"], rd)
    applied = run("adjudicate-apply", "--run-dir", rd, "--decisions",
                  _decisions(str(tmp_path / f"{name}.json"),
                             [(episode, "label", "custom_label", label)],
                             relabel_rerun=relabel_rerun))
    assert applied.rc == 0 and applied.doc["rerun_task_success"] == [episode]
    with FakeVlmServer() as vlm:
        res = run("check", "--modules", "task_success", "--input", vlm_stage["dataset"],
                  "--run-dir", rd, "--episodes", str(episode), "--vlm-endpoint", vlm.url,
                  "--vlm-model", "fake-vlm")
    assert res.rc == 0, res.doc
    return latest_results(rd, "task_success")[episode], vlm


def test_v1_relabels_send_v1s_requests_and_reach_v1s_verdict(vlm_stage, tmp_path):
    for episode in (1, 3):
        rec, v2 = _relabel_and_check(vlm_stage, tmp_path, f"v1-{episode}", episode, LABEL,
                                     "v1")
        with FakeVlmServer() as v1:
            ref = _v1_rerun(vlm_stage, v1.url, episode, LABEL)
        assert _posts(v2) == _posts(v1), episode                  # byte for byte
        assert not any(p in c["text"] for c in v2.calls for p in ARBITRATION)
        assert rec["passed"] == ref["passed"]
        details = {k: v for k, v in rec["details"].items() if k not in BOOKKEEPING}
        assert details == {k: v for k, v in json.loads(ref["detail"]).items() if k not in BOOKKEEPING}
        assert (rec["details"]["task_desc"], rec["details"]["task_desc_source"],
                rec["details"]["relabel_rerun"]) == (LABEL, "人工改标", "v1")


def test_full_relabels_run_the_first_runs_flow(vlm_stage, tmp_path):
    """Episode 1 relabelled with its own annotation: the first run abstained after the
    two layers and arbitration rescued it. v1's re-judge stops at the abstention;
    the full flow is the first run again - the same decision, another verdict."""
    two_layers, _ = _relabel_and_check(vlm_stage, tmp_path, "v1-same", 1, ORIGINAL, "v1")
    full, v2 = _relabel_and_check(vlm_stage, tmp_path, "full-same", 1, ORIGINAL, "full")
    assert two_layers["verdict"] == "abstain"
    assert full["verdict"] == "pass" and full["details"]["verdict"] == "arbitration_success"
    assert any(p in c["text"] for c in v2.calls for p in ARBITRATION)
    first_run = vlm_stage["reference"][1]
    assert {k: v for k, v in full["details"].items() if k not in BOOKKEEPING} == \
        {k: v for k, v in first_run["details"].items() if k not in BOOKKEEPING}
    assert (full["details"]["task_desc_source"], full["details"]["relabel_rerun"]) == \
        ("人工改标", "full")
