"""aggregate and adjudicate-apply on hand-made run directories (W3, doc 02 §3.6 / §3.9, 17 §4).

Each test writes the module records it needs (records 2.0, the ``check`` record format), runs
the commands in process - every ``--json`` checked against its C2 schema - and reads the files
they leave. The rules under test are the policy verdicts of design doc 17 (D58) under the default
policy, which is v1's gates (``pipeline/verdict.py``, ``rejudge``) without the soft-score rejects
(P18), plus D24 / D33 / D35: a blocking finding drops an episode, else an execution error holds it.
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict

import pytest

from curation.contracts import schemas
from curation.pipeline.records import record_v2

from .pipeline import read_jsonl, run

ALL = ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
       "video_action_sync", "task_success", "dedup")
SCORED = ("motion_quality", "visual_quality")          # a score reading, never a reject (P18)
TASK = "pick up the red block and place it in the bin"


def record(module: str, ep: int, verdict: str, *, score=None, details=None,
           incidents=None) -> dict:
    """A record 2.0 of ``module`` that a 1.0 reader would call ``verdict``: written from the same
    answer the module gives (``records.record_v2``, the derivation ``check`` uses) - pass finds
    nothing, fail reports the module's blocking finding, abstain its review finding (or only what it
    could not assess), scored a score reading, error an execution error."""
    passed = {"pass": True, "fail": False}.get(verdict)
    if verdict == "scored" and score is None:
        score = 0.95
    error = None
    if verdict == "error":
        error = {"kind": "execution",
                 "incidents": incidents or [{"step": "probe", "call_kind": "probe",
                                             "cause": "timeout", "attempts": 1}]}
    if module == "task_success" and details is None:
        details = {"verdict": {"pass": "success", "fail": "failure"}.get(verdict,
                                                                          "review_conflict"),
                   "task_desc": TASK, "task_desc_source": "原始标注"}
        if verdict == "abstain":
            details["reason"] = "两层证据矛盾,进人工"
    return record_v2(module, ep, passed, score, details or {}, error=error, evidence=[],
                     elapsed_s=0.1)


def modules_of(refs: list[dict]) -> list[str]:
    return [r["module"] for r in refs]


class RunDir:
    """A run directory with module records, written as part files."""

    def __init__(self, path: str):
        self.path = path
        self.parts: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    def put(self, module: str, ep: int, verdict: str, part: str = "0001", **kw) -> RunDir:
        self.parts[module][part].append(record(module, ep, verdict, **kw))
        return self

    def good(self, *eps: int) -> RunDir:
        for ep in eps:
            for m in ALL:
                self.put(m, ep, "scored" if m in SCORED else "pass")
        return self

    def drop(self, module: str, ep: int, part: str = "0001") -> RunDir:
        for p in self.parts[module].values():
            p[:] = [r for r in p if r["episode_index"] != ep]
        return self

    def replace(self, module: str, ep: int, verdict: str, **kw) -> RunDir:
        return self.drop(module, ep).put(module, ep, verdict, **kw)

    def write(self) -> str:
        for module, parts in self.parts.items():
            d = os.path.join(self.path, "checks", module, "parts")
            os.makedirs(d, exist_ok=True)
            for part, recs in parts.items():
                with open(os.path.join(d, f"{part}.jsonl"), "w", encoding="utf-8") as fh:
                    fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
        return self.path


def funnel(run_dir: str, episodes: str) -> dict[int, dict]:
    res = run("aggregate", "--run-dir", run_dir, "--phase", "funnel", "--revision", "1",
              "--modules", ",".join(ALL), "--episodes", episodes)
    assert res.rc == 0, res.doc
    lines = read_jsonl(os.path.join(run_dir, "revisions", "r0001", "verdicts.jsonl"))
    for ln in lines:
        assert schemas.errors("cli/verdict-line.schema.json", ln) == []
    return {ln["episode_index"]: ln for ln in lines}


def final(run_dir: str, episodes: str, revision: int = 1) -> dict[str, dict[int, dict]]:
    res = run("aggregate", "--run-dir", run_dir, "--phase", "final", "--revision",
              str(revision), "--modules", ",".join(ALL), "--episodes", episodes)
    assert res.rc == 0, res.doc
    out = {}
    rev = os.path.join(run_dir, "revisions", f"r{revision:04d}")
    for name in ("passed", "reject", "held", "review"):
        with open(os.path.join(rev, f"{name}.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        assert schemas.errors("cli/final-list.schema.json", doc) == [], name
        out[name] = {e["episode_index"]: e for e in doc["episodes"]}
    assert res.doc["counts"]["passed"] == len(out["passed"])
    return out


def decisions(path: str, *items) -> str:
    doc = {"schema_version": "1.0", "decisions": [
        {"id": i, "episode_index": ep, "line": line, "decision": decision,
         "new_label": new_label, "note": None, "decided_by": "alice",
         "decided_at": 1790000000000 + i}
        for i, (ep, line, decision, new_label) in enumerate(items, start=1)]}
    assert schemas.errors("cli/decisions.schema.json", doc) == []
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def apply(run_dir: str, path: str) -> dict:
    res = run("adjudicate-apply", "--run-dir", run_dir, "--decisions", path)
    assert res.rc == 0, res.doc
    return res.doc


# ---------------------------------------------------------------- funnel


def test_funnel_gates_errors_and_d35(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0, 6)
    # 1: a finding blocks; every other module judged it too (D57)
    rd.good(1).replace("timestamp_check", 1, "fail", details={"reason": "丢帧"})
    # 2: a frame module's finding blocks while the other frame module failed to run
    rd.good(2).replace("visual_quality", 2, "error").replace("video_action_sync", 2, "fail")
    # 3: task_success failed to run; low scores are readings and reject nothing (P18): held back
    rd.good(3).replace("task_success", 3, "error")
    rd.replace("motion_quality", 3, "scored", score=0.1)
    rd.replace("visual_quality", 3, "scored", score=0.1)
    # 4: task_success failed to run, nothing rejects it: held back
    rd.good(4).replace("task_success", 4, "error")
    # 5: task_success never ran for it: held back as well
    rd.good(5).drop("task_success", 5)
    # 6: the model could not tell - a review finding, kept and asked
    rd.replace("task_success", 6, "abstain")
    # 7: a module failed to run and three never judged it: held, every one of them named (D57, D58)
    rd.good(7).replace("motion_quality", 7, "error")
    for m in ("visual_quality", "video_action_sync", "task_success"):
        rd.drop(m, 7)
    # 8: a blocking finding drops it wherever it is, a module that failed is named (D35, D58)
    rd.good(8).replace("motion_quality", 8, "error").replace("task_success", 8, "fail")
    lines = funnel(rd.write(), "0-8")

    assert lines[0]["verdict"] == "keep" and lines[0]["error_modules"] == []
    assert lines[0]["blocking"] == [] and lines[0]["review"] == []
    assert lines[1]["verdict"] == "drop" and lines[1]["blocking"] == [
        {"module": "timestamp_check", "code": "gap", "item": "STRM-3"}]
    assert lines[1]["error_modules"] == []
    assert lines[2]["verdict"] == "drop" and modules_of(lines[2]["blocking"]) == ["video_action_sync"]
    assert lines[2]["error_modules"] == ["visual_quality"]
    assert "执行出错，不影响结论" in lines[2]["reason"]
    assert lines[3]["verdict"] == "held" and lines[3]["error_modules"] == ["task_success"]
    assert lines[3]["blocking"] == []
    for ep in (4, 5):
        assert lines[ep]["verdict"] == "held" and lines[ep]["error_modules"] == ["task_success"]
        assert lines[ep]["blocking"] == [] and lines[ep]["reason"].startswith("待补跑")
    assert "没有结果" in lines[5]["reason"]
    assert lines[6]["verdict"] == "keep" and lines[6]["review"] == [
        {"module": "task_success", "code": "uncertain", "item": "TASK-5", "line": "task_verdict"}]
    assert lines[7]["verdict"] == "held" and lines[7]["error_modules"] == [
        "motion_quality", "visual_quality", "video_action_sync", "task_success"]
    assert lines[8]["verdict"] == "drop" and modules_of(lines[8]["blocking"]) == ["task_success"]
    assert lines[8]["error_modules"] == ["motion_quality"]
    with open(os.path.join(rd.path, "revisions", "r0001", "keep.txt"), encoding="utf-8") as fh:
        assert fh.read().split() == ["0", "6"]


def test_funnel_without_revision_goes_to_the_funnel_directory(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0, 1).write()
    res = run("aggregate", "--run-dir", rd, "--phase", "funnel", "--modules", ",".join(ALL),
              "--episodes", "0-1")
    assert res.rc == 0 and res.doc["files"]["keep"] == "funnel/keep.txt"
    assert not os.path.exists(os.path.join(rd, "revisions"))


def test_the_modules_come_from_the_plan(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0).write()
    plan = {"stages": [{"id": "numeric", "modules": ["timestamp_check"]}]}
    with open(os.path.join(rd, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh)
    res = run("aggregate", "--run-dir", rd, "--phase", "funnel", "--episodes", "0")
    assert res.rc == 0 and res.doc["counts"] == {"total": 1, "keep": 1, "drop": 0, "held": 0,
                                              "decided_in": 0, "decided_out": 0,
                                              "skipped": 0}


# ---------------------------------------------------------------- final


def test_final_lists_are_disjoint_and_complete(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0, 1, 2, 3, 5)
    rd.replace("dedup", 1, "fail", details={"duplicate_of": 0})
    rd.drop("dedup", 2)                                        # dedup never saw it
    rd.replace("visual_quality", 3, "error")
    rd.put("timestamp_check", 4, "fail")                       # the funnel rejects it
    rd.replace("task_success", 5, "error")                     # the funnel holds it
    lists = final(rd.write(), "0-5")

    assert sorted(lists["passed"]) == [0]
    assert sorted(lists["reject"]) == [1, 4]
    assert sorted(lists["held"]) == [2, 3, 5]
    dup = lists["reject"][1]["reasons"]
    assert dup == [{"module": "dedup", "kind": "duplicate", "code": "duplicate", "item": "SET-1",
                    "appealable": True, "text": "与 ep000000 字节级完全重复", "duplicate_of": 0}]
    gap = lists["reject"][4]["reasons"][0]
    assert (gap["kind"], gap["code"], gap["item"], gap["appealable"]) == ("finding", "gap", "STRM-3", False)
    assert [r["module"] for r in lists["held"][2]["reasons"]] == ["dedup"]
    assert [r["module"] for r in lists["held"][3]["reasons"]] == ["visual_quality"]
    assert lists["held"][5]["reasons"][0] == {
        "module": "task_success", "kind": "execution_error",
        "text": "「任务成败判定」执行出错（probe/probe: timeout）"}
    assert all(r["kind"] == "execution_error" for e in lists["held"].values()
               for r in e["reasons"])


def test_human_decisions_follow_v1s_priorities(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0, 1, 2, 3, 4, 5, 6, 7)
    rd.replace("task_success", 0, "abstain")
    rd.replace("task_success", 1, "abstain")
    rd.replace("task_success", 2, "fail")
    rd.drop("timestamp_check", 3).put("timestamp_check", 3, "fail")
    rd.replace("task_success", 4, "error")
    rd.replace("task_success", 5, "abstain")
    run_dir = rd.write()
    before = final(run_dir, "0-7")
    assert sorted(before["passed"]) == [0, 1, 5, 6, 7]
    assert sorted(before["reject"]) == [2, 3] and sorted(before["held"]) == [4]
    assert {e: [i["kind"] for i in v["review"]] for e, v in before["review"].items()} == \
        {0: ["task_verdict"], 1: ["task_verdict"], 2: ["reject_appeal"], 5: ["task_verdict"]}

    path = decisions(str(tmp_path / "decisions.json"),
                     (0, "task_verdict", "failure", None),      # a person rejects ...
                     (1, "task_verdict", "success", None),      # ... or passes an abstention
                     (2, "reject_appeal", "restore", None),     # appeal of a task reject
                     (4, "label", "discard", None),             # discard beats held
                     (5, "task_verdict", "unsure", None),       # changes nothing
                     (6, "label", "adopt_suggestion", "stack the cups"),
                     (7, "label", "custom_label", "wipe the table"),
                     (7, "task_verdict", "success", None))
    out = apply(run_dir, path)
    assert out["applied"] == 8 and out["skipped_already_applied"] == 0
    assert out["rerun_task_success"] == [6]          # 7 has a human verdict: not re-judged
    assert out["label_changes"] == [{"episode_index": 6, "new_label": "stack the cups"},
                                    {"episode_index": 7, "new_label": "wipe the table"}]
    again = apply(run_dir, path)                          # idempotent
    assert again["applied"] == 0 and again["skipped_already_applied"] == 8
    assert len(read_jsonl(os.path.join(run_dir, "adjudication", "applied.jsonl"))) == 8

    after = final(run_dir, "0-7", revision=2)
    assert sorted(after["passed"]) == [1, 2, 5, 7]
    assert sorted(after["reject"]) == [0, 3, 4]
    assert sorted(after["held"]) == [6]                   # relabelled, not judged again yet
    assert after["reject"][0]["reasons"][0]["text"].startswith("人工裁决判失败")
    assert after["reject"][0]["reasons"][0]["kind"] == "human"
    assert after["reject"][3]["reasons"][0]["kind"] == "finding"     # gates are final
    assert after["reject"][4]["reasons"] == [{"module": "task_success", "kind": "human",
                                              "text": "人工裁决弃用"}]
    assert after["held"][6]["reasons"][0]["text"] == "改标后尚未按新标注重跑任务成败判定"
    assert {e: [i["kind"] for i in v["review"]] for e, v in after["review"].items()} == \
        {5: ["task_verdict"]}                              # "unsure": still in the queue
    with open(os.path.join(run_dir, "revisions", "r0002", "adjudications.json")) as fh:
        assert json.load(fh) == {"applied": list(range(1, 9))}

    # task_success judged 6 again with the new label: it passes
    rd.put("task_success", 6, "pass", part="0002",
           details={"verdict": "success", "task_desc": "stack the cups",
                    "task_desc_source": "人工改标"})
    rd.write()
    third = final(run_dir, "0-7", revision=3)
    assert 6 in third["passed"]
    assert third["passed"][6]["task_text"] == {"text": "stack the cups", "source": "人工改标"}


def _keep_txt(run_dir: str, revision: int) -> list[int]:
    with open(os.path.join(run_dir, "revisions", f"r{revision:04d}", "keep.txt"),
              encoding="utf-8") as fh:
        return [int(x) for x in fh.read().split()]


def _funnel_phase(run_dir: str, revision: int, episodes: str) -> dict:
    res = run("aggregate", "--run-dir", run_dir, "--phase", "funnel", "--revision",
              str(revision), "--modules", ",".join(ALL), "--episodes", episodes)
    assert res.rc == 0, res.doc
    return res.doc["counts"]


def test_a_restored_appeal_is_never_held_for_dedup_or_profile(tmp_path):
    """The run order the Daemon uses: ep 1 was rejected by task_success alone, so it
    was not in keep.txt and dedup never saw it. A person
    restores it: keep.txt of the next revision takes it in, so the incremental
    profile files it, and dedup - not run again - is not asked about it."""
    rd = RunDir(str(tmp_path / "run")).good(0, 2)
    for m in ("timestamp_check", "kinematic_limits", "video_action_sync"):
        rd.put(m, 1, "pass")
    for m in ("motion_quality", "visual_quality"):
        rd.put(m, 1, "scored")
    rd.put("task_success", 1, "fail")
    run_dir = rd.write()
    assert _funnel_phase(run_dir, 1, "0-2")["keep"] == 2
    assert _keep_txt(run_dir, 1) == [0, 2]
    first = final(run_dir, "0-2")
    assert sorted(first["reject"]) == [1] and first["reject"][1]["reasons"][0]["kind"] \
        == "finding"

    apply(run_dir, decisions(str(tmp_path / "d.json"), (1, "reject_appeal", "restore", None)))
    counts = _funnel_phase(run_dir, 2, "0-2")
    assert (counts["keep"], counts["decided_in"], counts["decided_out"]) == (2, 1, 0)
    assert _keep_txt(run_dir, 2) == [0, 1, 2]
    # what the next revision adds for it
    rd.put("visual_quality", 1, "pass", part="0002")
    rd.write()
    after = final(run_dir, "0-2", revision=2)
    assert sorted(after["passed"]) == [0, 1, 2] and after["held"] == {}
    assert _keep_txt(run_dir, 2) == [0, 1, 2]                 # final writes the same set


def test_dedup_is_not_run_again_after_an_adjudication(tmp_path):
    """Dedup's groups stand; aggregate picks each group's keeper after the human decisions - the
    first member not rejected for another reason (D58, design doc 17 §4.5): 3 judged failed by a
    person, its copy 7 is kept in its place. An episode a person brought in is never deduplicated;
    a kept episode dedup has no result for still waits for it."""
    from curation.pipeline import aggregate as agg
    from curation.pipeline.adjudication import Decisions

    rd = RunDir(str(tmp_path / "run")).good(0, 3, 6, 7)
    rd.replace("task_success", 3, "abstain")
    rd.replace("dedup", 7, "fail", details={"duplicate_of": 3})        # 7 copies 3
    rd.drop("dedup", 6)                                                  # never compared
    for m in ("timestamp_check", "kinematic_limits", "video_action_sync", "visual_quality"):
        rd.put(m, 5, "pass")
    for m in ("motion_quality", "visual_quality"):
        rd.put(m, 5, "scored")
    rd.put("task_success", 5, "fail")
    # as if dedup had been run again with 5 in its input: v1 never asks about it
    rd.put("dedup", 5, "fail", details={"duplicate_of": 0})
    run_dir = rd.write()
    apply(run_dir, decisions(str(tmp_path / "d.json"),
                             (3, "task_verdict", "failure", None),     # 7's original goes
                             (5, "reject_appeal", "restore", None)))
    assert _funnel_phase(run_dir, 1, "0,3,5,6,7") and _keep_txt(run_dir, 1) == [0, 5, 6, 7]
    lists = final(run_dir, "0,3,5,6,7")
    assert sorted(lists["passed"]) == [0, 5, 7]
    assert sorted(lists["reject"]) == [3]
    assert lists["reject"][3]["reasons"] == [{
        "module": "task_success", "kind": "human", "code": "failure", "item": "TASK-5",
        "appealable": False, "text": "人工裁决判失败（任务未完成）"}]
    assert [r["module"] for r in lists["held"][6]["reasons"]] == ["dedup"]

    state = agg.RunState(run_dir, list(ALL), [0, 3, 5, 6, 7])
    members, restored = agg.profile_members(state, Decisions.of(run_dir))
    assert restored == {5}
    assert members == [0, 3, 5, 6, 7]               # 7 keeps its group; 5 counts as none

    # had 3 stayed, 7 would be its copy: rejected, appealable, left out of the profile
    rd2 = RunDir(str(tmp_path / "run2")).good(0, 3, 7)
    rd2.replace("dedup", 7, "fail", details={"duplicate_of": 3})
    run2 = rd2.write()
    lists = final(run2, "0,3,7")
    assert sorted(lists["passed"]) == [0, 3] and sorted(lists["reject"]) == [7]
    assert lists["reject"][7]["reasons"][0]["duplicate_of"] == 3
    state = agg.RunState(run2, list(ALL), [0, 3, 7])
    assert agg.profile_members(state, Decisions.of(run2)) == ([0, 3], set())


def _kinds(lists) -> dict[int, list[tuple[str, str]]]:
    return {e: [(i["kind"], i["source_module"]) for i in v["review"]]
            for e, v in lists["review"].items()}


def _gate_reject(rd: RunDir, ep: int, gate: str) -> RunDir:
    """ep rejected by ``gate`` in the numeric or frame stage (later stages not asked)."""
    return rd.good(ep).drop(gate, ep).put(gate, ep, "fail")


def test_review_kinds_follow_v1s_queues(tmp_path):
    """D42 by finding: a task verdict is asked for delivered episodes only, and only of
    task_success; a reject whose blocking findings are all appealable - task_success's failure,
    dedup's duplicate - can be appealed until an appeal is decided, one with a final finding
    among them cannot; held episodes wait."""
    rd = RunDir(str(tmp_path / "run")).good(0, 1, 2, 3, 6, 7, 8, 9)
    rd.replace("task_success", 0, "abstain")                            # asked
    rd.replace("kinematic_limits", 1, "abstain")                        # not asked
    rd.replace("task_success", 2, "fail")                               # appealable
    rd.replace("task_success", 3, "abstain")
    rd.replace("dedup", 3, "fail", details={"duplicate_of": 0})         # a copy of 0
    _gate_reject(rd, 4, "timestamp_check")                              # final
    rd.good(5).replace("video_action_sync", 5, "fail")                  # final besides an
    rd.replace("task_success", 5, "fail")                               # appealable one
    rd.replace("task_success", 6, "error")                              # held: waits
    for ep in (7, 8, 9):
        rd.replace("task_success", ep, "fail")
    run_dir = rd.write()
    apply(run_dir, decisions(str(tmp_path / "d.json"),
                             (7, "reject_appeal", "unsure", None),      # still listed
                             (8, "reject_appeal", "keep_rejected", None),
                             (9, "label", "discard", None)))
    lists = final(run_dir, "0-9")
    assert sorted(lists["reject"]) == [2, 3, 4, 5, 7, 8, 9] and sorted(lists["held"]) == [6]
    assert _kinds(lists) == {0: [("task_verdict", "task_success")],
                             2: [("reject_appeal", "task_success")],
                             3: [("reject_appeal", "dedup")],
                             7: [("reject_appeal", "task_success")]}
    assert lists["review"][0]["current_list"] == "passed"
    copy = lists["review"][3]
    assert copy["current_list"] == "reject"
    assert copy["reasons"] == [{"module": "dedup", "kind": "duplicate", "code": "duplicate",
                                "item": "SET-1", "appealable": True, "duplicate_of": 0,
                                "text": "与 ep000000 字节级完全重复"}]
    assert copy["review"][0]["reason"] == "与 ep000000 字节级完全重复"
    assert copy["review"][0]["codes"] == ["duplicate"] and copy["review"][0]["duplicate_of"] == 0
    assert [(r["module"], r["appealable"]) for r in lists["reject"][5]["reasons"]] == \
        [("video_action_sync", False), ("task_success", True)]        # every reason, in order
    assert lists["review"][7]["review"][0]["reason"].endswith("（复议拿不准，待定）")


def test_a_restored_dedup_appeal_comes_back_and_its_abstention_is_asked(tmp_path):
    """D42: restore overturns dedup's finding - keep.txt keeps it, the profile files it
    as restored, it is passed, and task_success's abstention on it is now a question."""
    from curation.pipeline import aggregate as agg
    from curation.pipeline.adjudication import Decisions

    rd = RunDir(str(tmp_path / "run")).good(0, 3)
    rd.replace("task_success", 3, "abstain")
    rd.replace("dedup", 3, "fail", details={"duplicate_of": 0})
    run_dir = rd.write()
    assert sorted(final(run_dir, "0,3")["reject"]) == [3]
    out = apply(run_dir, decisions(str(tmp_path / "d.json"), (3, "reject_appeal", "restore",
                                                              None)))
    assert out["profile_resync"] == [3]
    _funnel_phase(run_dir, 2, "0,3")
    assert _keep_txt(run_dir, 2) == [0, 3]
    state = agg.RunState(run_dir, list(ALL), [0, 3])
    assert agg.profile_members(state, Decisions.of(run_dir)) == ([0, 3], {3})
    after = final(run_dir, "0,3", revision=2)
    assert sorted(after["passed"]) == [0, 3]
    assert _kinds(after) == {3: [("task_verdict", "task_success")]}


