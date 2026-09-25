"""The v2 command chain against its own golden baseline (plan A, 2026-09-24).

Design 13 switched the judgement protocol to continuous video, so the frozen v1
(image probes) can no longer answer v2's requests and the v1-vs-v2 comparison is
retired. The safety net is now self-referential: ``run-v2 --fake-vlm`` records a
golden dump **and** a tape of every model call; a second ``run-v2 --replay`` of
that tape must reproduce the golden exactly (``compare --all-strict``) — the
model is pinned, so any difference is the code's. The sessions's golden recording
(``v2_golden``) is shared by the tests below. Deselect with ``-m "not e2e"``.

The adjudication (D39) is golden-based the same way: ``run-v2 --from ... --decisions
--fake-vlm`` records the adjudication golden; replaying its tape with the same
decisions must find the re-judged records, the skill assignments, the final lists
and the call graph identical. A ``relabel_rerun=full`` adjudication sends requests
the v1-rerun-mode golden never recorded, which fails the replay — on purpose.
"""
from __future__ import annotations

import json
import os

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
def v2_golden(mini_dataset, tmp_path_factory):
    """``run-v2 --fake-vlm``: the golden dump and the tape of every model call."""
    out, proc, doc = run_v2(tmp_path_factory.mktemp("golden"), "golden", mini_dataset,
                            "--fake-vlm")
    assert proc.returncode == 0, proc.stderr[-4000:]
    return out, proc, doc


def test_golden_walks_the_whole_funnel(v2_golden):
    out, proc, doc = v2_golden
    assert [s["step"] for s in doc["steps"]] == STEPS
    assert all(s["exit_code"] == 0 for s in doc["steps"])
    assert doc["steps"][-1]["output"]["complete_marker"] is True
    assert doc["tape"]["mode"] == "record"


