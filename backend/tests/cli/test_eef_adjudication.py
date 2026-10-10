"""The EEF module's person's question (C1 1.9 ``eef_check``, design doc 12 D-E13; F5.11; design doc 25 §7.3).

A run from before registry 5.0 (its run.json says 4.4, no frozen defaults: D81's legacy table): an episode
the module could not settle (its ``unsettled`` review finding) is kept and asked on the review tab;
"consistent" settles it, "inconsistent" makes it a person's blocking finding, "unsure" keeps it asked. A
reject may be appealed when every blocking finding is appealable (D42 by finding, design doc 17 §4.4: the
module's own, also with task_success's failure); with a final finding beside it, or once a person concluded,
it may not. The answers act next to a human task verdict.

From 5.0 on (``test_a_conflict_is_the_only_question_and_nothing_is_rejected``): opinions reject nothing, a
conflict between the channels is the only question and the answers act on it the same way.
"""
from __future__ import annotations

import csv
import json
import os

from curation.contracts import schemas
from curation.pipeline.records import record_v2

from .pipeline import run
from .test_aggregate import ALL, RunDir, _apply_more, apply, decisions

EEF = "eef_video_consistency"
MODULES = ",".join((*ALL, EEF))
WHY = "「位置」（相机 ext）CPU 判为可疑，模型多数认为一致（支持 2、反对 1）"


def eef(rd: RunDir, ep: int, verdict: str) -> RunDir:
    passed = {"pass": True, "fail": False}.get(verdict)
    reason = {"pass": "", "fail": "「位置」（相机 ext）CPU 与模型都认为不一致（模型反对 2、支持 0）",
              "abstain": "需要人工裁决：" + WHY}[verdict]
    outcome = {"pass": "pass", "fail": "reject", "abstain": "human"}[verdict]
    details = {"reason": reason, "assessment_mode": "verdict", "decision": {"outcome": outcome}}
    rd.parts[EEF]["0001"].append(record_v2(EEF, ep, passed, None, details, error=None, evidence=[],
                                           elapsed_s=0.1))
    return rd