def test_restore_overturns_the_appealed_module_only(tmp_path):
    """A task_success-only reject that another module could not judge (D35): restoring
    it leaves the other module's gap, so it is held until a retry (P11)."""
    rd = RunDir(str(tmp_path / "run")).good(0, 1)
    rd.drop("visual_quality", 1)                     # the module never judged it
    rd.replace("task_success", 1, "fail")
    run_dir = rd.write()
    first = final(run_dir, "0,1")
    assert [r["kind"] for r in first["reject"][1]["reasons"]] == ["finding", "execution_error"]
    assert _kinds(first) == {1: [("reject_appeal", "task_success")]}
    apply(run_dir, decisions(str(tmp_path / "d.json"), (1, "reject_appeal", "restore", None)))
    after = final(run_dir, "0,1", revision=2)
    assert sorted(after["held"]) == [1]
    assert [r["module"] for r in after["held"][1]["reasons"]] == ["visual_quality"]
    assert _kinds(after) == {}


@pytest.mark.parametrize("episode, why", [
    (4, "a final finding of its own"), (5, "a final finding besides an appealable one"),
    (0, "not rejected"), (9, "discarded"), (3, "a low score rejects nothing (P18)"),
])
def test_an_appeal_on_a_final_reject_is_refused(tmp_path, episode, why):
    rd = RunDir(str(tmp_path / "run")).good(0, 2, 9)
    rd.replace("task_success", 2, "fail").replace("task_success", 9, "fail")
    _gate_reject(rd, 4, "timestamp_check")
    rd.good(5).replace("video_action_sync", 5, "fail").replace("task_success", 5, "fail")
    rd.good(3).replace("motion_quality", 3, "scored", score=0.1)
    rd.replace("visual_quality", 3, "scored", score=0.1)
    run_dir = rd.write()
    apply(run_dir, decisions(str(tmp_path / "d0.json"), (9, "label", "discard", None)))
    res = run("adjudicate-apply", "--run-dir", run_dir, "--decisions",
              decisions(str(tmp_path / "d1.json"), (2, "reject_appeal", "keep_rejected", None),
                        (episode, "reject_appeal", "restore", None)))
    assert res.rc == 2 and "has no reject a person may appeal" in res.doc["error"]["message"], \
        why
    assert len(read_jsonl(os.path.join(run_dir, "adjudication", "applied.jsonl"))) == 1
    ok = run("adjudicate-apply", "--run-dir", run_dir, "--decisions",
             decisions(str(tmp_path / "d2.json"), (2, "reject_appeal", "restore", None)))
    assert ok.rc == 0, ok.doc


