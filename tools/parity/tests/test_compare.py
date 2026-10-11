"""The compare command on hand-made v2 run directories."""
from __future__ import annotations

import json
import os

import pytest

from parity import compare as C
from parity import records as R


def _run(root, *, records=None, final=None, duplicates=None, parts=None, tape_mode="record", misses=0,
         kind="v2-run"):
    """A v2 run directory: ``checks/<m>/results.jsonl``, ``revisions/r0001`` with the lists, ``parity.json``."""
    os.makedirs(os.path.join(root, "checks"), exist_ok=True)
    with open(os.path.join(root, "parity.json"), "w") as fh:
        json.dump({"kind": kind, "tape": {"mode": tape_mode, "hooks": {"misses": misses,
                                                                       "hits": {"logical": 3}}}}, fh)
    for module, rows in (records or {}).items():
        os.makedirs(os.path.join(root, "checks", module), exist_ok=True)
        R.write_jsonl(os.path.join(root, "checks", module, "results.jsonl"), rows)
    for module, by_part in (parts or {}).items():
        os.makedirs(os.path.join(root, "checks", module, "parts"), exist_ok=True)
        for part, rows in by_part.items():
            R.write_jsonl(os.path.join(root, "checks", module, "parts", f"{part}.jsonl"), rows)
    if duplicates is not None:
        os.makedirs(os.path.join(root, "checks", "duplicate_check"), exist_ok=True)
        with open(os.path.join(root, "checks", "duplicate_check", "groups.json"), "w") as fh:
            json.dump(duplicates, fh)
    if final is not None:
        rev = os.path.join(root, "revisions", "r0001")
        os.makedirs(rev, exist_ok=True)
        for name in ("passed", "reject", "held", "review"):
            eps = [{"episode_index": e, **({"review": [{"kind": "task_verdict"}]} if name == "review" else {})}
                   for e in final.get(name, [])]
            with open(os.path.join(rev, f"{name}.json"), "w") as fh:
                json.dump({"episodes": eps}, fh)
        with open(os.path.join(rev, "commit.json"), "w") as fh:
            json.dump({}, fh)
    return str(root)


def _motion(idx, score):
    return {"episode_index": idx, "module": "motion_quality", "status": "ok", "findings": [],
            "assessed": ["ACT-1"], "unassessable": [], "details": {"jerk": score / 3, "score": score}}


def _task(idx, failed=False, rules=("video_single_pass_success",), error=False):
    findings = [{"code": "failure", "item": "TASK-4", "severity": "high", "scope": None, "readings": {},
                 "message_zh": "任务失败"}] if failed else []
    return {"episode_index": idx, "module": "task_success", "status": "error" if error else "ok",
            "findings": findings, "assessed": ["TASK-4"], "unassessable": [],
            "details": {"rules": list(rules)}}


def _args(golden, candidate, **kw):
    ns = C.build_parser().parse_args(["--golden", golden, "--candidate", candidate])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_identical_runs_pass(tmp_path):
    recs = {"motion_quality": [_motion(i, 0.5 + i / 10) for i in range(3)]}
    g = _run(tmp_path / "g", records=recs)
    c = _run(tmp_path / "c", records=recs)
    res = C.run_compare(_args(g, c))
    assert res["conclusion"] == "pass"
    assert res["modules"]["motion_quality"]["status"] == "pass"


def test_one_ulp_difference_fails_strict(tmp_path):
    g = _run(tmp_path / "g", records={"motion_quality": [_motion(0, 0.1)]})
    c = _run(tmp_path / "c", records={"motion_quality": [_motion(0, 0.1 + 2 ** -55)]})
    res = C.run_compare(_args(g, c))
    m = res["modules"]["motion_quality"]
    assert m["status"] == "fail" and m["different"] == 1 and res["conclusion"] == "fail"


def test_missing_episode_fails_strict(tmp_path):
    g = _run(tmp_path / "g", records={"motion_quality": [_motion(0, .5), _motion(1, .5)]})
    c = _run(tmp_path / "c", records={"motion_quality": [_motion(0, .5)]})
    assert C.run_compare(_args(g, c))["modules"]["motion_quality"]["only_in_golden"] == [1]