def final(run_dir: str, episodes: str, revision: int = 1) -> dict[str, dict[int, dict]]:
    res = run("aggregate", "--run-dir", run_dir, "--phase", "final", "--revision", str(revision),
              "--modules", MODULES, "--episodes", episodes)
    assert res.rc == 0, res.doc
    out = {}
    for name in ("passed", "reject", "held", "review"):
        with open(os.path.join(run_dir, "revisions", f"r{revision:04d}", f"{name}.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        assert schemas.errors("cli/final-list.schema.json", doc) == [], name
        out[name] = {e["episode_index"]: e for e in doc["episodes"]}
    return out


def _asked(lists) -> dict[int, list[tuple[str, str, str]]]:
    return {e: [(i["line"], i["kind"], i["source_module"]) for i in v["review"]] for e, v in lists["review"].items()}


def _legacy(run_dir: str) -> str:
    """The run started under registry 4.4: no frozen defaults, so 4.x's (the EEF module's rejects stand)."""
    with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as fh:
        json.dump({"policy": {"preset": "default", "rules": [], "version": "2"}, "registry_version": "4.4"}, fh)
    return run_dir


def _world(tmp_path) -> str:
    rd = RunDir(str(tmp_path / "run")).good(*range(9))
    for ep, v in ((0, "abstain"), (1, "pass"), (2, "fail"), (3, "abstain"), (4, "abstain"), (5, "fail"),
                  (6, "abstain"), (7, "abstain"), (8, "fail")):
        eef(rd, ep, v)
    rd.replace("video_action_sync", 5, "fail")               # a final finding beside it: final
    rd.replace("task_success", 6, "abstain")                 # both questions on one card
    rd.replace("task_success", 7, "abstain")
    rd.replace("task_success", 8, "fail")                    # two appealable findings: appealable
    return _legacy(rd.write())


def test_what_the_module_could_not_settle_is_asked_and_its_rejects_may_be_appealed(tmp_path):
    lists = final(_world(tmp_path), "0-8")
    assert sorted(lists["passed"]) == [0, 1, 3, 4, 6, 7] and sorted(lists["reject"]) == [2, 5, 8]
    eef_q = ("eef_check", "eef_consistency", EEF)
    appeal = ("reject_appeal", "reject_appeal", EEF)
    assert _asked(lists) == {0: [eef_q], 2: [appeal], 3: [eef_q], 4: [eef_q],
                             6: [("task_verdict", "task_verdict", "task_success"), eef_q],
                             7: [("task_verdict", "task_verdict", "task_success"), eef_q], 8: [appeal]}
    assert lists["review"][0]["review"][1:] == [] and lists["review"][0]["review"][0]["reason"] == WHY
    assert lists["review"][0]["review"][0]["codes"] == ["unsettled"]
    assert lists["review"][2]["reasons"][0]["text"].startswith("「位置」（相机 ext）CPU 与模型都认为不一致")
    assert lists["review"][8]["review"][0]["codes"] == ["inconsistent", "failure"]
    assert [(r["module"], r["appealable"]) for r in lists["reject"][5]["reasons"]] == \
        [("video_action_sync", False), (EEF, True)]


def test_the_answers_act_as_the_gates_result(tmp_path):
    run_dir = _world(tmp_path)
    final(run_dir, "0-8")
    out = apply(run_dir, decisions(str(tmp_path / "d.json"),
                                   (0, "eef_check", "consistent", None),
                                   (3, "eef_check", "inconsistent", None),
                                   (4, "eef_check", "unsure", None),
                                   (2, "reject_appeal", "restore", None),
                                   (6, "eef_check", "inconsistent", None), (6, "task_verdict", "success", None),
                                   (7, "eef_check", "consistent", None), (7, "task_verdict", "failure", None)))
    assert out["rerun_task_success"] == []
    after = final(run_dir, "0-8", revision=2)
    assert sorted(after["passed"]) == [0, 1, 2, 4] and sorted(after["reject"]) == [3, 5, 6, 7, 8]
    assert _asked(after) == {4: [("eef_check", "eef_consistency", EEF)],          # unsure: still asked
                             8: [("reject_appeal", "reject_appeal", EEF)]}
    human = {"module": EEF, "kind": "human", "code": "unsettled", "item": "MV-4", "appealable": False,
             "text": "人工裁决判为 EEF 与视频不一致"}
    assert after["reject"][3]["reasons"] == [human] and after["reject"][6]["reasons"] == [human]
    assert after["reject"][7]["reasons"] == [{"module": "task_success", "kind": "human", "code": "failure",
                                             "item": "TASK-4", "appealable": False,
                                             "text": "人工裁决判失败（任务未完成）"}]
    with open(os.path.join(run_dir, "revisions", "r0002", "keep.txt"), encoding="utf-8") as fh:
        assert [int(x) for x in fh.read().split()] == [0, 1, 2, 4]
    with open(os.path.join(run_dir, "human-decisions", "eef_checks.csv"), encoding="utf-8") as fh:
        assert [(r["episode_id"], r["decision"]) for r in csv.DictReader(fh)] == \
            [("ep000000", "一致"), ("ep000003", "不一致"), ("ep000004", "拿不准"), ("ep000006", "不一致"),
             ("ep000007", "一致")]


def test_a_reject_shared_with_a_final_finding_or_answered_by_a_person_is_final(tmp_path):
    run_dir = _world(tmp_path)
    final(run_dir, "0-8")
    refused = run("adjudicate-apply", "--run-dir", run_dir, "--decisions",
                  decisions(str(tmp_path / "d0.json"), (5, "reject_appeal", "restore", None)))
    assert refused.rc == 2 and "has no reject a person may appeal" in refused.doc["error"]["message"]
    apply(run_dir, decisions(str(tmp_path / "d1.json"), (3, "eef_check", "inconsistent", None)))
    path = tmp_path / "d2.json"
    path.write_text(json.dumps({"schema_version": "1.0", "decisions": [
        {"id": 10, "episode_index": 3, "line": "reject_appeal", "decision": "restore", "new_label": None,
         "note": None, "decided_by": "alice", "decided_at": 1790000000010}]}))
    again = run("adjudicate-apply", "--run-dir", run_dir, "--decisions", str(path))
    assert again.rc == 2 and "has no reject a person may appeal" in again.doc["error"]["message"]
    # two appealable findings (the module's and task_success's): a restore lifts both
    _apply_more(run_dir, str(tmp_path / "d3.json"), 20, (8, "reject_appeal", "restore", None))
    assert 8 in final(run_dir, "0-8", revision=2)["passed"]


def _cell(p: float, flags: list, cpu: float, vlm: float | None) -> dict:
    srcs = {"cpu": {"verdict": "issue" if cpu >= 0.5 else "ok", "p": cpu}}
    if vlm is not None:
        srcs["vlm_review"] = {"verdict": "issue" if vlm >= 0.5 else "ok", "p": vlm}
    label = "inconsistent" if p >= 0.7 else "possibly_inconsistent" if p >= 0.4 else "consistent"
    return {"subitem": "position_2d", "camera": "ext", "p": p, "label": label, "flags": flags, "sources": srcs}


def opinion(rd: RunDir, ep: int, cell: dict | None) -> RunDir:
    """A 5.0 record: the merged cells and the episode (design doc 25 §7)."""
    cells = [cell] if cell else []
    episode = ({"label": cell["label"], "p": cell["p"], "subitem": "position_2d", "camera": "ext",
                "flags": cell["flags"], "reason": "x", "conflicts": int("conflict" in cell["flags"])} if cell else
               {"label": "cannot_tell", "p": None, "flags": [], "why": [], "conflicts": 0, "reason": "判断不了"})
    details = {"assessment_mode": "verdict", "merged": {"cells": cells, "episode": episode}}
    rd.parts[EEF]["0001"].append(record_v2(EEF, ep, True, None, details, error=None, evidence=[], elapsed_s=0.1))
    return rd


def test_a_conflict_is_the_only_question_and_nothing_is_rejected(tmp_path):
    """Registry 5.0 (D81): an opinion "inconsistent" at any confidence keeps the episode; a conflict is asked on
    eef_check; "inconsistent" on it blocks (kind human, not appealable), "consistent" settles it, "unsure"
    keeps it asked; the module's findings give nothing to appeal."""
    rd = RunDir(str(tmp_path / "run")).good(*range(6))
    conflict = _cell(0.95, ["conflict"], 0.95, 0.1)
    for ep, cell in ((0, conflict), (1, _cell(1.0, [], 1.0, 0.9)), (2, _cell(0.8, ["single_source"], 0.9, None)),
                     (3, conflict), (4, conflict), (5, None)):
        opinion(rd, ep, cell)
    run_dir = rd.write()
    lists = final(run_dir, "0-5")
    assert sorted(lists["passed"]) == [0, 1, 2, 3, 4, 5] and not lists["reject"]
    q = ("eef_check", "eef_consistency", EEF)
    assert _asked(lists) == {0: [q], 3: [q], 4: [q]}
    assert lists["review"][0]["review"][0]["codes"] == ["conflict"]
    apply(run_dir, decisions(str(tmp_path / "d.json"), (0, "eef_check", "consistent", None),
                             (3, "eef_check", "inconsistent", None), (4, "eef_check", "unsure", None)))
    after = final(run_dir, "0-5", revision=2)
    assert sorted(after["passed"]) == [0, 1, 2, 4, 5] and sorted(after["reject"]) == [3]
    assert after["reject"][3]["reasons"] == [{"module": EEF, "kind": "human", "code": "conflict", "item": "MV-4",
                                             "appealable": False, "text": "人工裁决判为 EEF 与视频不一致"}]
    assert _asked(after) == {4: [q]}
    path = tmp_path / "d2.json"                                  # a person's conclusion is final
    path.write_text(json.dumps({"schema_version": "1.0", "decisions": [
        {"id": 10, "episode_index": 3, "line": "reject_appeal", "decision": "restore", "new_label": None,
         "note": None, "decided_by": "alice", "decided_at": 1790000000010}]}))
    refused = run("adjudicate-apply", "--run-dir", run_dir, "--decisions", str(path))
    assert refused.rc == 2 and "has no reject a person may appeal" in refused.doc["error"]["message"]
