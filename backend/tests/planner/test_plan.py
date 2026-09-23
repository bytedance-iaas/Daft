"""build_plan(): stages, survivors, autolabel, merge proposal, estimates (04 §3)."""
from __future__ import annotations

import itertools
import json

import pytest

from curation.contracts import modules as C1
from curation.contracts import schemas
from curation.planner import PlanError, build_plan, derive_gates, validate_plan

from . import examples as X

ALL = list(C1.ids())
V1 = [m.id for m in C1.MODULES if m.affects_dataset_verdict]          # the design example's eight
VLM_MODULES = [m.id for m in C1.MODULES if "vlm" in m.needs]


def plan(modules=ALL, **kw):
    kw.setdefault("limits", {"cpu_cores": 32})
    pf = kw.pop("preflight", None) or X.preflight()
    p = build_plan(pf, modules, **kw)
    validate_plan(p)
    return p


def stage(p, sid):
    found = [s for s in p["stages"] if s["id"] == sid]
    return found[0] if found else None


def ids(p):
    return [s["id"] for s in p["stages"]]


def test_example_preflight_is_valid():
    schemas.validate("cli/preflight.schema.json", X.preflight())
    schemas.validate("cli/preflight.schema.json", X.preflight(supported=False))


def test_full_plan_matches_the_design_example():
    p = plan(V1, preflight=X.preflight(200, without_task=88))
    assert ids(p) == ["autolabel", "numeric", "frame", "vlm", "verdict", "dedup", "profile_vlm", "final"]
    assert p["vlm_parallelism"] == 64
    assert p["limits"] == {"cpu_concurrency": {"value": 8, "bound_by": "planner"},
                           "vlm_parallelism": {"value": 64, "bound_by": "planner"}}
    assert stage(p, "autolabel") == {"id": "autolabel", "kind": "vlm", "command": "autolabel",
                                     "episodes": "unlabeled", "gates": {"caption": 32}}
    assert stage(p, "numeric") == {
        "id": "numeric", "kind": "cpu", "command": "check", "concurrency": 8,
        "modules": ["timestamp_check", "kinematic_limits", "motion_quality"],
        "episodes": "selected", "hard_gates": ["timestamp_check", "kinematic_limits"]}
    assert stage(p, "frame") == {
        "id": "frame", "kind": "cpu", "command": "check", "concurrency": 8,
        "modules": ["visual_quality", "video_action_sync"], "episodes": "survivors:numeric",
        "hard_gates": ["video_action_sync"]}
    vlm = stage(p, "vlm")
    assert vlm["modules"] == ["task_success"] and vlm["episodes"] == "survivors:frame"
    assert vlm["gates"] == {"episode": 32, "probe": 64, "endstate": 64, "arbitration": 32,
                            "guard_caption": 32}
    assert vlm["merge"] == {"strategy": "none", "groups": []}
    assert stage(p, "verdict") == {"id": "verdict", "kind": "aggregate", "command": "aggregate",
                                   "phase": "funnel"}
    assert stage(p, "dedup") == {"id": "dedup", "kind": "cpu", "command": "check",
                                 "concurrency": 1, "modules": ["dedup"], "episodes": "keep"}
    profile = stage(p, "profile_vlm")
    assert profile["episodes"] == "keep-minus-duplicates"
    assert profile["gates"] == {"caption": 32, "llm": 16, "audit": 16}
    assert stage(p, "final")["phase"] == "final"


def test_advisory_modules_run_on_every_selected_episode_outside_the_funnel():
    """Registry 1.4 / design doc 12 §11.1: the EEF pair never joins a funnel stage."""
    p = plan(preflight=X.preflight(200, without_task=88))
    assert ids(p) == ["autolabel", "numeric", "frame", "vlm", "advisory_frame", "advisory_vlm", "verdict",
                      "dedup", "profile_vlm", "final"]
    assert stage(p, "frame")["modules"] == ["visual_quality", "video_action_sync"]
    assert stage(p, "vlm")["modules"] == ["task_success"]
    assert stage(p, "advisory_frame") == {"id": "advisory_frame", "kind": "cpu", "command": "check",
                                          "modules": ["eef_video_consistency"], "episodes": "selected",
                                          "concurrency": 8}
    rev = stage(p, "advisory_vlm")
    assert rev["modules"] == ["eef_video_review"] and rev["episodes"] == "selected"
    assert "hard_gates" not in stage(p, "advisory_frame")
    only = plan(["eef_video_consistency"])
    assert ids(only) == ["advisory_frame", "verdict", "final"]


def test_unselected_modules_and_empty_stages_disappear():
    p = plan(["visual_quality", "task_success"])
    assert ids(p) == ["frame", "vlm", "verdict", "final"]
    assert stage(p, "frame")["episodes"] == "selected"
    assert stage(p, "frame")["modules"] == ["visual_quality"] and "hard_gates" not in stage(p, "frame")
    assert stage(p, "vlm")["episodes"] == "survivors:frame"


