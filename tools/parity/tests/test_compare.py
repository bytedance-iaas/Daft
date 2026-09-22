"""The compare command on hand-made dumps."""
from __future__ import annotations

import json
import os

from parity import compare as C
from parity import records as R


def _dump(root, *, records=None, autolabel=None, skill=None, final=None, dedup=None,
          tape_mode="record", misses=0):
    os.makedirs(os.path.join(root, "records"), exist_ok=True)
    with open(os.path.join(root, "dump.json"), "w") as fh:
        json.dump({"tape": {"mode": tape_mode, "hooks": {"misses": misses,
                                                         "hits": {"logical": 3}}}}, fh)
    for module, rows in (records or {}).items():
        R.write_jsonl(os.path.join(root, "records", f"{module}.jsonl"), rows)
    if autolabel is not None:
        R.write_jsonl(os.path.join(root, "autolabel.jsonl"), autolabel)
    for name, obj in (("skill_profile.json", skill), ("final.json", final),
                      ("dedup.json", dedup)):
        if obj is not None:
            with open(os.path.join(root, name), "w") as fh:
                json.dump(obj, fh)
    return str(root)


def _motion(idx, score):
    return R.make_record("motion_quality", idx, passed=None, score=score,
                         details={"jerk": score / 3})


def _task(idx, passed, rules=("review_confirms_success",), incidents=None):
    return R.make_record("task_success", idx, passed=passed, score=None,
                         details={"rules": list(rules)}, incidents=incidents)


def _args(golden, candidate, **kw):
    ns = C.build_parser().parse_args(["--golden", golden, "--candidate", candidate])
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_identical_dumps_pass(tmp_path):
    recs = {"motion_quality": [_motion(i, 0.5 + i / 10) for i in range(3)]}
    g = _dump(tmp_path / "g", records=recs)
    c = _dump(tmp_path / "c", records=recs)
    res = C.run_compare(_args(g, c))
    assert res["conclusion"] == "pass"
    assert res["modules"]["motion_quality"]["status"] == "pass"


def test_one_ulp_difference_fails_strict(tmp_path):
    g = _dump(tmp_path / "g", records={"motion_quality": [_motion(0, 0.1)]})
    c = _dump(tmp_path / "c", records={"motion_quality": [_motion(0, 0.1 + 2 ** -55)]})
    res = C.run_compare(_args(g, c))
    m = res["modules"]["motion_quality"]
    assert m["status"] == "fail" and m["different"] == 1 and res["conclusion"] == "fail"


def test_missing_episode_fails_strict(tmp_path):
    g = _dump(tmp_path / "g", records={"motion_quality": [_motion(0, .5), _motion(1, .5)]})
    c = _dump(tmp_path / "c", records={"motion_quality": [_motion(0, .5)]})
    assert C.run_compare(_args(g, c))["modules"]["motion_quality"]["only_in_golden"] == [1]


def test_task_success_rate_and_noise_floor(tmp_path):
    base = [_task(i, True) for i in range(100)]
    one_off = [_task(i, i != 0) for i in range(100)]            # 1 % differs
    three_off = [_task(i, i > 2) for i in range(100)]           # 3 % differs
    g = _dump(tmp_path / "g", records={"task_success": base})
    assert C.run_compare(_args(g, _dump(tmp_path / "c1", records={"task_success": one_off})
                               ))["modules"]["task_success"]["status"] == "pass"
    assert C.run_compare(_args(g, _dump(tmp_path / "c3", records={"task_success": three_off})
                               ))["modules"]["task_success"]["status"] == "fail"
    # with a noise floor of 0 %, even 1 % is too much; with 1 %, 1.5 % is allowed
    same = _dump(tmp_path / "n0", records={"task_success": base})
    res = C.run_compare(_args(g, _dump(tmp_path / "c1b", records={"task_success": one_off}),
                              noise_floor=same))
    assert res["modules"]["task_success"]["status"] == "fail"
    res = C.run_compare(_args(g, _dump(tmp_path / "c1c", records={"task_success": one_off}),
                              noise_floor=_dump(tmp_path / "n1",
                                                records={"task_success": one_off})))
    assert res["modules"]["task_success"]["noise_floor"]["rate"] == 0.01
    assert res["modules"]["task_success"]["status"] == "pass"


