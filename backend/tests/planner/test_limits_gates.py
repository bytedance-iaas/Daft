"""Caps intersect (D31) and one parallelism N -> the eight gates (04 §2.2)."""
from __future__ import annotations

import copy

import pytest

from curation.pipeline import config as v1_config
from curation.planner import (GATE_NAMES, V1_CONFIG_KEYS, PlanLimits, SiteConfig,
                              available_cpu_workers, derive_gates, effective_cpu_concurrency,
                              effective_vlm_parallelism, retired_site_keys, v1_set_overrides)
from curation.planner.limits import DEFAULT_VLM_PARALLELISM

from . import v1_source


# ---------------------------------------------------------------- gates vs v1

def test_v1_factory_gates_are_read_from_v1_itself():
    """The helper finds every gate in v1's code, and v1's call sites of each gate agree."""
    values = v1_source.gate_values(v1_source.factory_config())
    assert set(values) == set(GATE_NAMES)
    for gate, sites in values.items():
        assert sites and len(set(sites)) == 1, (gate, sites)


def test_n64_gates_equal_v1_factory_defaults_item_by_item():
    """Acceptance (F2.4 as amended by D23): at N = 64 the eight derived gates are v1's defaults."""
    v1 = v1_source.factory_gates()
    derived = derive_gates(64)
    for gate in GATE_NAMES:
        assert derived[gate] == v1[gate], f"{gate}: planner {derived[gate]} != v1 {v1[gate]}"
    assert DEFAULT_VLM_PARALLELISM == 64


def test_default_plan_parallelism_reproduces_v1():
    """With no caps at all the planner lands on v1's factory gates."""
    n = effective_vlm_parallelism(PlanLimits(), SiteConfig())
    assert n.value == 64 and n.bound_by == "planner"
    assert derive_gates(n.value) == v1_source.factory_gates()


@pytest.mark.parametrize("n", [1, 2, 3, 7, 16, 33, 64, 100, 128, 256])
def test_v1_config_keys_reproduce_every_gate(n):
    """Setting the five v1 keys makes v1's own code size all eight gates as derive_gates(n)."""
    cfg = v1_config.apply_overrides(copy.deepcopy(v1_source.factory_config()),
                                    v1_set_overrides(derive_gates(n)))
    v1_config.validate_config(cfg)
    assert v1_source.gates(cfg) == derive_gates(n)


def test_v1_keys_exist_in_factory_config():
    cfg = v1_source.factory_config()
    for path in V1_CONFIG_KEYS.values():
        node = cfg
        for part in path.split("."):
            node = node[part]
        assert isinstance(node, int)


def test_v1_objects_hold_those_gates():
    """Cross-check by running v1's factories: the SharedGates they build have these capacities."""
    pytest.importorskip("daft")
    from curation.adapters import vlm_client
    from curation.pipeline import funnel

    def capacities(fn, depth: int = 4, seen=None) -> set[int]:
        seen = set() if seen is None else seen
        out: set[int] = set()
        if depth < 0 or id(fn) in seen:
            return out
        seen.add(id(fn))
        for cell in getattr(fn, "__closure__", None) or ():
            value = cell.cell_contents
            if isinstance(value, vlm_client.SharedGate):
                out.add(value.capacity)
            elif callable(value):
                out |= capacities(value, depth - 1, seen)
        return out

    cfg = v1_source.factory_config()
    derived = derive_gates(64)
    assert capacities(vlm_client.vlm_completion_from_config(cfg)) == {derived["probe"]}
    deps = funnel.build_arbitration_deps(cfg)
    for name in ("question_writer", "grounder", "judge"):
        assert capacities(deps[name]) == {derived["arbitration"]}
    assert capacities(deps["captioner"]) == {derived["guard_caption"]}


# ---------------------------------------------------------------- derive_gates

