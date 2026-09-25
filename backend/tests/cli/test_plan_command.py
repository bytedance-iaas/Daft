"""curation plan, and check --plan-stage taking its gates from the plan (W3, doc 02 §3.2).

The planner itself (W6) is tested in ``tests/planner``; here: the command's
arguments and errors, and that a check stage runs with its plan stage's gates.
"""
from __future__ import annotations

import json
import os
import shutil

from .fakevlm_server import FakeVlmServer
from .pipeline import comparable, results, run

MODULES = ("timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,"
           "task_success,dedup,skill_profile")


def _plan(vlm_stage, tmp_path, *extra):
    out = str(tmp_path / "plan.json")
    res = run("plan", "--preflight", os.path.join(vlm_stage["base"], "preflight.json"),
              "--modules", MODULES, "--episodes", "0-7", "--out", out, *extra)
    return res, out


def _stage(plan: dict, stage_id: str) -> dict:
    return next(s for s in plan["stages"] if s["id"] == stage_id)


def test_the_default_plan_has_v1s_gates(vlm_stage, tmp_path):
    res, out = _plan(vlm_stage, tmp_path)
    assert res.rc == 0, res.doc
    with open(out, encoding="utf-8") as fh:
        assert json.load(fh) == res.doc                 # --out writes what was printed
    plan = res.doc
    assert plan["vlm_parallelism"] == 64
    assert _stage(plan, "vlm")["gates"] == {"episode": 32, "probe": 64, "endstate": 64,
                                            "arbitration": 32, "guard_caption": 32}
    assert _stage(plan, "vlm")["merge"] == {"strategy": "none", "groups": []}
    assert [s["id"] for s in plan["stages"]][:4] == ["autolabel", "numeric", "frame", "vlm"]


def test_the_smallest_limit_wins(vlm_stage, tmp_path):
    res, _ = _plan(vlm_stage, tmp_path, "--vlm-parallelism", "16",
                   "--task-vlm-parallelism", "8", "--cpu-cores", "4")
    assert res.rc == 0, res.doc
    assert res.doc["limits"]["vlm_parallelism"] == {"value": 8, "bound_by": "task"}
    assert res.doc["vlm_parallelism"] == 8
    assert _stage(res.doc, "vlm")["gates"]["probe"] == 8


def test_cpu_workers_are_the_cores_but_two_and_old_site_keys_only_warn(vlm_stage, tmp_path):
    """P4, D54: no site knob for the CPU any more; an old site file still plans, with a warning."""
    site = tmp_path / "site.yaml"
    site.write_text("concurrency: {cpu: 8, cpuMax: 16, vlmParallelism: 32}\n")
    res, _ = _plan(vlm_stage, tmp_path, "--cpu-cores", "32", "--site-config", site)
    assert res.rc == 0, res.doc
    assert res.doc["limits"]["cpu_concurrency"] == {"value": 30, "bound_by": "planner"}
    assert _stage(res.doc, "numeric")["concurrency"] == _stage(res.doc, "frame")["concurrency"] == 30
    assert res.doc["vlm_parallelism"] == 32
    assert any(e.get("level") == "warn" and "concurrency.cpu, concurrency.cpuMax ignored"
               in e.get("msg", "") for e in res.events), res.events
    res, _ = _plan(vlm_stage, tmp_path, "--cpu-cores", "32", "--task-cpu-concurrency", "10")
    assert res.doc["limits"]["cpu_concurrency"] == {"value": 10, "bound_by": "task"}


def test_bad_arguments_are_usage_errors(vlm_stage, tmp_path):
    res, _ = _plan(vlm_stage, tmp_path, "--episodes", "0-99")
    assert res.rc == 2 and "beyond" in res.doc["error"]["message"]
    res = run("plan", "--preflight", os.path.join(vlm_stage["base"], "plan.json"),
              "--modules", MODULES)
    assert res.rc == 2 and "not a preflight result" in res.doc["error"]["message"]
    res = run("plan", "--preflight", os.path.join(vlm_stage["base"], "preflight.json"),
              "--modules", "timestamp_check,no_such_module")
    assert res.rc == 2 and "cannot plan" in res.doc["error"]["message"]


def test_check_runs_with_its_plan_stage_gates(vlm_stage, tmp_path):
    res, plan = _plan(vlm_stage, tmp_path, "--task-vlm-parallelism", "4")
    assert res.rc == 0
    assert _stage(res.doc, "vlm")["gates"] == {"episode": 2, "probe": 4, "endstate": 4,
                                               "arbitration": 2, "guard_caption": 2}
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["base"], rd)
    args = ["check", "--modules", "task_success", "--input", vlm_stage["dataset"],
            "--run-dir", rd, "--episodes", vlm_stage["episodes"], "--vlm-model", "fake-vlm"]
    with FakeVlmServer(delay_s=0.02) as vlm:
        res = run(*args, "--vlm-endpoint", vlm.url, "--plan-stage", plan)
    assert res.rc == 0, res.doc
    assert 1 < vlm.max_in_flight <= 4 + 4 + 2 + 2       # the gates, each independent
    got, ref = results(rd, "task_success"), vlm_stage["reference"]
    assert {e: comparable(r) for e, r in got.items()} == \
        {e: comparable(r) for e, r in ref.items()}

    res = run("check", "--modules", "visual_quality,video_action_sync", "--input",
              vlm_stage["dataset"], "--run-dir", rd, "--episodes", "0",
              "--plan-stage", os.path.join(os.path.dirname(plan), "missing.json"))
    assert res.rc == 2
    stage = str(tmp_path / "vlm-stage.json")
    with open(plan, encoding="utf-8") as fh:
        vlm_stage_doc = _stage(json.load(fh), "vlm")
    with open(stage, "w", encoding="utf-8") as fh:
        json.dump(vlm_stage_doc, fh)
    res = run("check", "--modules", "visual_quality,video_action_sync", "--input",
              vlm_stage["dataset"], "--run-dir", rd, "--episodes", "0", "--plan-stage", stage)
    assert res.rc == 2 and "not for" in res.doc["error"]["message"]


def test_the_merge_proposal_of_the_vlm_stage(vlm_stage, tmp_path):
    """task_success declares no merge units (an evidence chain, D23): whatever the plan
    proposes, its requests go out one by one; a strategy nobody knows is refused."""
    res, plan = _plan(vlm_stage, tmp_path)
    with open(plan, encoding="utf-8") as fh:
        stage = _stage(json.load(fh), "vlm")
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["base"], rd)
    args = ["check", "--modules", "task_success", "--input", vlm_stage["dataset"],
            "--run-dir", rd, "--episodes", "3", "--vlm-model", "fake-vlm"]

    def with_merge(merge: dict) -> str:
        path = str(tmp_path / f"stage-{merge['strategy']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**stage, "merge": merge}, fh)
        return path

    with FakeVlmServer() as vlm:
        res = run(*args, "--vlm-endpoint", vlm.url,
                  "--plan-stage", with_merge({"strategy": "per_episode_multi_module",
                                              "groups": []}))
        assert res.rc == 0, res.doc
        assert any(e["kind"] == "log" and "declares merge units" in e["msg"]
                   for e in res.events)
        res = run(*args, "--vlm-endpoint", vlm.url,
                  "--plan-stage", with_merge({"strategy": "bogus", "groups": []}))
        assert res.rc == 2 and "unknown merge strategy" in res.doc["error"]["message"]
