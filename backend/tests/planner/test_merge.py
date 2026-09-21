"""Merge framework primitives: policies, units, composition, splitting, strategies (04 §4.2)."""
from __future__ import annotations

import json

import pytest

from curation.planner import (FramePolicy, MergeLimits, MergeUnit, NoMerge,
                              PerEpisodeMultiModule, compose_merged_prompt, merge_enabled,
                              split_answer, strategy_for_stage, strategy_named)
from curation.planner.merge import estimate_tokens, pack

from . import examples as X


def unit(ep=0, module="example_grasp", prompt=X.GRASP_PROMPT, policy=X.FRAME_POLICY, **kw):
    return MergeUnit(ep, module, policy, prompt, parser=str, call_kind=kw.pop("call_kind", "caption"),
                     **kw)


# ---------------------------------------------------------------- FramePolicy / MergeUnit

def test_frame_policy_key_and_images():
    p = FramePolicy("interval", 0.5, 448, 4, "linspace:8")
    assert p.key == "interval:0.5/max_side:448/max_cams:4/linspace:8"
    assert (p.frames_per_camera(), p.max_images()) == (8, 32)
    full = FramePolicy("full_rate", None, 448, 4, "all")
    assert full.key == "full_rate/max_side:448/max_cams:4/all"
    assert full.max_images() is None
    assert p == FramePolicy("interval", 0.5, 448, 4, "linspace:8") and p != full


def test_policy_keys_are_exact():
    a = FramePolicy("interval", 0.5, 448, 4, "linspace:8")
    b = FramePolicy("interval", 0.5000001, 448, 4, "linspace:8")
    assert a != b and a.key != b.key
    one, one_f = (FramePolicy("interval", v, 448, 4, "linspace:8") for v in (1, 1.0))
    assert one == one_f and one.key == one_f.key == "interval:1.0/max_side:448/max_cams:4/linspace:8"
    with pytest.raises(ValueError):
        FramePolicy("interval", 0.5, 448, 4, "linspace:8/max_cams:1")
    groups = PerEpisodeMultiModule().group([unit(module="a1", policy=a), unit(module="a2", policy=b)],
                                           MergeLimits())
    assert [g.modules for g in groups] == [("a1",), ("a2",)]


@pytest.mark.parametrize("args", [("sparse", 0.5, 448, 4, "linspace:8"), ("interval", None, 448, 4, "x"),
                                  ("interval", 0, 448, 4, "x"), ("full_rate", 0.5, 448, 4, "x"),
                                  ("interval", 0.5, 0, 4, "x"), ("interval", 0.5, 448, 4, "")])
def test_frame_policy_rejects_nonsense(args):
    with pytest.raises(ValueError):
        FramePolicy(*args)


def test_unit_validation():
    assert unit().key == (0, "example_grasp", "")
    for bad in (dict(ep=-1), dict(module="Bad-Id"), dict(prompt="  "), dict(call_kind="vision"),
                dict(call_kind="merged"), dict(max_tokens=0)):
        with pytest.raises(ValueError):
            unit(**bad)
    with pytest.raises(ValueError):
        MergeUnit(0, "m", X.FRAME_POLICY, "p", parser=None, call_kind="caption")


def test_declared_units_check_what_modules_return():
    units = X.EXAMPLE_GRASP.merge_units(7)
    assert [u.key for u in units] == [(7, "example_grasp", "")]
    assert units[0].prompt_part == X.GRASP_PROMPT and units[0].call_kind == "caption"
    from curation.planner import DeclaredMergeUnits

    liar = DeclaredMergeUnits(X.FRAME_POLICY, "caption", lambda ep, ctx: [unit(ep=ep + 1)])
    with pytest.raises(ValueError, match="episode"):
        liar(3)
    other = FramePolicy("interval", 1.0, 448, 4, "linspace:8")
    drifted = DeclaredMergeUnits(X.FRAME_POLICY, "caption", lambda ep, ctx: [unit(ep=ep, policy=other)])
    with pytest.raises(ValueError, match="declared"):
        drifted(3)


# ---------------------------------------------------------------- composition and splitting

def test_merged_prompt_keeps_every_prompt_verbatim():
    units = [unit(module="example_grasp"), unit(module="example_table", prompt=X.TABLE_PROMPT)]
    text = compose_merged_prompt(units, 24)
    assert text.startswith("You are given 24 frames from one robot demonstration episode.\n"
                           "Complete ALL of the following tasks. Return STRICT JSON with exactly\n"
                           'these top-level keys: "task_1", "task_2".')
    assert f"Task 1 (example_grasp): {X.GRASP_PROMPT}" in text
    assert f"Task 2 (example_table): {X.TABLE_PROMPT}" in text
    assert text.index("Task 1") < text.index("Task 2")
    assert "You are given frames" in compose_merged_prompt(units, None)
    with pytest.raises(ValueError):
        compose_merged_prompt(units[:1], 24)


@pytest.mark.parametrize("content,expected", [
    ('{"task_1": "yes", "task_2": "no"}', {"task_1": "yes", "task_2": "no"}),
    ('```json\n{"task_1": "yes", "task_2": "no"}\n```', {"task_1": "yes", "task_2": "no"}),
    ('<think>task_1 is {hard}</think>{"task_1": "yes"}', {"task_1": "yes"}),
    ('Sure! {"task_1": 73, "task_2": {"x": 1}} Hope it helps.', {"task_1": "73", "task_2": '{"x": 1}'}),
    ('{"task_1": null, "task_2": "no", "task_9": "x"}', {"task_2": "no"}),
    ('{"task_1": "是"}', {"task_1": "是"}),
])
def test_split_answer(content, expected):
    assert split_answer(content, ["task_1", "task_2"]) == expected


