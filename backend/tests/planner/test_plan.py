"""build_plan(): the two blocks and their stages, autolabel, merge proposal, estimates (04 §3, design doc 17 §3)."""
from __future__ import annotations

import itertools
import json

import pytest

from curation.contracts import modules as C1
from curation.contracts import schemas
from curation.planner import PlanError, build_plan, derive_gates, validate_plan

from . import examples as X

ALL = list(C1.ids())
V1 = [m.id for m in C1.MODULES if not m.rides_on and m.id not in C1.native_ids()]   # v1's eight
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
    assert p["schema_version"] == "2.0"
    assert ids(p) == ["numeric", "frame", "dedup", "autolabel", "vlm", "final"]
    assert p["vlm_parallelism"] == 64
    assert p["limits"] == {"cpu_concurrency": {"value": 30, "bound_by": "planner"},
                           "vlm_parallelism": {"value": 64, "bound_by": "planner"}}
    assert stage(p, "numeric") == {
        "id": "numeric", "kind": "cpu", "command": "check", "block": "cpu", "concurrency": 30,
        "modules": ["timestamp_check", "kinematic_limits", "motion_quality"], "episodes": "selected"}
    assert stage(p, "frame") == {
        "id": "frame", "kind": "cpu", "command": "check", "block": "cpu", "after": "numeric",
        "concurrency": 30, "modules": ["visual_quality", "video_action_sync"], "episodes": "selected"}
    assert stage(p, "dedup") == {"id": "dedup", "kind": "cpu", "command": "check", "block": "cpu",
                                 "after": "frame", "concurrency": 1,
                                 "modules": ["dedup"], "episodes": "selected"}
    assert stage(p, "autolabel") == {"id": "autolabel", "kind": "vlm", "command": "autolabel",
                                     "block": "vlm", "episodes": "unlabeled", "gates": {"caption": 32}}
    vlm = stage(p, "vlm")
    assert (vlm["block"], vlm["after"], vlm["episodes"]) == ("vlm", "autolabel", "selected")
    assert vlm["modules"] == ["task_success", "camera_defects"] and "hard_gates" not in vlm
    assert vlm["gates"] == {"episode": 32, "probe": 64, "endstate": 64, "arbitration": 32,
                            "guard_caption": 32}
    assert vlm["merge"] == {"strategy": "none", "groups": []}
    assert stage(p, "final") == {"id": "final", "kind": "aggregate", "command": "aggregate", "phase": "final"}


def test_blocks_never_chain_into_each_other():
    """design doc 17 §3.3: ``after`` names a stage of the same block; every stage takes the selection."""
    for chosen in (ALL, V1, ["timestamp_check", "task_success"], ["dedup"]):
        p = plan(chosen, preflight=X.preflight(64, without_task=5))
        by_id = {s["id"]: s for s in p["stages"]}
        for s in p["stages"]:
            if s["kind"] == "aggregate":
                continue
            assert s["id"] in C1.BLOCKS[s["block"]]
            if "after" in s:
                assert by_id[s["after"]]["block"] == s["block"]
            assert s["episodes"] in ("selected", "unlabeled")
            assert "full_set" not in s, "D70: a plan no longer marks a whole-set stage"


def test_the_eef_module_is_in_the_vlm_block():
    """D49 / design doc 12 D-E11: the EEF module joins the vlm stage next to task_success, and like every
    stage it takes the whole selection (D57: nothing upstream filters it)."""
    p = plan(preflight=X.preflight(200, without_task=88))
    assert ids(p) == ["integrity", "numeric", "frame", "dedup", "autolabel", "vlm", "final"]
    vlm = stage(p, "vlm")
    assert vlm["modules"] == ["eef_video_consistency", "task_success", "camera_defects"] \
        and vlm["episodes"] == "selected" and "hard_gates" not in vlm
    only = plan(["eef_video_consistency"])
    assert ids(only) == ["vlm", "final"] and "after" not in stage(only, "vlm")


def test_the_data_integrity_module_is_the_cpu_blocks_first_stage():
    """design doc 14 §2.2: its own stage before numeric, a CPU stage (mostly I/O) with the plan's
    concurrency; without it the CPU block starts at numeric."""
    p = plan()
    assert stage(p, "integrity") == {"id": "integrity", "kind": "cpu", "command": "check", "block": "cpu",
                                     "concurrency": 30, "modules": ["data_integrity"], "episodes": "selected"}
    assert stage(p, "numeric")["after"] == "integrity"
    assert any("decode_test" in n for n in p["estimates"]["notes"])
    without = plan(V1)
    assert "integrity" not in ids(without) and "after" not in stage(without, "numeric")


def test_unselected_modules_and_empty_stages_disappear():
    p = plan(["visual_quality", "task_success"])
    assert ids(p) == ["frame", "vlm", "final"]
    assert stage(p, "frame")["modules"] == ["visual_quality"] and "after" not in stage(p, "frame")
    assert "after" not in stage(p, "vlm")                 # another block: never after frame


def test_after_skips_missing_stages():
    assert "after" not in stage(plan(["timestamp_check", "task_success"]), "vlm")
    p = plan(["motion_quality", "video_action_sync", "dedup"])
    assert stage(p, "frame")["after"] == "numeric" and stage(p, "dedup")["after"] == "frame"
    assert stage(plan(["timestamp_check", "dedup"]), "dedup")["after"] == "numeric"
    assert "after" not in stage(plan(["dedup"]), "dedup")


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
    assert ids(p) == ["final"]


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


