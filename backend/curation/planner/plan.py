"""The planner: preflight + selected modules + limits -> execution plan (04 §3).

The plan is data. The Daemon generates it, stores it with the task
(``plan.json``), schedules by it and serves it read-only; ``curation plan
--json`` prints the same thing (02 §3.2). Nobody submits a plan (D5); callers
can only lower the limits it is derived from (D31).

Stages follow v1's funnel (D18), in this order, and a stage without modules is
left out:

    autolabel  captions for episodes without a task text, before the funnel
    integrity  the data integrity module            cpu (mostly I/O), hard gate 0 (design doc 14)
    numeric    parquet-only checks                  cpu, hard gate 1
    frame      checks sharing one full-rate decode  cpu, hard gate 2
    vlm        the VLM check on the survivors       vlm gates, merge proposal
    verdict    aggregate --phase funnel             (always)
    dedup      exact duplicates on the kept set     cpu, concurrency always 1
    profile_vlm skill profile on the kept set       vlm gates
    final      aggregate --phase final              (always)

Each funnel stage reads the survivors of the one before it. autolabel runs only
if a selected funnel module needs the task text (in v1 only task_success does:
``run.py`` captions unlabeled episodes only when task_success is on;
skill_profile reuses those captions and captions the rest itself) and some
selected episode may lack one. Modules the preflight marked unsupported stay
out, and the plan says why.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..contracts import modules as registry_mod
from .estimates import estimate
from .gates import derive_gates, stage_gates
from .limits import (PlanLimits, SiteConfig, coerce_limits, coerce_site,
                     effective_cpu_concurrency, effective_vlm_parallelism)
from .merge import declared_frame_policy

SCHEMA_VERSION = "1.0"
FUNNEL_STAGES = ("integrity", "numeric", "frame", "vlm")


class PlanError(ValueError):
    """The inputs cannot make a plan (unsupported dataset, unknown module, bad episodes)."""


def _registry(registry: Iterable[Any] | None) -> dict[str, Any]:
    specs = list(registry_mod.MODULES if registry is None else registry)
    by_id: dict[str, Any] = {}
    for spec in specs:
        if spec.id in by_id:
            raise PlanError(f"module {spec.id!r} is registered twice")
        if spec.stage not in registry_mod.STAGE_ORDER:
            raise PlanError(f"module {spec.id!r} has unknown stage {spec.stage!r}")
        by_id[spec.id] = spec
    return by_id


def _requested(modules: Iterable[Any], specs: Mapping[str, Any]) -> list[str]:
    if isinstance(modules, str):
        raise PlanError("modules must be a list of module ids, not one string")
    out: list[str] = []
    for item in modules:
        module_id = item.get("id") if isinstance(item, Mapping) else item
        if module_id not in specs:
            raise PlanError(f"unknown module {module_id!r}; known: {', '.join(specs)}")
        if module_id not in out:
            out.append(module_id)
    if not out:
        raise PlanError("no modules selected")
    return out


def _selected(episodes: Iterable[int] | None, count: int) -> list[int]:
    if episodes is None:
        chosen = list(range(count))
    else:
        chosen = []
        for index in episodes:
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
                raise PlanError(f"episode {index!r} is not in 0..{count - 1}")
            chosen.append(index)
        chosen = sorted(set(chosen))
    if not chosen:
        raise PlanError("no episodes selected")
    return chosen


def _unlabeled(dataset: Mapping[str, Any], selected: Sequence[int], count: int,
               unlabeled_episodes: Iterable[int] | None, notes: list[str]) -> int:
    """How many selected episodes lack a task text (exact when the caller knows)."""
    if unlabeled_episodes is not None:
        return len(set(unlabeled_episodes) & set(selected))
    without = int(((dataset.get("labels") or {}).get("without_task")) or 0)
    if without == 0 or len(selected) == count:
        return without
    guess = max(1, round(without * len(selected) / count))
    notes.append(f"unlabeled episodes among the selection estimated from the preflight "
                 f"totals ({without} of {count})")
    return guess


def _merge_proposal(stage_specs: Sequence[Any], site: SiteConfig,
                    notes: list[str]) -> dict[str, Any]:
    """Which modules of a VLM stage may share a request (the proposal, 04 §4.2)."""
    by_policy: dict[str, list[str]] = {}
    for spec in stage_specs:
        policy = declared_frame_policy(spec.merge_units) if spec.merge_units is not None else None
        if policy is not None:
            by_policy.setdefault(policy.key, []).append(spec.id)
    groups = [{"modules": mods, "frame_policy": key}
              for key, mods in by_policy.items() if len(mods) > 1]
    if not groups:
        return {"strategy": "none", "groups": []}
    if not site.merge_enabled:
        notes.append("request merging is switched off by the site config (vlm.merge.enabled)")
        return {"strategy": "none", "groups": []}
    return {"strategy": "per_episode_multi_module", "groups": groups}


def build_plan(preflight: Mapping[str, Any], modules: Iterable[Any],
               episodes: Iterable[int] | None = None,
               limits: PlanLimits | Mapping[str, Any] | None = None,
               site_config: SiteConfig | Mapping[str, Any] | None = None, *,
               unlabeled_episodes: Iterable[int] | None = None,
               registry: Iterable[Any] | None = None,
               validate: bool = False) -> dict[str, Any]:
    """Build ``plan.json`` (``docs/contracts/cli/plan.schema.json``).

    ``preflight`` is a ``curation preflight --json`` result; ``modules`` the
    selected ids (or ``{"id": ...}`` choices); ``episodes`` the selected indices
    (default: all); ``limits`` a :class:`PlanLimits` or mapping;
    ``site_config`` a :class:`SiteConfig` or mapping. ``unlabeled_episodes``
    makes the autolabel decision exact when the caller knows which episodes lack
    a task text. ``registry`` defaults to C1 (tests pass extra example modules).
    ``validate=True`` checks the result against the contract (needs jsonschema
    and ``docs/contracts``).
    """
    limits, site = coerce_limits(limits), coerce_site(site_config)
    specs = _registry(registry)
    fmt = preflight.get("format") or {}
    if not fmt.get("supported", False):
        raise PlanError(f"the dataset format is not supported ({fmt.get('detail') or fmt.get('kind')}); "
                        "every module is unavailable (D6)")
    dataset = preflight.get("dataset")
    if not isinstance(dataset, Mapping):
        raise PlanError("the preflight has no dataset block")
    count = int(dataset["episode_count"])
    selected = _selected(episodes, count)
    requested = set(_requested(modules, specs))
    availability = {m["id"]: m for m in preflight.get("modules") or []}

    notes: list[str] = []
    chosen = []
    for spec in specs.values():                        # registry order, not the caller's
        if spec.id not in requested:
            continue
        entry = availability.get(spec.id) or {}
        state = entry.get("availability", "available")
        if state == "unsupported":
            notes.append(f"{spec.id} skipped: {entry.get('reason', 'unsupported')}")
            continue
        if state == "needs_input":
            field = (entry.get("input_hint") or {}).get("field", "input")
            notes.append(f"{spec.id} needed {field} at preflight; the task must supply it")
        chosen.append(spec)

    cpu = effective_cpu_concurrency(limits, site)
    vlm = effective_vlm_parallelism(limits, site)
    gates = derive_gates(vlm.value, site.gate_overrides, site.reference_parallelism)

    stages: list[dict[str, Any]] = []
    unlabeled = _unlabeled(dataset, selected, count, unlabeled_episodes, notes)
    if unlabeled and any(s.stage not in ("post_verdict", "profile_vlm") and "autolabel" in s.depends_on for s in chosen):
        stages.append({"id": "autolabel", "kind": "vlm", "command": "autolabel",
                       "episodes": "unlabeled", "gates": stage_gates("autolabel", gates)})

    previous = None
    advisory = [s for s in chosen if s.input_scope == "all_selected"]
    funnel = [s for s in chosen if s.input_scope != "all_selected"]
    for stage_id in FUNNEL_STAGES:
        members = [s for s in funnel if s.stage == stage_id]
        if not members:
            continue
        kind = "vlm" if any("vlm" in s.needs for s in members) else "cpu"
        stage: dict[str, Any] = {"id": stage_id, "kind": kind, "command": "check"}
        if kind == "cpu":
            stage["concurrency"] = cpu.value
        stage["modules"] = [s.id for s in members]
        stage["episodes"] = f"survivors:{previous}" if previous else "selected"
        hard = [s.id for s in members if s.gate == "hard"]
        if hard:
            stage["hard_gates"] = hard
        if kind == "vlm":
            stage["gates"] = stage_gates("vlm", gates)
            stage["merge"] = _merge_proposal(members, site, notes)
        stages.append(stage)
        previous = stage_id
    # advisory modules (registry 1.4): every selected episode, never a gate, outside the verdict
    for stage_id in FUNNEL_STAGES:
        members = [s for s in advisory if s.stage == stage_id]
        if not members:
            continue
        kind = "vlm" if any("vlm" in s.needs for s in members) else "cpu"
        stage = {"id": f"advisory_{stage_id}", "kind": kind, "command": "check",
                 "modules": [s.id for s in members], "episodes": "selected"}
        if kind == "cpu":
            stage["concurrency"] = cpu.value
        else:
            stage["gates"] = stage_gates("vlm", gates)
            stage["merge"] = _merge_proposal(members, site, notes)
        stages.append(stage)
    stages.append({"id": "verdict", "kind": "aggregate", "command": "aggregate", "phase": "funnel"})

    post = [s for s in chosen if s.stage == "post_verdict"]
    profile = [s for s in chosen if s.stage == "profile_vlm"]
    dedup = [s for s in post if s.gate == "dedup"]
    if dedup:
        stages.append({"id": "dedup", "kind": "cpu", "command": "check", "concurrency": 1,
                       "modules": [s.id for s in dedup], "episodes": "keep"})
    if profile:
        kind = "vlm" if any("vlm" in s.needs for s in profile) else "cpu"
        stage = {"id": "profile_vlm", "kind": kind, "command": "check"}
        if kind == "cpu":
            stage["concurrency"] = cpu.value
        stage["modules"] = [s.id for s in profile]
        stage["episodes"] = "keep-minus-duplicates" if dedup else "keep"
        if kind == "vlm":
            stage["gates"] = stage_gates("profile", gates)
            stage["merge"] = _merge_proposal(profile, site, notes)
        stages.append(stage)
    stages.append({"id": "final", "kind": "aggregate", "command": "aggregate", "phase": "final"})

    if not chosen:
        notes.append("no selected module can run on this dataset; only the verdict steps remain")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "vlm_parallelism": vlm.value,
        "limits": {"cpu_concurrency": cpu.to_json(), "vlm_parallelism": vlm.to_json()},
        "stages": stages,
        "estimates": estimate(stages, specs, selected=len(selected), unlabeled=unlabeled,
                              cameras=len(dataset.get("cameras") or ()),
                              max_units=site.merge_limits_obj().max_units, notes=notes),
    }
    if validate:
        validate_plan(plan)
    return plan


def validate_plan(plan: Mapping[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` unless ``plan`` fits ``cli/plan.schema.json``."""
    from ..contracts import schemas

    schemas.validate("cli/plan.schema.json", plan)