def test_rules_are_part_of_the_task_verdict(tmp_path):
    g = _dump(tmp_path / "g", records={"task_success": [_task(0, True)]})
    c = _dump(tmp_path / "c", records={"task_success": [_task(0, True, ("review_rescue",))]})
    assert C.run_compare(_args(g, c, max_diff_rate=0.5))["modules"]["task_success"][
        "different"] == 1


def test_final_lists_leave_out_error_episodes(tmp_path):
    err = [{"step": "probe", "cause": "timeout"}]
    g = _dump(tmp_path / "g", records={"task_success": [_task(0, True), _task(1, True)]},
              final={"passed": [0, 1], "reject": [], "review": [], "held": []})
    c = _dump(tmp_path / "c", records={"task_success": [_task(0, True),
                                                         _task(1, None, incidents=err)]},
              final={"passed": [0], "reject": [], "review": [], "held": [1]})
    res = C.run_compare(_args(g, c))
    assert res["final"]["status"] == "pass" and res["final"]["excluded_errors"] == [1]


def test_a_reject_v1_still_asks_about_is_left_out_of_review_and_listed(tmp_path):
    """v1 queues the abstention of a copy dedup removed (7); in v2 a rejected episode
    has no task question (D42): v1's review leaves out its own rejects."""
    g = _dump(tmp_path / "g", final={"passed": [0, 3], "reject": [2, 7],
                                     "review": [3, 7], "held": []})
    c = _dump(tmp_path / "c", final={"passed": [0, 3], "reject": [2, 7],
                                     "review": [3], "held": []})
    res = C.run_compare(_args(g, c))
    assert res["final"]["status"] == "pass"
    assert res["final"]["review"]["golden"] == 1
    assert res["final"]["review_excluded_rejects"] == [7]
    assert "rejected by v1" in C.render(res)
    # a question on a passed episode still has to match
    c = _dump(tmp_path / "c2", final={"passed": [0, 3], "reject": [2, 7],
                                      "review": [], "held": []})
    assert C.run_compare(_args(g, c))["final"]["review"]["missing_in_candidate"] == [3]


def test_replay_misses_fail(tmp_path):
    g = _dump(tmp_path / "g")
    c = _dump(tmp_path / "c", tape_mode="replay", misses=2)
    res = C.run_compare(_args(g, c))
    assert res["replay"] == {"status": "fail", "hits": 3, "misses": 2, "unused_on_tape": None}
    assert res["conclusion"] == "fail"


def test_dedup_pairs_must_match(tmp_path):
    g = _dump(tmp_path / "g", dedup={"dropped": [{"episode_index": 7, "duplicate_of": 3}],
                                     "action_collisions": [[3, 7]]})
    c = _dump(tmp_path / "c", dedup={"dropped": [{"episode_index": 3, "duplicate_of": 7}],
                                     "action_collisions": [[3, 7]]})
    assert C.run_compare(_args(g, c))["modules"]["dedup"]["status"] == "fail"


def test_skill_profile_verdict_ignores_family_names(tmp_path):
    def sp(fam):
        return {"ran": True, "assignments": [{"episode_index": 0, "family": fam}],
                "label_audit_queue": []}
    g = _dump(tmp_path / "g", skill=sp("grasp-and-transport"))
    c = _dump(tmp_path / "c", skill=sp("pick-and-place"))
    res = C.run_compare(_args(g, c))
    assert res["modules"]["skill_profile"]["status"] == "pass"
    res = C.run_compare(_args(g, c, all_strict=True))
    assert res["modules"]["skill_profile"]["status"] == "fail"


def test_render_mentions_every_check(tmp_path):
    g = _dump(tmp_path / "g", records={"motion_quality": [_motion(0, .5)]})
    text = C.render(C.run_compare(_args(g, g)))
    assert "motion_quality" in text and "conclusion: PASS" in text