def test_v2_replays_its_own_tape_exactly(v2_golden, mini_dataset, tmp_path_factory):
    golden = v2_golden[0]
    out, proc, doc = run_v2(tmp_path_factory.mktemp("v2"), "v2", mini_dataset,
                            "--replay", os.path.join(golden, "vlm_tape.jsonl.gz"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert [s["step"] for s in doc["steps"]] == STEPS
    assert all(s["exit_code"] == 0 for s in doc["steps"])
    hooks = doc["tape"]["hooks"]
    assert hooks["misses"] == 0 and hooks["unused"] == 0
    rc, report = compare(golden, out)
    assert rc == 0, json.dumps(report, ensure_ascii=False)[:3000]
    assert report["conclusion"] == "pass"
    assert {m: r["status"] for m, r in report["modules"].items()} == {
        m: "pass" for m in report["modules"]}
    assert set(report["modules"]) >= {"timestamp_check", "kinematic_limits", "motion_quality",
                                      "visual_quality", "video_action_sync", "task_success",
                                      "dedup", "autolabel", "skill_profile"}
    assert report["replay"]["misses"] == 0


def test_two_live_runs_agree(v2_golden, mini_dataset, tmp_path):
    """The fake model is deterministic: a second live run equals the golden."""
    out, proc, doc = run_v2(tmp_path, "v2-live", mini_dataset, "--fake-vlm")
    assert proc.returncode == 0, proc.stderr[-4000:]
    rc, report = compare(v2_golden[0], out)
    assert rc == 0, json.dumps(report, ensure_ascii=False)[:3000]


def kept_task_text_v2(golden: str, dataset: str) -> str:
    """kept_task_text for a ``run-v2`` dump (autolabel/captions.jsonl, revisions/r0001)."""
    import collections

    with open(os.path.join(dataset, "meta", "episodes.jsonl"), encoding="utf-8") as fh:
        texts = {row["episode_index"]: (row.get("tasks") or [""])[0]
                 for row in map(json.loads, fh)}
    with open(os.path.join(golden, "autolabel", "captions.jsonl"), encoding="utf-8") as fh:
        texts.update({row["episode_index"]: row["caption"] for row in map(json.loads, fh)
                      if row.get("caption")})
    with open(os.path.join(golden, "revisions", "r0001", "keep.txt"), encoding="utf-8") as fh:
        kept = [int(x) for x in fh.read().split()]
    shared = collections.Counter(texts.values())
    return min((shared[texts[e]], texts[e]) for e in kept if texts.get(e))[1]


def test_a_request_missing_from_the_tape_fails_the_comparison(v2_golden, mini_dataset, tmp_path):
    # the primary judgements of a task only episodes the golden kept have (picked by text:
    # the request hash covers video bytes, which differ between platforms). In video mode a
    # missing per-camera review only abstains — the primary verdict stands — so the request
    # whose loss must cascade the kept set is the probe.
    golden = v2_golden[0]
    task = kept_task_text_v2(golden, mini_dataset)

    def edit(entries):
        gone = of_task(entries, "probe", task)
        assert gone
        return [x for x in entries if not any(x is e for e in gone)]

    tape = rewrite_tape(os.path.join(golden, "vlm_tape.jsonl.gz"),
                        str(tmp_path / "short.jsonl.gz"), edit)
    out, proc, doc = run_v2(tmp_path, "v2-short", mini_dataset, "--replay", tape)
    # the missing answer is an execution error of that episode (held back), so the kept set
    # and the final lists change; in video mode the chain itself still completes (skill
    # profile reads every episode's caption) — the divergence is caught by the comparison.
    assert proc.returncode == 0
    assert doc["tape"]["hooks"]["misses"] >= 1
    rc, report = compare(golden, out)
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


def adjudicate_v2(tmp, name: str, dataset: str, base: str, decisions: str,
                  tape: str | None = None):
    out = str(tmp / name)
    mode = ["--fake-vlm"] if tape is None else ["--replay", tape]
    proc = run_parity("run-v2", "--out", out, "--from", base, "--input", dataset,
                      "--decisions", decisions, *mode)
    with open(os.path.join(out, "parity.json"), encoding="utf-8") as fh:
        return out, proc, json.load(fh)


@pytest.fixture(scope="module")
def v2_adj_golden(v2_golden, mini_dataset, tmp_path_factory):
    """The adjudication golden: the recorded rerun of two relabels on the golden base."""
    out, proc, doc = adjudicate_v2(tmp_path_factory.mktemp("adj"), "adj-golden", mini_dataset,
                                   v2_golden[0], _decisions(str(tmp_path_factory.mktemp("adj")
                                                                        / "d.json")))
    assert proc.returncode == 0, proc.stderr[-4000:]
    return out, proc, doc


def test_adjudication_replays_its_golden_exactly(v2_adj_golden, mini_dataset, tmp_path):
    golden = v2_adj_golden[0]
    out, proc, doc = adjudicate_v2(tmp_path, "v2-adj", mini_dataset, v2_adj_golden[2]["from"],
                                   _decisions(str(tmp_path / "d.json")),
                                   os.path.join(golden, "vlm_tape.jsonl.gz"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert [s["step"] for s in doc["steps"]] == [
        "adjudicate-apply", "check task_success", "aggregate funnel", "check skill_profile",
        "aggregate final", "report"]
    assert doc["tape"]["hooks"]["misses"] == 0 and doc["tape"]["hooks"]["unused"] == 0
    rc, report = compare(golden, out)
    assert rc == 0, json.dumps(report, ensure_ascii=False)[:3000]
    ts = report["modules"]["task_success"]
    assert ts["status"] == "pass" and ts["judged_again"] == [0, 1] and ts["compared"] == 2
    assert report["modules"]["skill_profile"]["status"] == "pass"
    assert report["final"]["status"] == "pass" and report["replay"]["status"] == "pass"


def test_full_rerun_leaves_the_golden_tape(v2_adj_golden, mini_dataset, tmp_path):
    """relabel_rerun "full" runs the first run's flow: requests the golden never recorded."""
    out, proc, doc = adjudicate_v2(tmp_path, "v2-full", mini_dataset, v2_adj_golden[2]["from"],
                                   _decisions(str(tmp_path / "d.json"), "full"),
                                   os.path.join(v2_adj_golden[0], "vlm_tape.jsonl.gz"))
    assert doc["tape"]["hooks"]["misses"] >= 1
    rc, report = compare(v2_adj_golden[0], out)
    assert rc == 1 and report["replay"]["status"] == "fail"
    # the full flow's rerun requests are not on the golden (v1-rerun-mode) tape, so both
    # episodes error on the candidate side and are excluded: nothing is comparable, which
    # is exactly the signal that "full" leaves the golden's protocol.
    ts = report["modules"]["task_success"]
    assert ts["judged_again"] == [] and sorted(ts["excluded_errors"]) == [0, 1]


def test_a_golden_with_the_data_integrity_gate_replays_exactly(v2_golden, mini_dataset, tmp_path_factory):
    """design doc 14: v2's own first gate recorded into a golden of its own (``--modules``) and replayed;
    on the clean fixture it changes no list: passed / reject / held equal the default golden's."""
    modules = "data_integrity,timestamp_check,kinematic_limits,motion_quality,visual_quality," \
              "video_action_sync,task_success,dedup,skill_profile"
    tmp = tmp_path_factory.mktemp("integrity")
    golden, proc, doc = run_v2(tmp, "golden", mini_dataset, "--fake-vlm", "--modules", modules)
    assert proc.returncode == 0, proc.stderr[-4000:]
    steps = [s["step"] for s in doc["steps"]]
    assert steps == STEPS[:4] + ["check integrity"] + STEPS[4:]
    out, proc, doc = run_v2(tmp, "v2", mini_dataset, "--replay", os.path.join(golden, "vlm_tape.jsonl.gz"),
                            "--modules", modules)
    assert proc.returncode == 0, proc.stderr[-4000:]
    rc, report = compare(golden, out)
    assert rc == 0 and report["conclusion"] == "pass", json.dumps(report, ensure_ascii=False)[:3000]
    res = run_parity("compare", "--golden", golden, "--candidate", out, "--strict", "data_integrity",
                     "--verdict-only", "task_success", "--json")
    integ = json.loads(res.stdout)["modules"]["data_integrity"]
    assert res.returncode == 0 and integ["status"] == "pass" and integ["mode"] == "strict", integ
    for name in ("passed", "reject", "held"):
        with open(os.path.join(golden, "revisions", "r0001", f"{name}.json"), encoding="utf-8") as fh:
            mine = json.load(fh)["episodes"]
        with open(os.path.join(v2_golden[0], "revisions", "r0001", f"{name}.json"), encoding="utf-8") as fh:
            theirs = json.load(fh)["episodes"]
        assert mine == theirs, name
