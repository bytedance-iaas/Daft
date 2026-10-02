"""The EEF module's person's question (C1 1.9 ``eef_check``, design doc 12 D-E13; F5.11).

An episode the module could not settle (its ``unsettled`` review finding) is kept and asked on
the review tab; "consistent" settles it, "inconsistent" makes it a person's blocking finding,
"unsure" keeps it asked. A reject may be appealed when every blocking finding is appealable
(D42 by finding, design doc 17 §4.4: the module's own, also with task_success's failure); with a
final finding beside it, or once a person concluded, it may not. The answers act next to a human
task verdict.
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


def _world(tmp_path) -> str:
    rd = RunDir(str(tmp_path / "run")).good(*range(9))
    for ep, v in ((0, "abstain"), (1, "pass"), (2, "fail"), (3, "abstain"), (4, "abstain"), (5, "fail"),
                  (6, "abstain"), (7, "abstain"), (8, "fail")):
        eef(rd, ep, v)
    rd.replace("video_action_sync", 5, "fail")               # a final finding beside it: final
    rd.replace("task_success", 6, "abstain")                 # both questions on one card
    rd.replace("task_success", 7, "abstain")
    rd.replace("task_success", 8, "fail")                    # two appealable findings: appealable
    return rd.write()


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
    assert out["rerun_task_success"] == [] and out["profile_resync"] == [0, 2, 3, 6, 7]
    after = final(run_dir, "0-8", revision=2)
    assert sorted(after["passed"]) == [0, 1, 2, 4] and sorted(after["reject"]) == [3, 5, 6, 7, 8]
    assert _asked(after) == {4: [("eef_check", "eef_consistency", EEF)],          # unsure: still asked
                             8: [("reject_appeal", "reject_appeal", EEF)]}
    human = {"module": EEF, "kind": "human", "code": "unsettled", "item": "MV-5", "appealable": False,
             "text": "人工裁决判为 EEF 与视频不一致"}
    assert after["reject"][3]["reasons"] == [human] and after["reject"][6]["reasons"] == [human]
    assert after["reject"][7]["reasons"] == [{"module": "task_success", "kind": "human", "code": "failure",
                                             "item": "TASK-5", "appealable": False,
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
