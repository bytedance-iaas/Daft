"""Effective upper bounds for one task (D31; design doc 04, sections 2.1-2.3).

Callers never submit a plan (D5). What they can do is lower two numbers:

* the CPU concurrency of the numeric and frame stages, and
* the VLM parallelism N the eight VLM gates are derived from.

Each number is the smallest of the bounds that apply, and the plan records
which layer won (``plan.json`` ``limits.*.bound_by``):

    cpu  = min(cores - 2 (at least 1),  task cap)        (P4: one worker per core, two
                                                          cores left to the Daemon)
    N    = min(task cap, model parallelism, backend parallelism, site max,
               site default | planner default 64   (only when neither the model
                                                     nor the backend sets one))
    N    = N // running tasks                        (P1, when more than one runs)

``cpu`` is what one task may use at most; the Daemon hands out the cores themselves from
one pool shared by every running task (04 §2.3, D54). ``dedup`` is not covered here: its
concurrency is always 1 (doc 05, section 1).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

#: Planner defaults when neither the site nor the task says anything.
DEFAULT_VLM_PARALLELISM = 64      # v1's factory defaults are the gates of N=64 (04 §2.2)
RESERVED_CORES = 2                # P4: left to the Daemon and the web console

#: ``concurrency`` keys of site settings before D54: read, ignored, warned about
RETIRED_SITE_KEYS = ("cpu", "cpuMax", "cpu_max")

#: When several bounds tie, the one reported is the first in this order: the layers
#: a user can change come first, so ``bound_by`` answers "which of my settings is in effect".
BOUND_BY_ORDER = ("task", "model", "backend", "site", "running_tasks", "planner")


@dataclass(frozen=True)
class Limit:
    """One effective bound and the layer that set it."""

    value: int
    bound_by: str

    def to_json(self) -> dict:
        return {"value": self.value, "bound_by": self.bound_by}


def _positive_int(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def available_cpu_workers(cores: int | None = None) -> int:
    """CPU workers of the node: one per core, two cores left to the Daemon, at least 1 (P4).

    ``cores`` is the container's CPU quota when there is one (the Daemon reads it);
    ``os.cpu_count()`` otherwise.
    """
    return max(1, (cores or os.cpu_count() or 1) - RESERVED_CORES)


def retired_site_keys(data: Mapping[str, Any] | None) -> list[str]:
    """``concurrency.cpu`` / ``cpuMax`` still written in old site settings (ignored since D54)."""
    conc = (data or {}).get("concurrency") if isinstance(data, Mapping) else None
    if not isinstance(conc, Mapping):
        return []
    return [f"concurrency.{k}" for k in RETIRED_SITE_KEYS if conc.get(k) is not None]


def _pick(candidates: list[tuple[str, int]]) -> Limit:
    value = min(v for _, v in candidates)
    for layer in BOUND_BY_ORDER:
        if any(src == layer and v == value for src, v in candidates):
            return Limit(value, layer)
    raise AssertionError(f"unknown layer in {candidates}")  # pragma: no cover


@dataclass(frozen=True)
class PlanLimits:
    """Bounds from below the site: the task, the chosen model and backend, the node.

    ``cpu_concurrency`` / ``vlm_parallelism`` are the task's ``params.limits``;
    ``model_parallelism`` / ``backend_parallelism`` come from the secrets and
    resources page; ``cpu_cores`` defaults to ``os.cpu_count()``;
    ``running_tasks`` is how many tasks run when this one starts (P1).
    """

    cpu_concurrency: int | None = None
    vlm_parallelism: int | None = None
    model_parallelism: int | None = None
    backend_parallelism: int | None = None
    cpu_cores: int | None = None
    running_tasks: int = 1

    def __post_init__(self) -> None:
        for name in ("cpu_concurrency", "vlm_parallelism", "model_parallelism",
                     "backend_parallelism", "cpu_cores", "running_tasks"):
            _positive_int(name, getattr(self, name))
        if self.running_tasks is None:
            raise ValueError("running_tasks must be a positive integer, got None")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "PlanLimits":
        data = dict(data or {})
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown plan limits {unknown}; known: {sorted(known)}")
        return cls(**data)


@dataclass(frozen=True)
class SiteConfig:
    """The site-level knobs the planner reads (Helm ``values.yaml`` / site config).

    Shape accepted by :meth:`from_mapping` (every key optional)::

        concurrency: {vlmParallelism: 64, vlmParallelismMax: 128}
        vlm:
          merge:
            enabled: true                 # the kill switch, vlm.merge.enabled (04 §4.2)
            max_units: 8                  # MergeLimits fields, see curation.planner.merge
            max_images: 32
            max_prompt_tokens: 6000
            context_window: 128000
          gates: {probe: 64}              # per-gate tuning, stated at vlmParallelism (or 64)

    The CPU side has no site knob (D54): ``concurrency.cpu`` / ``cpuMax`` of older files
    are ignored (:func:`retired_site_keys` names them so the caller can warn).
    """

    vlm_parallelism: int | None = None
    vlm_parallelism_max: int | None = None
    merge_enabled: bool = True
    gate_overrides: Mapping[str, int] = field(default_factory=dict)
    merge_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("vlm_parallelism", "vlm_parallelism_max"):
            _positive_int(name, getattr(self, name))
        if not isinstance(self.merge_enabled, bool):
            raise ValueError(f"vlm.merge.enabled must be true or false, got {self.merge_enabled!r}")
        from .gates import GATE_NAMES

        for gate, value in self.gate_overrides.items():
            if gate not in GATE_NAMES:
                raise ValueError(f"unknown gate {gate!r} in vlm.gates; known: {GATE_NAMES}")
            _positive_int(f"vlm.gates.{gate}", value)
        self.merge_limits_obj()                       # fail early on a bad vlm.merge block

    @property
    def reference_parallelism(self) -> int:
        """The N that ``gate_overrides`` are stated at."""
        return self.vlm_parallelism or DEFAULT_VLM_PARALLELISM

    def merge_limits_obj(self):
        """``vlm.merge.*`` as :class:`curation.planner.merge.MergeLimits`."""
        from .merge import MergeLimits

        unknown = sorted(set(self.merge_limits) - set(MergeLimits.__dataclass_fields__))
        if unknown:
            raise ValueError(f"unknown vlm.merge keys {unknown}")
        return MergeLimits(**dict(self.merge_limits))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SiteConfig":
        data = dict(data or {})
        conc = dict(data.get("concurrency") or {})
        vlm = dict(data.get("vlm") or {})
        merge = dict(vlm.get("merge") or {})

        def one(block: dict, *names: str):
            found = [n for n in names if block.get(n) is not None]
            if len(found) > 1:
                raise ValueError(f"both {found[0]!r} and {found[1]!r} are set; use one")
            return block.get(found[0]) if found else None

        return cls(
            vlm_parallelism=one(conc, "vlmParallelism", "vlm_parallelism"),
            vlm_parallelism_max=one(conc, "vlmParallelismMax", "vlm_parallelism_max"),
            merge_enabled=merge.get("enabled", True),
            gate_overrides=dict(vlm.get("gates") or {}),
            merge_limits={k: v for k, v in merge.items() if k != "enabled"},
        )


def coerce_limits(limits: PlanLimits | Mapping[str, Any] | None) -> PlanLimits:
    return limits if isinstance(limits, PlanLimits) else PlanLimits.from_mapping(limits)


def coerce_site(site: SiteConfig | Mapping[str, Any] | None) -> SiteConfig:
    return site if isinstance(site, SiteConfig) else SiteConfig.from_mapping(site)


def effective_cpu_concurrency(limits: PlanLimits | Mapping | None = None,
                              site: SiteConfig | Mapping | None = None) -> Limit:
    """The most CPU workers one task's CPU stages may use (04 §2.1, P4).

    ``site`` is accepted for symmetry with :func:`effective_vlm_parallelism`; nothing in it
    bounds the CPU side any more (D54).
    """
    limits = coerce_limits(limits)
    coerce_site(site)
    candidates = [("planner", available_cpu_workers(limits.cpu_cores))]
    if limits.cpu_concurrency is not None:
        candidates.append(("task", limits.cpu_concurrency))
    return _pick(candidates)


def effective_vlm_parallelism(limits: PlanLimits | Mapping | None = None,
                              site: SiteConfig | Mapping | None = None) -> Limit:
    """The one VLM parallelism N (04 §2.2 - §2.3)."""
    limits, site = coerce_limits(limits), coerce_site(site)
    candidates: list[tuple[str, int]] = []
    if limits.vlm_parallelism is not None:
        candidates.append(("task", limits.vlm_parallelism))
    if limits.model_parallelism is not None:
        candidates.append(("model", limits.model_parallelism))
    if limits.backend_parallelism is not None:
        candidates.append(("backend", limits.backend_parallelism))
    if site.vlm_parallelism_max is not None:
        candidates.append(("site", site.vlm_parallelism_max))
    if limits.model_parallelism is None and limits.backend_parallelism is None:
        candidates.append(("site", site.vlm_parallelism) if site.vlm_parallelism is not None
                          else ("planner", DEFAULT_VLM_PARALLELISM))
    limit = _pick(candidates)
    if limits.running_tasks > 1:
        share = max(1, limit.value // limits.running_tasks)
        if share < limit.value:
            limit = Limit(share, "running_tasks")
    return limit
