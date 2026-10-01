"""The funnel's gate attributes, which the module registry dropped in 2.0 (design doc 17 §2.1).

Not a contract. Registry 2.0 describes a module by its block, stage and finding codes; the
funnel that still runs until F12.3 / F12.4 replace it needs two facts the registry no longer
carries: how a module votes in v1's verdict (``gate``: a hard veto, a soft score, dedup's removal
or none) and whether it votes at all (an advisory module's results never enter keep / drop /
held). They live here for the planner's ``hard_gates``, aggregate's funnel and soft score, the
report's module sections, check's circuit breaker and the Daemon's live verdicts, and this module
goes away with the last of them.
"""
from __future__ import annotations

from ..contracts import modules as registry

#: v1's gate of every registered module, as registry 1.14 had it
_GATES = {"data_integrity": "hard", "timestamp_check": "hard", "kinematic_limits": "hard",
          "motion_quality": "soft", "visual_quality": "soft", "video_action_sync": "hard",
          "eef_video_consistency": "hard", "task_success": "hard", "camera_defects": "none",
          "dedup": "dedup", "skill_profile": "none"}
#: modules whose results never enter keep / drop / held (1.x ``affects_dataset_verdict=False``)
_ADVISORY = frozenset({"camera_defects"})


def gate(module) -> str:
    """``module``: an id or a ModuleSpec. A module the table does not know (a test's example
    module) is a hard gate when one of its codes blocks by default, else none; an unknown id is none."""
    mid = module if isinstance(module, str) else module.id
    if mid in _GATES:
        return _GATES[mid]
    try:
        spec = registry.get(mid) if isinstance(module, str) else module
    except KeyError:
        return "none"
    return "hard" if any(c.level == "blocking" for c in spec.codes) else "none"


def votes(module_id: str) -> bool:
    """Whether the module's results enter keep / drop / held."""
    return module_id not in _ADVISORY


def advisory_ids() -> tuple[str, ...]:
    """The registered modules that never vote, in registry order."""
    return tuple(m for m in registry.ids() if m in _ADVISORY)
