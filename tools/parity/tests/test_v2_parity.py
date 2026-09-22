"""The v2 command chain against v1 on the synthetic dataset (W3 acceptance, doc 11 §3).

``run-v2`` runs preflight -> ... -> verify in the Daemon's order, each command in
the same process as the tape hooks; ``compare --all-strict`` must then find
every module's records, the final lists and the VLM call graph identical: every
request v2 sends is on v1's tape (no miss) and every recorded request is sent
(nothing unused). The sessions's v1 recording (``v1_golden``) is shared with
``test_dump_v1_e2e``. Deselect with ``-m "not e2e"``.

The adjudication (D39): v1's ``rejudge`` applies two relabels to its delivery
(``dump-v1 -- rejudge``, recorded on the fake model); v2's adjudication sequence
(``run-v2 --from ... --decisions``) applies the same decisions to its run replaying
that tape, and ``compare --all-strict`` must find the re-judged records, the skill
assignments, the final lists and the call graph identical.
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

from .conftest import run_parity
from .test_dump_v1_e2e import kept_task_text, of_task, rewrite_tape

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


@pytest.fixture(scope="module")
def v2_replayed(v1_golden, mini_dataset, tmp_path_factory):
    """``run-v2 --replay`` of the golden tape: (run directory, process, parity.json)."""
    return run_v2(tmp_path_factory.mktemp("v2"), "v2", mini_dataset,
                  "--replay", os.path.join(v1_golden, "vlm_tape.jsonl.gz"))


def test_v2_replays_v1s_tape_exactly(v1_golden, v2_replayed):
    out, proc, doc = v2_replayed
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
    # the end-state votes on a task only episodes v1 kept have (picked by text: the
    # request hash covers image bytes, which differ between platforms)
    task = kept_task_text(v1_golden, mini_dataset)

    def edit(entries):
        gone = of_task(entries, "endstate", task)
        assert gone
        return [x for x in entries if not any(x is e for e in gone)]

    tape = rewrite_tape(os.path.join(v1_golden, "vlm_tape.jsonl.gz"),
                        str(tmp_path / "short.jsonl.gz"), edit)
    out, proc, doc = run_v2(tmp_path, "v2-short", mini_dataset, "--replay", tape)
    # the missing answers are execution errors of those episodes (held back), so the
    # kept set changes and the taxonomy prompt with it: not on the tape either, and
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


# ---------------------------------------------------------------- adjudication (D39)

RELABELS = {1: "stack the cups", 0: "wipe the table"}


def _decisions(path: str, relabel_rerun: str | None = None) -> str:
    doc = {"schema_version": "1.0", "decisions": [
        {"id": i, "episode_index": ep, "line": "label", "decision": "custom_label",
         "new_label": label, "note": None, "decided_by": "alice", "decided_at": 1790000000000 + i}
        for i, (ep, label) in enumerate(RELABELS.items(), start=1)]}
    if relabel_rerun is not None:
        doc["relabel_rerun"] = relabel_rerun
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


@pytest.fixture(scope="module")
def v1_rejudge(v1_golden, v1_src, mini_dataset, tmp_path_factory):
    """v1's ``rejudge`` of the golden delivery with the two relabels (a dump)."""
    tmp = tmp_path_factory.mktemp("rejudge")
    delivery = str(tmp / "delivery")
    shutil.copytree(os.path.join(os.path.dirname(v1_golden), "rec1-delivery"), delivery)
    os.makedirs(os.path.join(delivery, "human-decisions"))
    with open(os.path.join(delivery, "human-decisions", "label_decisions.csv"), "w",
              encoding="utf-8") as fh:
        fh.write("episode_id,decision,new_label,note,at\n")
        for i, (ep, label) in enumerate(RELABELS.items()):
            fh.write(f"ep{ep:06d},采纳建议改标,{label},,2026-09-22 01:00:0{i}\n")
    config = str(tmp / "fake.yaml")
    with open(config, "w", encoding="utf-8") as fh:        # what run's --vlm-* flags set
        fh.write("checks:\n  task_success:\n    vlm:\n"
                 "      endpoint: http://fake-vlm.local/v1\n      model: fake-vlm\n")
    out = str(tmp / "rej")
    proc = run_parity("dump-v1", "--out", out, "--v1-src", v1_src, "--fake-vlm", "--",
                      "rejudge", "--delivery", delivery, "--input", mini_dataset,
                      "--config", config)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return out


def adjudicate_v2(tmp, name: str, dataset: str, base: str, decisions: str, tape: str):
    out = str(tmp / name)
    proc = run_parity("run-v2", "--out", out, "--from", base, "--input", dataset,
                      "--decisions", decisions, "--replay", tape)
    with open(os.path.join(out, "parity.json"), encoding="utf-8") as fh:
        return out, proc, json.load(fh)


def test_v1s_rejudge_dump_holds_what_it_judged_again(v1_rejudge):
    with open(os.path.join(v1_rejudge, "dump.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    assert meta["status"] == "clean" and meta["command"] == "rejudge", meta["problems"]
    adj = meta["adjudication"]
    assert adj["relabels"] == {str(e): t for e, t in RELABELS.items()}
    assert adj["rejudged"] == [0, 1] and adj["rerun_failed"] == []
    tags = {row["tag"] for row in meta["tape"]["summary"]["by_kind_tag_outcome"]}
    assert {"probe", "endstate"} <= tags and "arbitration" not in tags      # v1's two layers


def test_v2s_adjudication_replays_v1s_rejudge_exactly(v1_rejudge, v2_replayed, mini_dataset,
                                                      tmp_path):
    base = v2_replayed[0]
    out, proc, doc = adjudicate_v2(tmp_path, "v2-adj", mini_dataset, base,
                                   _decisions(str(tmp_path / "d.json")),   # v1 by default
                                   os.path.join(v1_rejudge, "vlm_tape.jsonl.gz"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert [s["step"] for s in doc["steps"]] == [
        "adjudicate-apply", "check task_success", "aggregate funnel", "check skill_profile",
        "aggregate final", "report"]
    assert doc["tape"]["hooks"]["misses"] == 0 and doc["tape"]["hooks"]["unused"] == 0
    rc, report = compare(v1_rejudge, out)
    assert rc == 0, json.dumps(report, ensure_ascii=False)[:3000]
    ts = report["modules"]["task_success"]
    assert ts["status"] == "pass" and ts["judged_again"] == [0, 1] and ts["compared"] == 2
    assert report["modules"]["skill_profile"]["status"] == "pass"
    assert report["final"]["status"] == "pass" and report["replay"]["status"] == "pass"


def test_the_full_protocol_is_not_v1s_rejudge(v1_rejudge, v2_replayed, mini_dataset, tmp_path):
    """relabel_rerun "full" runs the first run's flow: requests v1's rejudge never sent."""
    out, proc, doc = adjudicate_v2(tmp_path, "v2-full", mini_dataset, v2_replayed[0],
                                   _decisions(str(tmp_path / "d.json"), "full"),
                                   os.path.join(v1_rejudge, "vlm_tape.jsonl.gz"))
    assert doc["tape"]["hooks"]["misses"] >= 1
    rc, report = compare(v1_rejudge, out)
    assert rc == 1 and report["replay"]["status"] == "fail"
    judged = report["modules"]["task_success"]["judged_with"]
    assert {j["candidate"]["relabel_rerun"] for j in judged} == {"full"}
