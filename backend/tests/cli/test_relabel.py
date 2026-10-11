"""How a relabelled episode is judged again (D39, D71, decisions 2.0).

A person who rewrites an episode's task text and leaves the verdict to the model has the episode judged
again with the new text, in the one request of the first run (no review per camera, no arbitration).
adjudicate-apply records the relabel in ``labels.json``; check judges it with that text.
"""
from __future__ import annotations

import json
import os
import shutil

from curation.pipeline.records import latest_results, read_jsonl

from .fakevlm_server import FakeVlmServer
from .pipeline import run, verdict_of

LABEL = "stack the cups"
ORIGINAL = "pick up the red block and place it in the bin"
ARBITRATION = ("You are preparing a verification checklist", "locate two things",
               "You are verifying whether a robot manipulation task succeeded",
               "Re-examine the action and object trajectory")
REVIEW = "Independently review ONLY this camera"
BOOKKEEPING = ("task_desc", "task_desc_source")


def _decisions(path: str, items, first_id: int = 1, **extra) -> str:
    doc = {"schema_version": "2.0", "decisions": [
        {"id": i, "episode_index": ep, "line": line, "decision": decision,
         "new_label": label, "note": None, "decided_by": "alice",
         "decided_at": 1790000000000 + i}
        for i, (ep, line, decision, label) in enumerate(items, start=first_id)], **extra}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def _labels(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "adjudication", "labels.json"), encoding="utf-8") as fh:
        return json.load(fh)["labels"]


def test_a_relabel_is_recorded_and_named_for_re_judging(vlm_stage, tmp_path):
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    first = run("adjudicate-apply", "--run-dir", rd, "--decisions",
                _decisions(str(tmp_path / "d1.json"),
                           [(1, "task_verdict", "unsure", LABEL),
                            (3, "task_verdict", "success", None)]))
    assert first.rc == 0 and first.doc["rerun_task_success"] == [1]
    assert first.doc["label_changes"] == [{"episode_index": 1, "new_label": LABEL}]
    assert len(read_jsonl(os.path.join(rd, "adjudication", "applied.jsonl"))) == 2
    assert _labels(rd) == {"1": {"text": LABEL, "decision_id": 1}}
    # a decisions file from before 2.0 (its top-level relabel_rerun) is refused, not half read
    old = run("adjudicate-apply", "--run-dir", rd, "--decisions",
              _decisions(str(tmp_path / "d2.json"), [(4, "task_verdict", "unsure", "x")],
                         first_id=3, relabel_rerun="full"))
    assert old.rc == 2 and "schema_version 2.0" in old.doc["error"]["message"]
    assert _labels(rd) == {"1": {"text": LABEL, "decision_id": 1}}


def _relabel_and_check(vlm_stage, tmp_path, name: str, episode: int, label: str):
    rd = str(tmp_path / name)
    shutil.copytree(vlm_stage["reference_dir"], rd)
    applied = run("adjudicate-apply", "--run-dir", rd, "--decisions",
                  _decisions(str(tmp_path / f"{name}.json"),
                             [(episode, "task_verdict", "unsure", label)]))
    assert applied.rc == 0 and applied.doc["rerun_task_success"] == [episode]
    with FakeVlmServer() as vlm:
        res = run("check", "--modules", "task_success", "--input", vlm_stage["dataset"],
                  "--run-dir", rd, "--episodes", str(episode), "--vlm-endpoint", vlm.url,
                  "--vlm-model", "fake-vlm")
    assert res.rc == 0, res.doc
    return latest_results(rd, "task_success")[episode], vlm


def test_a_relabel_is_judged_in_one_request_with_its_new_text(vlm_stage, tmp_path):
    """D71: one request, no review per camera and no arbitration; the record names the new text."""
    for episode in (1, 3):
        rec, vlm = _relabel_and_check(vlm_stage, tmp_path, f"relabel-{episode}", episode, LABEL)
        texts = [c["text"] for c in vlm.calls]
        assert sum("Assess the robot manipulation task" in t for t in texts) == 1, episode
        assert any(LABEL in t for t in texts)
        assert not any(REVIEW in t for t in texts)
        assert not any(p in t for t in texts for p in ARBITRATION)
        assert rec["details"]["protocol"] == "video-task/2"
        assert (rec["details"]["task_desc"], rec["details"]["task_desc_source"]) == (LABEL, "人工改标")
        assert "relabel_rerun" not in rec["details"]


def test_a_relabel_with_the_episodes_own_text_gives_the_first_runs_judgement(vlm_stage, tmp_path):
    """The judgement is a function of the text and the video: relabelled with its own annotation, episode 1
    gets the first run's record back (bookkeeping aside)."""
    rec, _ = _relabel_and_check(vlm_stage, tmp_path, "same", 1, ORIGINAL)
    strip = lambda r: {k: v for k, v in r["details"].items() if k not in BOOKKEEPING}  # noqa: E731
    assert verdict_of(rec) == "fail"
    assert strip(rec) == strip(vlm_stage["reference"][1])