def test_a_dataset_with_its_own_indices_is_selected_by_them():
    """F12.8: the preflight's episode_indices (a subset that keeps its source's numbers) are what all
    and an explicit list pick from; 0..count-1 is not."""
    from curation.planner import dataset_episodes

    pf = X.preflight(5)
    pf["dataset"]["episode_indices"] = "2604-2606,3000,3010"
    schemas.validate("cli/preflight.schema.json", pf)
    assert dataset_episodes(pf["dataset"]) == [2604, 2605, 2606, 3000, 3010]
    assert dataset_episodes(X.preflight(3)["dataset"]) == [0, 1, 2]
    plan(["timestamp_check"], preflight=pf)                                  # all of them
    plan(["timestamp_check"], preflight=pf, episodes=[2605, 3010])
    with pytest.raises(PlanError, match=r"episode 0 is not in the dataset's 5 episodes \(2604, 2605, 2606, 3000, 3010\)"):
        build_plan(pf, ["timestamp_check"], [0, 2605])
    with pytest.raises(PlanError, match=r"episode 64 is not in 0\.\.63"):
        build_plan(X.preflight(64), ["timestamp_check"], [64])


# ---------------------------------------------------------------- autolabel

def test_autolabel_only_with_unlabeled_episodes_and_task_success():
    labelled, unlabelled = X.preflight(64), X.preflight(64, without_task=10)
    assert "autolabel" not in ids(plan(["task_success"], preflight=labelled))
    assert ids(plan(["task_success"], preflight=unlabelled))[0] == "autolabel"
    # v1 captions unlabeled episodes only when task_success runs
    # those captions and captions the rest itself (run.py), so it does not trigger autolabel
    assert "autolabel" not in ids(plan(["dedup"], preflight=unlabelled))
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
    assert vlm["modules"] == ["task_success", "camera_defects", "example_grasp", "example_table"]
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


def test_dedup_is_a_streaming_segment_of_the_cpu_block():
    """design doc 17 §3.2 (D70): it reads every selected episode, one at a time, after the frame
    segment - no ``full_set``, so nothing in the block waits for the whole selection."""
    alone = stage(plan(["dedup"]), "dedup")
    assert (alone["episodes"], alone["block"]) == ("selected", "cpu")
    assert "after" not in alone, "selected on its own it is the block's first segment"
    with_frame = stage(plan(["visual_quality", "dedup"]), "dedup")
    assert with_frame["after"] == "frame"
    assert alone["concurrency"] == with_frame["concurrency"] == 1
    assert "full_set" not in alone and "full_set" not in with_frame


# ---------------------------------------------------------------- estimates

def test_estimates_follow_v1_call_graph():
    pf = X.preflight(10, cameras=("a", "b", "c"))
    p = plan(["task_success"], preflight=pf)
    assert p["estimates"]["vlm_requests"] == 10                     # one judgement per episode (D71)
    assert p["estimates"]["wall_clock_s"] > 0
    p = plan(["task_success"], preflight=X.preflight(10, without_task=4))
    assert p["estimates"]["vlm_requests"] == 4 + 10                  # autolabel, then one judgement each
    p = plan(["example_grasp", "example_table"], registry=X.REGISTRY, preflight=X.preflight(10))
    assert p["estimates"]["vlm_requests"] == 10                      # merged: one request per episode
    p = plan(["example_grasp", "example_table"], registry=X.REGISTRY, preflight=X.preflight(10),
             site_config={"vlm": {"merge": {"enabled": False}}})
    assert p["estimates"]["vlm_requests"] == 20
    assert plan(["timestamp_check"])["estimates"]["vlm_requests"] == 0


def test_the_wall_clock_is_the_longer_block():
    """The blocks run side by side: CPU checks of many episodes do not add to the model's time."""
    pf = X.preflight(10, cameras=("a", "b", "c"))
    vlm = plan(["task_success"], preflight=pf)["estimates"]["wall_clock_s"]
    both = plan(["timestamp_check", "task_success"], preflight=pf)["estimates"]["wall_clock_s"]
    assert both == vlm                                   # 10 parquet checks hide under the model calls
    cpu = plan(["visual_quality"], preflight=X.preflight(10_000))["estimates"]["wall_clock_s"]
    assert plan(["visual_quality", "task_success"], preflight=X.preflight(10_000))["estimates"]["wall_clock_s"] \
        >= cpu


def test_plan_is_deterministic_and_json():
    a = plan(preflight=X.preflight(64, without_task=3))
    b = plan(preflight=X.preflight(64, without_task=3))
    assert json.dumps(a, sort_keys=False) == json.dumps(b, sort_keys=False)


def test_a_rider_runs_with_its_host_and_never_alone():
    """Registry 1.14: camera_defects is answered inside task_success's requests: it is in the vlm
    stage whenever task_success is, asked for or not, costs no request, and alone it is left out."""
    p = plan(["task_success"])
    assert stage(p, "vlm")["modules"] == ["task_success", "camera_defects"]
    assert plan(["task_success", "camera_defects"]) == p
    assert stage(plan(["timestamp_check", "camera_defects"]), "numeric")["modules"] == ["timestamp_check"]
    assert "vlm" not in ids(plan(["timestamp_check", "camera_defects"]))
