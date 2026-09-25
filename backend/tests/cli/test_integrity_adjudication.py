"""The data integrity gate in the verdict and its person's question (design doc 14 §4, D50-D51).

A reject is final (no appeal) and ends the funnel for its episode: the later stages have no
result for it, and it is a reject, not held. A suspect is kept, delivered and asked on
``integrity_check`` - counted as pending like every line on passed episodes; "intact" keeps
it, "broken" rejects it with a human reason, "unsure" keeps it asked.
"""
from __future__ import annotations

import csv
import json
import os

from .pipeline import run
from .test_aggregate import ALL, RunDir, apply, decisions
from .test_eef_adjudication import _asked

MOD = "data_integrity"
MODULES = ",".join((MOD, *ALL))
WHY = "exterior 相机的视频与 ep 7 的内容完全相同"


def integrity(rd: RunDir, ep: int, verdict: str) -> RunDir:
    passed = {"pass": True, "fail": False}.get(verdict)
    reason = {"pass": "", "fail": "文件被截断：exterior 相机的视频在 1.2 MB 处被截断，第 40 帧（2.67 秒）起的数据缺失",
              "abstain": "需要人工裁决：" + WHY}[verdict]
    rd.parts[MOD]["0001"].append({
        "episode_index": ep, "module": MOD, "verdict": verdict, "passed": passed, "score": None,
        "gate": "hard", "details": {"outcome": {"pass": "pass", "fail": "reject", "abstain": "suspect"}[verdict],
                                    "reason": reason, "tiers": {"L1": True, "L2": True, "L3": False},
                                    "findings": [], "files": []},
        "evidence": [], "elapsed_s": 0.1, "error": None})
    return rd


def final(run_dir: str, episodes: str, revision: int = 1) -> dict[str, dict[int, dict]]:
    import json

    res = run("aggregate", "--run-dir", run_dir, "--phase", "final", "--revision", str(revision),
              "--modules", MODULES, "--episodes", episodes)
    assert res.rc == 0, res.doc
    out = {}
    for name in ("passed", "reject", "held", "review"):
        with open(os.path.join(run_dir, "revisions", f"r{revision:04d}", f"{name}.json"), encoding="utf-8") as fh:
            out[name] = {e["episode_index"]: e for e in json.load(fh)["episodes"]}
    return out


def _world(tmp_path) -> str:
    rd = RunDir(str(tmp_path / "run")).good(0, 2, 3, 4)
    for ep, v in ((0, "pass"), (1, "fail"), (2, "abstain"), (3, "abstain"), (4, "abstain")):
        integrity(rd, ep, v)
    # 1 was rejected at the first gate: no later stage ever saw it
    rd.replace("task_success", 4, "abstain")                  # two questions on one card
    return rd.write()


def test_a_reject_ends_the_funnel_and_is_final(tmp_path):
    lists = final(_world(tmp_path), "0-4")
    assert sorted(lists["passed"]) == [0, 2, 3, 4] and sorted(lists["reject"]) == [1]
    assert lists["held"] == {}                                # not "no result": killed by the gate
    with open(os.path.join(str(tmp_path / "run"), "revisions", "r0001", "verdicts.jsonl"), encoding="utf-8") as fh:
        line = next(json.loads(ln) for ln in fh if json.loads(ln)["episode_index"] == 1)
    assert line["verdict"] == "drop" and line["hard_fails"] == [MOD] and line["reason"].startswith("未通过「数据完整性」")
    assert lists["reject"][1]["reasons"] == [{
        "module": MOD, "kind": "hard_gate",
        "text": "未通过「数据完整性」:文件被截断：exterior 相机的视频在 1.2 MB 处被截断，第 40 帧（2.67 秒）起的数据缺失"}]
    q = ("integrity_check", "integrity_suspect", MOD)
    assert _asked(lists) == {2: [q], 3: [q], 4: [("task_verdict", "task_verdict", "task_success"), q]}
    assert lists["review"][2]["review"][0]["reason"] == WHY
    refused = run("adjudicate-apply", "--run-dir", str(tmp_path / "run"), "--decisions",
                  decisions(str(tmp_path / "d0.json"), (1, "reject_appeal", "restore", None)))
    assert refused.rc == 2 and "has no reject a person may appeal" in refused.doc["error"]["message"]


def test_the_answers_act_as_the_gates_result(tmp_path):
    run_dir = _world(tmp_path)
    final(run_dir, "0-4")
    apply(run_dir, decisions(str(tmp_path / "d.json"),
                             (2, "integrity_check", "intact", None),
                             (3, "integrity_check", "broken", None),
                             (4, "integrity_check", "unsure", None)))
    after = final(run_dir, "0-4", revision=2)
    assert sorted(after["passed"]) == [0, 2, 4] and sorted(after["reject"]) == [1, 3]
    assert after["reject"][3]["reasons"] == [{"module": MOD, "kind": "human", "text": "人工裁决判为数据确有问题"}]
    assert _asked(after) == {4: [("task_verdict", "task_verdict", "task_success"),
                                 ("integrity_check", "integrity_suspect", MOD)]}   # unsure: still asked
    with open(os.path.join(run_dir, "human-decisions", "integrity_checks.csv"), encoding="utf-8") as fh:
        assert [(r["episode_id"], r["decision"]) for r in csv.DictReader(fh)] == \
            [("ep000002", "数据无误"), ("ep000003", "数据确有问题"), ("ep000004", "拿不准")]