@pytest.mark.parametrize("n", range(1, 257))
def test_gates_are_positive_and_follow_the_table(n):
    g = derive_gates(n)
    assert set(g) == set(GATE_NAMES) and all(v >= 1 for v in g.values())
    assert g["probe"] == n
    assert g["episode"] == g["caption"] == max(1, n // 2)
    assert g["arbitration"] == g["guard_caption"] == g["episode"]
    assert g["endstate"] == max(2, 2 * g["episode"])
    assert g["llm"] == g["audit"] == max(1, n // 4)
    if n % 2 == 0:
        assert g["endstate"] == max(2, n)           # the design table's N


def test_gates_never_shrink_as_n_grows():
    previous = derive_gates(1)
    for n in range(2, 300):
        current = derive_gates(n)
        assert all(current[k] >= previous[k] for k in GATE_NAMES)
        previous = current


def test_site_overrides_scale_with_n():
    assert derive_gates(64, {"probe": 96})["probe"] == 96
    assert derive_gates(32, {"probe": 96})["probe"] == 48
    assert derive_gates(1, {"probe": 96})["probe"] == 1
    assert derive_gates(16, {"probe": 48}, reference=32)["probe"] == 24
    with pytest.raises(ValueError):
        derive_gates(64, {"warp": 3})
    with pytest.raises(ValueError):
        derive_gates(64, {"probe": 0})
    with pytest.raises(ValueError):
        derive_gates(0)
    with pytest.raises(ValueError):
        derive_gates(True)


def test_v1_overrides_refuse_what_v1_cannot_express():
    gates = derive_gates(64, {"endstate": 100})
    with pytest.raises(ValueError, match="endstate"):
        v1_set_overrides(gates)
    assert v1_set_overrides(derive_gates(64))[0] == "pipeline.vlm_episode_concurrency=32"


def test_episode_override_carries_v1_coupled_gates():
    gates = derive_gates(64, {"episode": 48})
    assert (gates["episode"], gates["endstate"], gates["arbitration"], gates["guard_caption"]) == \
        (48, 96, 48, 48)
    cfg = v1_config.apply_overrides(copy.deepcopy(v1_source.factory_config()),
                                    v1_set_overrides(gates))
    assert v1_source.gates(cfg) == gates
    assert derive_gates(64, {"episode": 48, "endstate": 64})["endstate"] == 64


# ---------------------------------------------------------------- CPU concurrency (P4, D54)

@pytest.mark.parametrize("cores,expected", [(32, 30), (64, 62), (16, 14), (4, 2), (3, 1), (2, 1),
                                            (1, 1)])
def test_cpu_default_is_every_core_but_two(cores, expected):
    lim = effective_cpu_concurrency(PlanLimits(cpu_cores=cores))
    assert (lim.value, lim.bound_by) == (expected, "planner")
    assert available_cpu_workers(cores) == expected


def test_cpu_caps_intersect():
    assert effective_cpu_concurrency(PlanLimits(cpu_cores=32, cpu_concurrency=10)).to_json() == \
        {"value": 10, "bound_by": "task"}
    # a task cap above the cores cannot raise it
    assert effective_cpu_concurrency(PlanLimits(cpu_cores=32, cpu_concurrency=64)).to_json() == \
        {"value": 30, "bound_by": "planner"}
    # ties name the layer the user controls
    assert effective_cpu_concurrency(PlanLimits(cpu_cores=32, cpu_concurrency=30)).bound_by == "task"


def test_the_site_no_longer_bounds_cpu():
    """D54: concurrency.cpu / cpuMax of an old site.yaml are ignored (and named, to warn)."""
    old = {"concurrency": {"cpu": 8, "cpuMax": 16, "vlmParallelism": 64}}
    site = SiteConfig.from_mapping(old)
    assert effective_cpu_concurrency(PlanLimits(cpu_cores=32), site).to_json() == \
        {"value": 30, "bound_by": "planner"}
    assert site.vlm_parallelism == 64
    assert retired_site_keys(old) == ["concurrency.cpu", "concurrency.cpuMax"]
    assert retired_site_keys({"concurrency": {"cpu_max": 4}}) == ["concurrency.cpu_max"]
    assert retired_site_keys({"concurrency": {"vlmParallelism": 64}}) == []
    assert retired_site_keys(None) == retired_site_keys({"concurrency": None}) == []


def test_cpu_default_uses_the_machine(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 12)
    assert effective_cpu_concurrency().value == 10
    monkeypatch.setattr("os.cpu_count", lambda: None)
    assert effective_cpu_concurrency().value == 1


# ---------------------------------------------------------------- VLM parallelism (D31, P1)

def test_vlm_caps_intersect():
    def n(**kw):
        return effective_vlm_parallelism(PlanLimits(**{k: v for k, v in kw.items() if k != "site"}),
                                         kw.get("site")).to_json()

    assert n() == {"value": 64, "bound_by": "planner"}
    assert n(model_parallelism=64) == {"value": 64, "bound_by": "model"}
    assert n(model_parallelism=100) == {"value": 100, "bound_by": "model"}
    assert n(model_parallelism=100, backend_parallelism=80) == {"value": 80, "bound_by": "backend"}
    assert n(model_parallelism=100, vlm_parallelism=16) == {"value": 16, "bound_by": "task"}
    assert n(vlm_parallelism=128) == {"value": 64, "bound_by": "planner"}   # a cap never raises N
    assert n(model_parallelism=200, site=SiteConfig(vlm_parallelism_max=128)) == \
        {"value": 128, "bound_by": "site"}
    assert n(site=SiteConfig(vlm_parallelism=48)) == {"value": 48, "bound_by": "site"}
    assert n(model_parallelism=100, site=SiteConfig(vlm_parallelism=48)) == \
        {"value": 100, "bound_by": "model"}                               # site default is only a default
    assert n(model_parallelism=64, running_tasks=2) == {"value": 32, "bound_by": "running_tasks"}
    assert n(model_parallelism=64, running_tasks=3) == {"value": 21, "bound_by": "running_tasks"}
    assert n(model_parallelism=1, running_tasks=4) == {"value": 1, "bound_by": "model"}
    assert n(vlm_parallelism=64, model_parallelism=64) == {"value": 64, "bound_by": "task"}


def test_limits_reject_bad_values():
    for bad in ({"cpu_concurrency": 0}, {"vlm_parallelism": -1}, {"running_tasks": 0},
                {"model_parallelism": 1.5}, {"cpu_cores": True}):
        with pytest.raises(ValueError):
            PlanLimits.from_mapping(bad)
    with pytest.raises(ValueError, match="unknown plan limits"):
        PlanLimits.from_mapping({"vlm_parallelism_cap": 3})


def test_site_config_from_values_yaml_shape():
    site = SiteConfig.from_mapping({
        "concurrency": {"vlmParallelism": 64, "vlmParallelismMax": 128},
        "vlm": {"merge": {"enabled": False, "max_units": 4}, "gates": {"llm": 8}}})
    assert (site.vlm_parallelism, site.vlm_parallelism_max) == (64, 128)
    assert site.merge_enabled is False and site.merge_limits_obj().max_units == 4
    assert site.gate_overrides == {"llm": 8}
    assert SiteConfig.from_mapping(None) == SiteConfig()
    with pytest.raises(ValueError):
        SiteConfig.from_mapping({"concurrency": {"vlmParallelism": 4, "vlm_parallelism": 5}})
    with pytest.raises(ValueError):
        SiteConfig.from_mapping({"vlm": {"merge": {"enabled": "no"}}})
    with pytest.raises(ValueError):
        SiteConfig.from_mapping({"vlm": {"merge": {"max_widgets": 3}}})
    with pytest.raises(ValueError):
        SiteConfig.from_mapping({"vlm": {"gates": {"warp": 3}}})
