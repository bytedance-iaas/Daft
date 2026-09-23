"""One VLM parallelism N -> the eight gates (design doc 04, section 2.2).

v1 has no single in-flight cap: eight self-built semaphores, each sized on its
own (the 2026-09-07 throughput diagnostic). The product exposes one number N and
the planner derives the eight from it:

    gate            what it limits                              derived
    episode         VLM-stage episodes in flight                N // 2
    probe           task-completion scoring, one per process    N
    endstate        per-camera review                           2 * episode   (v1: episode x 2, >= 2)
    arbitration     evidence-arbitration chain, 4 factories     episode       (v1: = episode)
    guard_caption   caption inside the kill guard               episode       (v1: = episode)
    caption         skill-profile and autolabel captioning      N // 2
    llm             text-only taxonomy calls                    N // 4
    audit           label-audit pair judge                      N // 4

endstate, arbitration and guard_caption follow v1's own coupling to the
episode gate (``pipeline/funnel.py``), so a plan shows exactly what v1's code
does when the CLI configures it through :data:`V1_CONFIG_KEYS`; for even N
they are the N, N/2, N/2 of the design table. At N = 64 every gate equals
v1's factory default (``tests/planner`` reads those from v1 itself).

N is not a hard cap: the gates are independent, so more than N requests can be
in flight (v1 measured 117 at N = 64). Hedges and retries queue inside the gates.
"""
from __future__ import annotations

from typing import Mapping

GATE_NAMES = ("episode", "probe", "endstate", "arbitration", "guard_caption",
              "caption", "llm", "audit")

#: The gates each VLM stage of a plan carries (v1's call sites, 04 §4.1).
STAGE_GATES: dict[str, tuple[str, ...]] = {
    "autolabel": ("caption",),
    "vlm": ("episode", "probe", "endstate", "arbitration", "guard_caption"),
    "profile": ("caption", "llm", "audit"),
    "profile_vlm": ("caption", "llm", "audit"),
}

#: Where each gate lives in v1's pipeline config. The three gates missing here are
#: derived inside ``pipeline/funnel.py`` from ``pipeline.vlm_episode_concurrency``
#: with the same formulas as :func:`derive_gates`, so setting these five keys
#: reproduces all eight gates (the CLI passes them as ``--set k=v``).
V1_CONFIG_KEYS: dict[str, str] = {
    "episode": "pipeline.vlm_episode_concurrency",
    "probe": "checks.task_success.vlm.max_concurrency",
    "caption": "skill_profile.caption_concurrency",
    "llm": "skill_profile.llm_concurrency",
    "audit": "skill_profile.audit_concurrency",
}


def derive_gates(parallelism: int, overrides: Mapping[str, int] | None = None,
                 reference: int = 64) -> dict[str, int]:
    """The eight gates for parallelism N.

    ``overrides`` are the site's per-gate tuning (``vlm.gates``), stated at
    ``reference`` (the site's default N); they scale with N so a task that lowers
    N lowers them too: ``gate = max(1, value * N // reference)``. Overriding the
    episode gate moves endstate, arbitration and guard_caption with it (v1's
    coupling) unless those are overridden themselves.
    """
    if isinstance(parallelism, bool) or not isinstance(parallelism, int) or parallelism < 1:
        raise ValueError(f"parallelism must be a positive integer, got {parallelism!r}")
    if isinstance(reference, bool) or not isinstance(reference, int) or reference < 1:
        raise ValueError(f"reference must be a positive integer, got {reference!r}")
    overrides = dict(overrides or {})
    for name, value in overrides.items():
        if name not in GATE_NAMES:
            raise ValueError(f"unknown gate {name!r}; known: {GATE_NAMES}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"gate {name} must be a positive integer, got {value!r}")
    n = parallelism

    def gate(name: str, derived: int) -> int:
        return max(1, overrides[name] * n // reference) if name in overrides else derived

    episode = gate("episode", max(1, n // 2))
    return {
        "episode": episode,
        "probe": gate("probe", n),
        "endstate": gate("endstate", max(2, 2 * episode)),
        "arbitration": gate("arbitration", episode),
        "guard_caption": gate("guard_caption", episode),
        "caption": gate("caption", max(1, n // 2)),
        "llm": gate("llm", max(1, n // 4)),
        "audit": gate("audit", max(1, n // 4)),
    }


def stage_gates(stage_id: str, gates: Mapping[str, int]) -> dict[str, int]:
    """The subset of ``gates`` a plan stage carries."""
    return {name: gates[name] for name in STAGE_GATES[stage_id]}


def v1_set_overrides(gates: Mapping[str, int]) -> list[str]:
    """``--set path=value`` items that make v1's pipeline use these gates.

    Raises ``ValueError`` when a site override moved endstate, arbitration or
    guard_caption away from v1's coupling to the episode gate: v1's config cannot
    express that until ``funnel.py`` is split into v2 stages (W3).
    """
    episode = gates["episode"]
    coupled = {"endstate": max(2, 2 * episode), "arbitration": episode, "guard_caption": episode}
    off = sorted(name for name, value in coupled.items() if gates[name] != value)
    if off:
        raise ValueError(f"gates {off} are derived from the episode gate in v1's funnel.py and "
                         "cannot be set on their own through v1's config")
    return [f"{path}={gates[name]}" for name, path in V1_CONFIG_KEYS.items()]
