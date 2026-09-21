"""The v2 command chain against v1 on the synthetic dataset (W3 acceptance, doc 11 §3).

``run-v2`` runs preflight -> ... -> verify in the Daemon's order, each command in
the same process as the tape hooks; ``compare --all-strict`` must then find
every module's records, the final lists and the VLM call graph identical: every
request v2 sends is on v1's tape (no miss) and every recorded request is sent
(nothing unused). The sessions's v1 recording (``v1_golden``) is shared with
``test_dump_v1_e2e``. Deselect with ``-m "not e2e"``.
"""
from __future__ import annotations

import json
import os

import pytest

from .conftest import run_parity
from .test_dump_v1_e2e import rewrite_tape

pytestmark = pytest.mark.e2e

STEPS = ["preflight", "plan", "snapshot", "autolabel", "check numeric", "check frame",
         "check vlm", "aggregate funnel", "check dedup", "check skill_profile",
         "aggregate final", "report", "export", "verify"]


def run_v2(tmp, name: str, dataset: str, *mode: str):
    out = str(tmp / name)
    proc = run_parity("run-v2", "--out", out, "--input", dataset,
                      "--delivery", str(tmp / f"{name}-delivery"), *mode)
    with open(os.path.join(out, "parity.json"), encoding="utf-8") as fh:
        return out, proc, json.load(fh)


def compare(golden: str, candidate: str) -> tuple[int, dict]:
    res = run_parity("compare", "--golden", golden, "--candidate", candidate, "--all-strict",
                     "--json")
    return res.returncode, json.loads(res.stdout)


def test_v2_replays_v1s_tape_exactly(v1_golden, mini_dataset, tmp_path):
    out, proc, doc = run_v2(tmp_path, "v2", mini_dataset,
                            "--replay", os.path.join(v1_golden, "vlm_tape.jsonl.gz"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert [s["step"] for s in doc["steps"]] == STEPS
    assert all(s["exit_code"] == 0 for s in doc["steps"])
    hooks = doc["tape"]["hooks"]
    assert hooks["misses"] == 0 and hooks["unused"] == 0
    rc, report = compare(v1_golden, out)
    assert rc == 0, json.dumps(report, ensure_ascii=False)[:3000]
    assert report["conclusion"] == "pass"
    assert {m: r["status"] for m, r in report["modules"].items()} == {
        m: "pass" for m in report["modules"]}
    assert set(report["modules"]) >= {"timestamp_check", "kinematic_limits", "motion_quality",
                                      "visual_quality", "video_action_sync", "task_success",
                                      "dedup", "autolabel", "skill_profile"}
    assert report["replay"]["misses"] == 0


def test_v2_live_on_the_fake_model_agrees(v1_golden, mini_dataset, tmp_path):
    out, proc, doc = run_v2(tmp_path, "v2-live", mini_dataset, "--fake-vlm")
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert doc["steps"][-1]["output"]["complete_marker"] is True
    rc, report = compare(v1_golden, out)
    assert rc == 0, json.dumps(report, ensure_ascii=False)[:3000]


def test_a_request_v1_never_sent_fails_the_comparison(v1_golden, mini_dataset, tmp_path):
    def edit(entries):
        e = min((e for e in entries if e.get("tag") == "endstate"), key=lambda e: e["hash"])
        return [x for x in entries if x is not e]

    tape = rewrite_tape(os.path.join(v1_golden, "vlm_tape.jsonl.gz"),
                        str(tmp_path / "short.jsonl.gz"), edit)
    out, proc, doc = run_v2(tmp_path, "v2-short", mini_dataset, "--replay", tape)
    # the missing answer is an execution error of that episode (held back), so the kept
    # set changes and the taxonomy prompt with it: not on the tape either, and
    # skill_profile fails as a module (exit 4), which stops the chain
    assert proc.returncode == 1
    last = doc["steps"][-1]
    assert last["step"] == "check skill_profile" and last["exit_code"] == 4
    assert last["output"]["error"]["code"] == "module_failed"
    assert doc["tape"]["hooks"]["misses"] >= 1
    rc, report = compare(v1_golden, out)
    assert rc == 1 and report["conclusion"] == "fail"
    assert report["replay"]["status"] == "fail" and report["replay"]["misses"] >= 1
    assert report["modules"]["task_success"]["status"] == "fail"
