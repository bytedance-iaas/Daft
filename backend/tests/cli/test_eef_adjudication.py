"""The EEF module's person's question (C1 ``eef_check``, design doc 25 §7.3, D81).

Opinions reject nothing: a conflict between the module's two channels is its only question, asked on the
review tab; "consistent" settles it, "inconsistent" makes it a person's blocking finding (not appealable),
"unsure" keeps it asked. The question shares the card with a task verdict when task_success is unsure too.
"""
from __future__ import annotations

import json
import os

from curation.contracts import schemas
from curation.pipeline.records import record_v2

from .pipeline import run
from .test_aggregate import ALL, RunDir, apply, decisions

EEF = "eef_video_consistency"
MODULES = ",".join((*ALL, EEF))


def final(run_dir: str, episodes: str, revision: int = 1) -> dict[str, dict[int, dict]]:
    res = run("aggregate", "--run-dir", run_dir, "--revision", str(revision),
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
    rd = RunDir(str(tmp_path / "run")).good(*range(7))
    conflict = _cell(0.95, ["conflict"], 0.95, 0.1)
    for ep, cell in ((0, conflict), (1, _cell(1.0, [], 1.0, 0.9)), (2, _cell(0.8, ["single_source"], 0.9, None)),
                     (3, conflict), (4, conflict), (5, None), (6, conflict)):
        opinion(rd, ep, cell)
    rd.replace("task_success", 6, "abstain")                     # both questions on one card
    run_dir = rd.write()
    lists = final(run_dir, "0-6")
    assert sorted(lists["passed"]) == [0, 1, 2, 3, 4, 5, 6] and not lists["reject"]
    q = ("eef_check", "eef_consistency", EEF)
    task = ("task_verdict", "task_verdict", "task_success")
    assert _asked(lists) == {0: [q], 3: [q], 4: [q], 6: [task, q]}
    assert lists["review"][0]["review"][0]["codes"] == ["conflict"]
    apply(run_dir, decisions(str(tmp_path / "d.json"), (0, "eef_check", "consistent", None),
                             (3, "eef_check", "inconsistent", None), (4, "eef_check", "unsure", None),
                             (6, "task_verdict", "success", None), (6, "eef_check", "consistent", None)))
    after = final(run_dir, "0-6", revision=2)
    assert sorted(after["passed"]) == [0, 1, 2, 4, 5, 6] and sorted(after["reject"]) == [3]
    assert after["reject"][3]["reasons"] == [{"module": EEF, "kind": "human", "code": "conflict", "item": "MV-4",
                                             "appealable": False, "text": "人工裁决判为 EEF 与视频不一致"}]
    assert _asked(after) == {4: [q]}
    path = tmp_path / "d2.json"                                  # a person's conclusion is final
    path.write_text(json.dumps({"schema_version": "2.0", "decisions": [
        {"id": 10, "episode_index": 3, "line": "reject_appeal", "decision": "restore", "new_label": None,
         "note": None, "decided_by": "alice", "decided_at": 1790000000010}]}))
    refused = run("adjudicate-apply", "--run-dir", run_dir, "--decisions", str(path))
    assert refused.rc == 2 and "has no reject a person may appeal" in refused.doc["error"]["message"]