def test_survivors_chain_skips_missing_stages():
    assert stage(plan(["timestamp_check", "task_success"]), "vlm")["episodes"] == "survivors:numeric"
    assert stage(plan(["task_success"]), "vlm")["episodes"] == "selected"
    p = plan(["motion_quality", "video_action_sync"])
    assert stage(p, "frame")["episodes"] == "survivors:numeric"
    assert "hard_gates" not in stage(p, "numeric")


def test_module_order_follows_the_registry_not_the_caller():
    p = plan(["motion_quality", "timestamp_check", {"id": "kinematic_limits"}, "timestamp_check"])
    assert stage(p, "numeric")["modules"] == ["timestamp_check", "kinematic_limits", "motion_quality"]


def test_unsupported_modules_stay_out_and_are_explained():
    pf = X.preflight(availability={"kinematic_limits": {
        "availability": "unsupported",
        "reason": "robot_type 'umi_dual_handheld_gripper' is not in the embodiment registry"}})
    p = plan(["timestamp_check", "kinematic_limits"], preflight=pf)
    assert stage(p, "numeric")["modules"] == ["timestamp_check"]
    assert any("kinematic_limits skipped" in n and "umi_dual_handheld_gripper" in n
               for n in p["estimates"]["notes"])
    p = plan(["kinematic_limits"], preflight=pf)          # the task does not fail (05 §4)
    assert ids(p) == ["verdict", "final"]


def test_needs_input_modules_are_planned_with_a_note():
    pf = X.preflight(availability={"kinematic_limits": {
        "availability": "needs_input", "reason": "robot_type not found",
        "input_hint": {"field": "embodiment_id", "options": ["franka"]}}})
    p = plan(["kinematic_limits"], preflight=pf)
    assert stage(p, "numeric")["modules"] == ["kinematic_limits"]
    assert any("embodiment_id" in n for n in p["estimates"]["notes"])


def test_unsupported_format_cannot_be_planned():
    with pytest.raises(PlanError, match="not supported"):
        build_plan(X.preflight(supported=False), ["timestamp_check"])


@pytest.mark.parametrize("bad", [["nope"], [], "task_success"])
def test_bad_modules(bad):
    with pytest.raises(PlanError):
        build_plan(X.preflight(), bad)


@pytest.mark.parametrize("episodes", [[64], [-1], [True], []])
def test_bad_episodes(episodes):
    with pytest.raises(PlanError):
        build_plan(X.preflight(64), ["timestamp_check"], episodes)


# ---------------------------------------------------------------- autolabel

def test_autolabel_only_with_unlabeled_episodes_and_task_success():
    labelled, unlabelled = X.preflight(64), X.preflight(64, without_task=10)
    assert "autolabel" not in ids(plan(["task_success"], preflight=labelled))
    assert ids(plan(["task_success"], preflight=unlabelled))[0] == "autolabel"
    # v1 captions unlabeled episodes only when task_success runs; skill_profile reuses
    # those captions and captions the rest itself (run.py), so it does not trigger autolabel
    assert "autolabel" not in ids(plan(["skill_profile"], preflight=unlabelled))
    assert "autolabel" not in ids(plan(["timestamp_check", "visual_quality"], preflight=unlabelled))


def test_autolabel_with_a_subset_of_episodes():
    pf = X.preflight(64, without_task=10)
    # exact knowledge: none of the selected episodes lacks a task text
    p = plan(["task_success"], preflight=pf, episodes=[0, 1, 2], unlabeled_episodes=[40, 41])
    assert "autolabel" not in ids(p)
    p = plan(["task_success"], preflight=pf, episodes=[0, 1, 40], unlabeled_episodes=[40, 41])
    assert ids(p)[0] == "autolabel"
    # only totals known: plan it, and say the count is an estimate
    p = plan(["task_success"], preflight=pf, episodes=range(32))
    assert ids(p)[0] == "autolabel"
    assert any("estimated from the preflight" in n for n in p["estimates"]["notes"])


# ---------------------------------------------------------------- D23: existing modules never merge

def test_existing_vlm_modules_always_plan_merge_none():
    """Acceptance: for every selection of the real modules, VLM stages say strategy none."""
    pf = X.preflight(64, without_task=5)
    for site in (None, {"vlm": {"merge": {"enabled": True}}}):
        for r in range(1, len(ALL) + 1):
            for chosen in itertools.combinations(ALL, r):
                p = build_plan(pf, chosen, limits={"cpu_cores": 32}, site_config=site)
                validate_plan(p)
                for s in p["stages"]:
                    if s["kind"] == "vlm" and s["command"] == "check":
                        assert s["merge"] == {"strategy": "none", "groups": []}, (chosen, s)


def test_registry_declares_no_merge_units():
    assert all(m.merge_units is None for m in C1.MODULES if m.id in VLM_MODULES)


# ---------------------------------------------------------------- merge proposal (example modules)