def test_task_success_differences_are_held_to_the_rate(tmp_path):
    base = [_task(i) for i in range(100)]
    one_off = [_task(i, failed=i == 0) for i in range(100)]            # 1 % differs
    three_off = [_task(i, failed=i <= 2) for i in range(100)]          # 3 % differs
    g = _run(tmp_path / "g", records={"task_success": base})
    res = C.run_compare(_args(g, _run(tmp_path / "c1", records={"task_success": one_off})))
    assert res["modules"]["task_success"]["status"] == "pass"
    assert res["modules"]["task_success"]["examples"][0]["candidate"][0] == "fail"
    assert C.run_compare(_args(g, _run(tmp_path / "c3", records={"task_success": three_off}))
                         )["modules"]["task_success"]["status"] == "fail"
    res = C.run_compare(_args(g, _run(tmp_path / "c1s", records={"task_success": one_off}), all_strict=True))
    assert res["modules"]["task_success"]["mode"] == "strict" and res["modules"]["task_success"]["different"] == 1


def test_rules_are_part_of_the_task_verdict(tmp_path):
    g = _run(tmp_path / "g", records={"task_success": [_task(0)]})
    c = _run(tmp_path / "c", records={"task_success": [_task(0, rules=("video_single_pass_uncertain",))]})
    assert C.run_compare(_args(g, c, max_diff_rate=0.5))["modules"]["task_success"]["different"] == 1


def test_final_lists_leave_out_error_episodes(tmp_path):
    g = _run(tmp_path / "g", records={"task_success": [_task(0), _task(1)]},
             final={"passed": [0, 1]})
    c = _run(tmp_path / "c", records={"task_success": [_task(0), _task(1, error=True)]},
             final={"passed": [0], "held": [1]})
    res = C.run_compare(_args(g, c))
    assert res["final"]["status"] == "pass" and res["final"]["excluded_errors"] == [1]


def test_final_lists_must_match(tmp_path):
    g = _run(tmp_path / "g", final={"passed": [0, 3], "reject": [2], "review": [3]})
    c = _run(tmp_path / "c", final={"passed": [0, 3], "reject": [2]})
    res = C.run_compare(_args(g, c))
    assert res["final"]["status"] == "fail" and res["final"]["review"]["missing_in_candidate"] == [3]


def test_an_adjudication_golden_compares_what_was_judged_again(tmp_path):
    """Only the task_success records of the parts after the first run's; a re-judge that failed on
    either side is left out and listed."""
    first = [_task(i) for i in range(3)]

    def adjudicated(root, again):
        current = {r["episode_index"]: r for r in first} | {r["episode_index"]: r for r in again}
        return _run(root, kind="v2-adjudication", records={"task_success": list(current.values())},
                    parts={"task_success": {"p0001": first, "p0002": again}}, final={"passed": [0, 1, 2]})

    g = adjudicated(tmp_path / "g", [_task(0, failed=True), _task(1)])
    c = adjudicated(tmp_path / "c", [_task(0, failed=True), _task(1, error=True)])
    res = C.run_compare(_args(g, c))
    ts = res["modules"]["task_success"]
    assert set(res["modules"]) == {"task_success"}
    assert ts["status"] == "pass" and ts["compared"] == 1 and ts["excluded_errors"] == [1]
    assert ts["judged_again"] == [0]
    c = adjudicated(tmp_path / "c2", [_task(0), _task(1)])
    assert C.run_compare(_args(g, c))["modules"]["task_success"]["different"] == 1


def test_replay_misses_fail(tmp_path):
    g = _run(tmp_path / "g")
    c = _run(tmp_path / "c", tape_mode="replay", misses=2)
    res = C.run_compare(_args(g, c))
    assert res["replay"] == {"status": "fail", "hits": 3, "misses": 2, "unused_on_tape": None}
    assert res["conclusion"] == "fail"


def test_duplicate_groups_must_match(tmp_path):
    g = _run(tmp_path / "g", duplicates={"groups": [[3, 7]], "action_collisions": [[3, 7]]})
    same = _run(tmp_path / "s", duplicates={"groups": [[7, 3]], "action_collisions": [[3, 7]]})
    assert C.run_compare(_args(g, same))["modules"]["duplicate_check"]["status"] == "pass"
    c = _run(tmp_path / "c", duplicates={"groups": [[3, 5, 7]], "action_collisions": [[3, 5, 7]]})
    res = C.run_compare(_args(g, c))["modules"]["duplicate_check"]
    assert res["status"] == "fail" and (res["golden_groups"], res["candidate_groups"]) == (1, 1)


def test_not_a_run_directory_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="not a v2 run directory"):
        C.RunDir(str(tmp_path / "empty"))
    assert C.main(["--golden", str(tmp_path / "empty"), "--candidate", str(tmp_path / "empty")]) == 2


def test_render_mentions_every_check(tmp_path):
    g = _run(tmp_path / "g", records={"motion_quality": [_motion(0, .5)]})
    text = C.render(C.run_compare(_args(g, g)))
    assert "motion_quality" in text and "conclusion: PASS" in text