def test_what_can_be_appealed_comes_from_the_registry(tmp_path, monkeypatch):
    """C1 2.x ``appealable`` on a finding code decides (task_success's failure and dedup's
    duplicate here): made appealable, a reject by video_action_sync's misaligned_all gets an
    appeal item, admits an appeal and a restore overturns it; nothing else names the codes."""
    from dataclasses import replace

    from curation.contracts import modules as registry

    assert [(m, c.code) for m in ALL for c in registry.get(m).codes if c.appealable] == \
        [("task_success", "failure"), ("dedup", "duplicate")]
    rd = _gate_reject(RunDir(str(tmp_path / "run")).good(0), 1, "video_action_sync")
    run_dir = rd.write()
    assert _kinds(final(run_dir, "0,1")) == {}
    refused = run("adjudicate-apply", "--run-dir", run_dir, "--decisions",
                  decisions(str(tmp_path / "d0.json"), (1, "reject_appeal", "restore", None)))
    assert refused.rc == 2
    real = registry.finding_code

    def finding_code(module, code):
        spec = real(module, code)
        return replace(spec, appealable=True) if (module, code) == ("video_action_sync", "misaligned_all") \
            else spec

    monkeypatch.setattr(registry, "finding_code", finding_code)
    assert _kinds(final(run_dir, "0,1")) == {1: [("reject_appeal", "video_action_sync")]}
    apply(run_dir, decisions(str(tmp_path / "d1.json"), (1, "reject_appeal", "restore", None)))
    assert sorted(final(run_dir, "0,1", revision=2)["passed"]) == [0, 1]