def test_example_modules_are_proposed_for_merging():
    p = plan(["task_success", "example_grasp", "example_table"], registry=X.REGISTRY)
    vlm = stage(p, "vlm")
    assert vlm["modules"] == ["task_success", "example_grasp", "example_table"]
    assert vlm["merge"] == {"strategy": "per_episode_multi_module",
                            "groups": [{"modules": ["example_grasp", "example_table"],
                                        "frame_policy": X.FRAME_POLICY.key}]}
    assert X.FRAME_POLICY.key == "interval:0.5/max_side:448/max_cams:4/linspace:8"
    groups = [g for s in p["stages"] for g in s.get("merge", {}).get("groups", [])]
    assert not any(m in g["modules"] for g in groups for m in VLM_MODULES)


def test_merge_can_be_switched_off_in_the_plan():
    site = {"vlm": {"merge": {"enabled": False}}}
    p = plan(["example_grasp", "example_table"], registry=X.REGISTRY, site_config=site)
    assert stage(p, "vlm")["merge"] == {"strategy": "none", "groups": []}
    assert any("switched off" in n for n in p["estimates"]["notes"])


def test_a_plain_callable_merge_units_is_planned_without_merging():
    import dataclasses

    plain = dataclasses.replace(X.EXAMPLE_GRASP, id="example_plain",
                                merge_units=lambda ep, ctx=None: [])
    p = plan(["example_plain", "example_table"], registry=X.REGISTRY + (plain,),
             preflight=X.preflight(10))
    assert stage(p, "vlm")["merge"] == {"strategy": "none", "groups": []}
    assert p["estimates"]["vlm_requests"] == 20                     # one question each, not merged


def test_one_mergeable_module_has_nothing_to_merge_with():
    p = plan(["example_grasp"], registry=X.REGISTRY)
    assert stage(p, "vlm")["merge"] == {"strategy": "none", "groups": []}


# ---------------------------------------------------------------- limits and gates in the plan

def test_caps_flow_into_the_plan():
    p = plan(limits={"cpu_cores": 32, "cpu_concurrency": 2, "vlm_parallelism": 16})
    assert p["limits"]["cpu_concurrency"] == {"value": 2, "bound_by": "task"}
    assert p["limits"]["vlm_parallelism"] == {"value": 16, "bound_by": "task"}
    assert p["vlm_parallelism"] == 16
    g = derive_gates(16)
    assert stage(p, "vlm")["gates"] == {k: g[k] for k in
                                        ("episode", "probe", "endstate", "arbitration", "guard_caption")}
    assert stage(p, "profile_vlm")["gates"] == {k: g[k] for k in ("caption", "llm", "audit")}
    assert stage(p, "numeric")["concurrency"] == stage(p, "frame")["concurrency"] == 2
    assert stage(p, "dedup")["concurrency"] == 1                 # never above 1 (05 §1)


def test_dedup_ignores_every_concurrency_setting():
    p = plan(["dedup"], limits={"cpu_cores": 256, "cpu_concurrency": 64},
             site_config={"concurrency": {"cpu": 32}})
    assert stage(p, "dedup")["concurrency"] == 1


def test_running_tasks_split_n():
    p = plan(limits={"cpu_cores": 32, "model_parallelism": 64, "running_tasks": 2})
    assert p["limits"]["vlm_parallelism"] == {"value": 32, "bound_by": "running_tasks"}


def test_site_gate_overrides_reach_the_plan():
    p = plan(["task_success"], site_config={"vlm": {"gates": {"probe": 96}}})
    assert stage(p, "vlm")["gates"]["probe"] == 96


def test_profile_reads_keep_without_dedup():
    assert stage(plan(["skill_profile"]), "profile_vlm")["episodes"] == "keep"


# ---------------------------------------------------------------- estimates

def test_estimates_follow_v1_call_graph():
    pf = X.preflight(10, cameras=("a", "b", "c"))
    p = plan(["task_success"], preflight=pf)
    assert p["estimates"]["vlm_requests"] == 10 * (8 + 2 * 3)       # probes + two questions per camera
    assert p["estimates"]["wall_clock_s"] > 0
    p = plan(["task_success", "skill_profile"], preflight=X.preflight(10, without_task=4))
    assert p["estimates"]["vlm_requests"] == 4 + 10 * 14 + 6         # autolabel, judge, profile captions
    p = plan(["example_grasp", "example_table"], registry=X.REGISTRY, preflight=X.preflight(10))
    assert p["estimates"]["vlm_requests"] == 10                      # merged: one request per episode
    p = plan(["example_grasp", "example_table"], registry=X.REGISTRY, preflight=X.preflight(10),
             site_config={"vlm": {"merge": {"enabled": False}}})
    assert p["estimates"]["vlm_requests"] == 20
    assert plan(["timestamp_check"])["estimates"]["vlm_requests"] == 0


def test_plan_is_deterministic_and_json():
    a = plan(preflight=X.preflight(64, without_task=3))
    b = plan(preflight=X.preflight(64, without_task=3))
    assert json.dumps(a, sort_keys=False) == json.dumps(b, sort_keys=False)
