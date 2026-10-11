"""One VLM parallelism N -> the VLM gates (design doc 04, section 2.2).

The product exposes one number N and the planner derives the gates of a VLM stage from it:

    gate      what it limits                                       derived
    probe     the model requests in flight (task success and its    N
              picture defects, the EEF module's review)
    episode   the VLM stage's episodes in flight                    probe

One request per episode (D71): an episode in flight is one probe in flight, so the episode gate follows
the probe gate unless the site overrides it. The per-camera review, the arbitration and the caption
gates of v1 are gone with their requests (design doc 27, D89).

N is not a hard cap of everything a task sends: hedges and retries queue inside the gates.
"""
from __future__ import annotations

from typing import Mapping

GATE_NAMES = ("episode", "probe")

#: The gates each VLM stage of a plan carries (04 §4.1).
STAGE_GATES: dict[str, tuple[str, ...]] = {
    "vlm": ("episode", "probe"),
}


def derive_gates(parallelism: int, overrides: Mapping[str, int] | None = None,
                 reference: int = 64) -> dict[str, int]:
    """The gates for parallelism N.

    ``overrides`` are the site's per-gate tuning (``vlm.gates``), stated at ``reference`` (the site's
    default N); they scale with N so a task that lowers N lowers them too:
    ``gate = max(1, value * N // reference)``. Overriding the probe gate moves the episode gate with it
    unless that is overridden itself.
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

    probe = gate("probe", n)
    return {"episode": gate("episode", probe), "probe": probe}


def stage_gates(stage_id: str, gates: Mapping[str, int]) -> dict[str, int]:
    """The subset of ``gates`` a plan stage carries."""
    return {name: gates[name] for name in STAGE_GATES[stage_id]}