def test_items_name_their_registry_line_and_where_it_applies(tmp_path):
    """Every item carries its C1 line (label for label_conflict) and the codes it asks about; a
    dedup appeal names the original; a line is only asked where it applies (label: passed
    episodes)."""
    rd = RunDir(str(tmp_path / "run")).good(0, 3)
    rd.replace("task_success", 0, "abstain")
    rd.replace("dedup", 3, "fail", details={"duplicate_of": 0})
    run_dir = _label_questions(rd, 0, 3)
    lists = final(run_dir, "0,3")
    items = {e: v["review"] for e, v in lists["review"].items()}
    assert [(i["kind"], i["line"], i["codes"]) for i in items[0]] == [
        ("task_verdict", "task_verdict", ["uncertain"]),
        ("label_conflict", "label", ["label_conflict_suspect"])]
    assert items[3] == [{"source_module": "dedup", "kind": "reject_appeal",
                         "line": "reject_appeal", "duplicate_of": 0, "codes": ["duplicate"],
                         "items": ["SET-1"], "reason": "与 ep000000 字节级完全重复"}]


def _label_questions(rd: RunDir, *eps: int) -> str:
    """The run directory of ``rd`` with the label audit queueing ``eps``.

    The queue used to have two sources, the skill profile and task_success's kill guard; the
    profile left with registry 3.0, so the guard is the only one and the records carry its
    ``label_conflict_suspect``."""
    for e in eps:
        rd.replace("task_success", e, "abstain",
                   details={"verdict": "label_conflict_suspect", "task_desc": TASK,
                            "task_desc_source": "原始标注",
                            "label_check": {"annotation": "旧标注", "caption": "画面描述"}})
    return rd.write()