@pytest.mark.parametrize("content", ["yes, no", "[1, 2]", "", "{broken json", "```\n```"])
def test_split_answer_rejects_non_objects(content):
    assert split_answer(content, ["task_1"]) is None


# ---------------------------------------------------------------- limits and strategies

def test_estimate_tokens_over_counts():
    assert estimate_tokens("abcd" * 30) >= 30                 # ~4 ASCII chars per token
    assert estimate_tokens("任务" * 10) >= 20                  # ~1 CJK char per token


def test_limits_check():
    two = [unit(module="a1"), unit(module="a2")]
    assert MergeLimits().check(two, 24) is None
    assert MergeLimits(max_units=1).check(two, 24) == "max_units"
    assert MergeLimits(max_images=10).check(two, 24) == "max_images"
    assert MergeLimits(max_prompt_tokens=100).check(two, 24) == "max_prompt_tokens"
    assert MergeLimits(context_window=20000, image_tokens=1000).check(two, 24) == "context_window"
    with pytest.raises(ValueError):
        MergeLimits(max_units=0)


def test_none_strategy_sends_everything_alone():
    units = X.units_for([0, 1])
    groups = NoMerge().group(units, MergeLimits())
    assert [len(g.units) for g in groups] == [1, 1, 1, 1]
    assert {g.receipt for g in groups} == {"single"}


def test_per_episode_groups_same_policy_units():
    units = X.units_for([0, 1, 2])
    groups = PerEpisodeMultiModule().group(units, MergeLimits())
    assert [(g.units[0].episode_index, g.modules, g.receipt) for g in groups] == [
        (ep, ("example_grasp", "example_table"), "merged") for ep in (0, 1, 2)]


def test_group_rules():
    from curation.planner import MergeGroup

    other = FramePolicy("interval", 1.0, 448, 4, "linspace:8")
    with pytest.raises(ValueError):
        MergeGroup(())
    with pytest.raises(ValueError, match="frame policy"):
        MergeGroup((unit(module="a1"), unit(module="a2", policy=other)))
    across = MergeGroup((unit(ep=3), unit(ep=1, module="a2"), unit(ep=3, module="a3")))
    assert across.episodes == (3, 1) and across.receipt == "merged"


def test_per_episode_keeps_different_policies_apart():
    other = FramePolicy("interval", 1.0, 448, 4, "linspace:8")
    units = [unit(module="a1"), unit(module="a2", policy=other), unit(module="a3")]
    groups = PerEpisodeMultiModule().group(units, MergeLimits())
    assert [g.modules for g in groups] == [("a1", "a3"), ("a2",)]


def test_over_limit_groups_are_split_in_order():
    units = [unit(module=f"m{i}") for i in range(5)]
    groups = pack(units, MergeLimits(max_units=2))
    assert [g.modules for g in groups] == [("m0", "m1"), ("m2", "m3"), ("m4",)]
    assert all(g.split and g.reason == "max_units" and g.receipt == "split" for g in groups)
    fits = pack(units, MergeLimits(max_units=5))
    assert len(fits) == 1 and fits[0].receipt == "merged"
    # the prompt budget splits too: two prompts fit, three do not
    budget = estimate_tokens(compose_merged_prompt(units[:2], 32))
    groups = pack(units[:3], MergeLimits(max_prompt_tokens=budget))
    assert [len(g.units) for g in groups] == [2, 1] and groups[0].reason == "max_prompt_tokens"


def test_plan_proposal_restricts_what_merges():
    units = X.units_for([0]) + [unit(module="stray")]
    stage = {"merge": {"strategy": "per_episode_multi_module",
                       "groups": [{"modules": ["example_grasp", "example_table"],
                                   "frame_policy": X.FRAME_POLICY.key}]}}
    groups = strategy_for_stage(stage).group(units, MergeLimits())
    assert [g.modules for g in groups] == [("example_grasp", "example_table"), ("stray",)]
    wrong_policy = {"merge": {"strategy": "per_episode_multi_module",
                              "groups": [{"modules": ["example_grasp", "example_table"],
                                          "frame_policy": "full_rate/max_side:448/max_cams:4/all"}]}}
    assert all(len(g.units) == 1 for g in strategy_for_stage(wrong_policy).group(units, MergeLimits()))
    assert isinstance(strategy_for_stage({"merge": {"strategy": "none", "groups": []}}), NoMerge)
    assert isinstance(strategy_for_stage(None), NoMerge)
    assert isinstance(strategy_for_stage(stage, enabled=False), NoMerge)
    with pytest.raises(ValueError):
        strategy_for_stage({"merge": {"strategy": "cross_episode", "groups": []}})


def test_strategy_names():
    assert strategy_named("none").name == "none"
    assert strategy_named("per_episode_multi_module").name == "per_episode_multi_module"
    with pytest.raises(ValueError):
        strategy_named("cross_episode")


def test_kill_switch_reads_the_config_path():
    assert merge_enabled(None) is True
    assert merge_enabled({"vlm": {"merge": {"enabled": False}}}) is False
    assert merge_enabled(json.loads('{"vlm": {"merge": {}}}')) is True
    with pytest.raises(ValueError):
        merge_enabled({"vlm": {"merge": {"enabled": "false"}}})