def test_a_task_verdict_after_a_relabel_is_taken_instead_of_a_re_judge(tmp_path):
    """C1 1.3's follow-up (v1's relabel card): on an episode asked about its label, a person who
    adopts a new label may also conclude the task. A success or failure is taken as it is and the
    episode is not judged again; "unsure" leaves the re-judge.

    Since the skill profile left (registry 3.0) the label question only ever comes from the kill
    guard, which holds the episode, so the card always carries the task verdict question too."""
    run_dir = _label_questions(RunDir(str(tmp_path / "run")).good(0, 1, 2), 0, 1, 2)
    first = final(run_dir, "0-2")
    assert {e: [i["line"] for i in v["review"]] for e, v in first["review"].items()} == \
        {0: ["task_verdict", "label"], 1: ["task_verdict", "label"], 2: ["task_verdict", "label"]}

    out = apply(run_dir, decisions(str(tmp_path / "d.json"),
                                   (0, "label", "adopt_suggestion", "stack the cups"),
                                   (0, "task_verdict", "success", None),
                                   (1, "label", "custom_label", "wipe the table"),
                                   (1, "task_verdict", "unsure", None),
                                   (2, "label", "adopt_suggestion", "stack the cups"),
                                   (2, "task_verdict", "failure", None)))
    assert out["applied"] == 6
    assert out["rerun_task_success"] == [1]              # 0 and 2 were concluded by a person
    after = final(run_dir, "0-2", revision=2)
    assert sorted(after["passed"]) == [0] and sorted(after["held"]) == [1]
    assert after["passed"][0]["task_text"] == {"text": "stack the cups", "source": "人工改标"}
    assert after["reject"][2]["reasons"][0]["text"].startswith("人工裁决判失败")
    assert after["held"][1]["reasons"][0]["text"] == "改标后尚未按新标注重跑任务成败判定"
    assert after["review"] == {}                          # every question answered

    # the re-judge of 1 with its new label abstains: now the task question is asked
    rd = RunDir(run_dir)
    rd.put("task_success", 1, "abstain", part="0002",
           details={"verdict": "review_conflict", "task_desc": "wipe the table",
                    "task_desc_source": "人工改标"})
    rd.write()
    third = final(run_dir, "0-2", revision=3)
    assert sorted(third["passed"]) == [0, 1]
    assert [i["line"] for i in third["review"][1]["review"]] == ["task_verdict"]


def _apply_more(run_dir: str, path: str, first_id: int, *items) -> dict:
    """adjudicate-apply of ``items`` numbered from ``first_id`` (a later batch)."""
    doc = {"schema_version": "1.0", "decisions": [
        {"id": i, "episode_index": ep, "line": line, "decision": decision,
         "new_label": new_label, "note": None, "decided_by": "alice",
         "decided_at": 1790000000000 + i}
        for i, (ep, line, decision, new_label) in enumerate(items, start=first_id)]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return apply(run_dir, path)


@pytest.mark.skip(reason="the follow-up question has no live path since the skill profile left (registry 3.0): the label question now only comes from the kill guard, which also leaves the task verdict open, so a task answer is a primary answer and never lapses")
def test_a_follow_up_verdict_lapses_when_its_label_answer_changes(tmp_path):
    """The verdict given with a relabel on a label-only card (task_success passed)
    answers the registry's follow-up: it counts while the label answer that opened it
    is still the latest and was given before it. Changed to keep_label, the failure
    lapses and the machine's pass is back; changed to "unsure", the failure lapses too,
    the relabel still stands, and it is judged again."""
    from curation.pipeline.adjudication import Decisions

    run_dir = _label_questions(RunDir(str(tmp_path / "run")).good(0, 1), 0, 1)
    out = _apply_more(run_dir, str(tmp_path / "d1.json"), 1,
                      (0, "label", "custom_label", "stack the cups"),
                      (0, "task_verdict", "failure", None),
                      (1, "label", "custom_label", "wipe the table"),
                      (1, "task_verdict", "failure", None))
    assert out["rerun_task_success"] == []
    second = final(run_dir, "0,1", revision=2)
    assert sorted(second["reject"]) == [0, 1]

    out = _apply_more(run_dir, str(tmp_path / "d2.json"), 5,
                      (0, "label", "keep_label", None), (1, "label", "unsure", None))
    assert out["rerun_task_success"] == [1]           # 1's relabel stands, its verdict not
    decided = Decisions.of(run_dir)
    assert decided.human_task_verdict(0) is None and decided.human_task_verdict(1) is None
    third = final(run_dir, "0,1", revision=3)
    assert sorted(third["passed"]) == [0] and sorted(third["held"]) == [1]
    assert "task_text" not in third["passed"][0]            # the original annotation again
    assert third["held"][1]["reasons"][0]["text"] == "改标后尚未按新标注重跑任务成败判定"


@pytest.mark.skip(reason="the follow-up question has no live path since the skill profile left (registry 3.0): the label question now only comes from the kill guard, which also leaves the task verdict open, so a task answer is a primary answer and never lapses")
def test_a_resubmitted_label_needs_its_verdict_again(tmp_path):
    """A new label answer lapses the verdict given before it: the new relabel is judged
    again, unless the verdict is given once more after it."""
    run_dir = _label_questions(RunDir(str(tmp_path / "run")).good(0), 0)
    assert _apply_more(run_dir, str(tmp_path / "d1.json"), 1,
                       (0, "label", "custom_label", "stack the cups"),
                       (0, "task_verdict", "success", None))["rerun_task_success"] == []
    out = _apply_more(run_dir, str(tmp_path / "d2.json"), 3,
                      (0, "label", "custom_label", "wipe the table"))
    assert out["rerun_task_success"] == [0]
    assert sorted(final(run_dir, "0", revision=2)["held"]) == [0]
    out = _apply_more(run_dir, str(tmp_path / "d3.json"), 4,
                      (0, "task_verdict", "success", None))
    assert out["rerun_task_success"] == []
    third = final(run_dir, "0", revision=3)
    assert sorted(third["passed"]) == [0]
    assert third["passed"][0]["task_text"] == {"text": "wipe the table", "source": "人工改标"}


def test_a_verdict_on_the_cards_own_question_never_lapses(tmp_path):
    """Where task_success abstained the task verdict is the card's own question: a later
    change of the label answer leaves it standing."""
    rd = RunDir(str(tmp_path / "run")).good(0)
    rd.replace("task_success", 0, "abstain")
    run_dir = _label_questions(rd, 0)
    _apply_more(run_dir, str(tmp_path / "d1.json"), 1,
                (0, "label", "custom_label", "stack the cups"),
                (0, "task_verdict", "failure", None))
    _apply_more(run_dir, str(tmp_path / "d2.json"), 3, (0, "label", "keep_label", None))
    lists = final(run_dir, "0", revision=2)
    assert sorted(lists["reject"]) == [0]
    assert lists["reject"][0]["reasons"][0]["text"] == "人工裁决判失败（任务未完成）"


def test_a_line_adjudicate_apply_has_no_rule_for_is_refused(tmp_path):
    """decisions.json lines are open strings (C2 1.5); applying one without a rule is
    refused, naming it - never skipped. The rules cover the registry's lines."""
    from curation.contracts import modules as registry
    from curation.pipeline.adjudication import LINE_DECISIONS

    assert {line: set(ds) for line, ds in LINE_DECISIONS.items()} == \
        {line.id: {c for c, _ in line.decisions} for line in registry.REVIEW_LINES}
    rd = RunDir(str(tmp_path / "run")).good(0).write()
    res = run("adjudicate-apply", "--run-dir", rd, "--decisions",
              decisions(str(tmp_path / "d.json"), (0, "retrim", "cut_tail", None)))
    assert res.rc == 2 and "line 'retrim' has no apply rule" in res.doc["error"]["message"]
    assert not os.path.exists(os.path.join(rd, "adjudication", "applied.jsonl"))


def test_decisions_are_copied_in_v1s_csv_words(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0, 1).replace("task_success", 1, "fail").write()
    apply(rd, decisions(str(tmp_path / "d.json"), (0, "task_verdict", "failure", None),
                        (1, "label", "keep_label", None), (1, "reject_appeal", "unsure", None)))
    with open(os.path.join(rd, "human-decisions", "task_verdicts.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["episode_id"], r["verdict"]) for r in rows] == [("ep000000", "判失败")]
    with open(os.path.join(rd, "human-decisions", "label_decisions.csv"), encoding="utf-8") as fh:
        assert [r["decision"] for r in csv.DictReader(fh)] == ["维持原标注"]
    with open(os.path.join(rd, "human-decisions", "reject_appeals.csv"), encoding="utf-8") as fh:
        assert [r["appeal"] for r in csv.DictReader(fh)] == ["拿不准"]
    with open(os.path.join(rd, "adjudication", "labels.json"), encoding="utf-8") as fh:
        assert json.load(fh) == {"labels": {}}


@pytest.mark.parametrize("bad, words", [
    ({"line": "task_verdict", "decision": "restore"}, "is not a task_verdict decision"),
    ({"line": "label", "decision": "adopt_suggestion", "new_label": ""}, "needs new_label"),
    ({"line": "verdict", "decision": "success"}, "has no apply rule"),
])
def test_a_decision_that_breaks_the_contract_is_a_usage_error(tmp_path, bad, words):
    rd = RunDir(str(tmp_path / "run")).good(0).write()
    d = {"id": 1, "episode_index": 0, "new_label": None, "note": None, "decided_by": "a",
         "decided_at": 1, **bad}
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"schema_version": "1.0", "decisions": [d]}))
    res = run("adjudicate-apply", "--run-dir", rd, "--decisions", str(path))
    assert res.rc == 2 and words in res.doc["error"]["message"]
    assert not os.path.exists(os.path.join(rd, "adjudication", "applied.jsonl"))


def test_every_entry_lists_its_findings_with_the_level_they_have(tmp_path):
    """C2 final-list 2.0 ``findings`` (F12.5): every finding of the episode with its level after the applied
    human decisions, pointing at its place in the module's record; a person's conclusion carries its text,
    an appeal turns the appealable blocking findings to info, a settled abstention is gone, and the member a
    dedup group keeps has no duplicate finding."""
    rd = RunDir(str(tmp_path / "run")).good(0, 1, 2, 3, 4, 5)
    rd.replace("timestamp_check", 1, "fail")
    rd.replace("task_success", 2, "fail")
    rd.replace("task_success", 3, "abstain")
    rd.replace("task_success", 4, "abstain")
    rd.replace("timestamp_check", 0, "fail")                   # the original of a group is rejected ...
    rd.replace("dedup", 5, "fail", details={"duplicate_of": 0})    # ... so its copy is the one kept
    run_dir = rd.write()

    def graded(lists, ep):
        name = next(n for n in ("passed", "reject", "held") if ep in lists[n])
        return [(f["module"], f["code"], f["item"], f["level"], f.get("line"), f.get("index"),
                 f.get("human", False), f["appealable"]) for f in lists[name][ep]["findings"]]

    first = final(run_dir, "0-5")
    assert graded(first, 1) == [("timestamp_check", "gap", "STRM-3", "blocking", None, 0, False, False)]
    assert graded(first, 2) == [("task_success", "failure", "TASK-5", "blocking", None, 0, False, True)]
    assert graded(first, 3) == [("task_success", "uncertain", "TASK-5", "review", "task_verdict", 0, False, False)]
    assert sorted(first["passed"]) == [3, 4, 5] and graded(first, 5) == []      # the keeper: no duplicate
    assert all(isinstance(e["findings"], list) for n in ("passed", "reject", "held") for e in first[n].values())

    apply(run_dir, decisions(str(tmp_path / "d.json"), (2, "reject_appeal", "restore", None),
                             (3, "task_verdict", "failure", None), (4, "task_verdict", "success", None)))
    after = final(run_dir, "0-5", revision=2)
    assert graded(after, 2) == [("task_success", "failure", "TASK-5", "info", None, 0, False, True)]
    assert graded(after, 3) == [("task_success", "failure", "TASK-5", "blocking", None, None, True, False)]
    assert after["reject"][3]["findings"][0]["message_zh"] == "人工裁决判失败（任务未完成）"
    assert graded(after, 4) == []                                              # its abstention is settled
