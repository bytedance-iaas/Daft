"""Rough plan estimates: VLM requests and wall clock (``plan.json`` ``estimates``).

Advice for the UI, never a limit. Every constant is one of v1's own
measurements or factory settings, so the tuning task can see where a number
comes from (design doc 04, sections 1, 2.1 and 4.1; doc 10, section 4).
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

VLM_LATENCY_S = 22.7          # 2026-09-07 diagnostic: mean latency 22.6-22.8 s at N=64
GATE_UTILISATION = 0.67       # the same run used 67 % of the probe gate
NUMERIC_S_PER_EPISODE = 0.05  # parquet-only checks, seconds per episode
FRAME_S_PER_EPISODE = 3.0     # 640 episodes took ~31 min serially (04 §2.1)
DEDUP_S_PER_EPISODE = 0.02    # action hashes; video hashes only for collisions
AGGREGATE_S = 1.0

#: v1's call graph per episode (04 §4.1) for the two existing VLM modules.
TASK_SUCCESS_PROBES = 1       # one multi-camera video assessment
ENDSTATE_PER_CAMERA = 1       # one independent video review per camera
MAX_ENDSTATE_CAMS = 4         # pipeline.max_endstate_cams
CAPTIONS_PER_EPISODE = 1


def _units_per_episode(spec: Any) -> int:
    """Questions a mergeable module asks per episode (1 unless it declares otherwise)."""
    return int(getattr(spec.merge_units, "units_per_episode", 1) or 1)


def _vlm_seconds(requests: int, gate: int) -> float:
    return requests * VLM_LATENCY_S / (max(1, gate) * GATE_UTILISATION) if requests else 0.0


def estimate(stages: Sequence[Mapping[str, Any]], specs: Mapping[str, Any], *,
             selected: int, unlabeled: int, cameras: int,
             max_units: int, notes: list[str]) -> dict[str, Any]:
    """Requests and seconds for ``stages``; appends what it could not count to ``notes``."""
    requests, seconds = 0, 0.0
    cams = max(1, min(cameras or 1, MAX_ENDSTATE_CAMS))
    autolabelled = 0
    uncounted: list[str] = []
    for stage in stages:
        sid, gates = stage["id"], stage.get("gates", {})
        if sid == "autolabel":
            autolabelled = unlabeled
            requests += unlabeled * CAPTIONS_PER_EPISODE
            seconds += _vlm_seconds(unlabeled * CAPTIONS_PER_EPISODE, gates["caption"])
        elif sid == "numeric":
            seconds += selected * NUMERIC_S_PER_EPISODE / stage["concurrency"]
        elif sid == "frame":
            seconds += selected * FRAME_S_PER_EPISODE / stage["concurrency"]
        elif sid == "vlm":
            grouped: set[str] = set()
            for group in stage.get("merge", {}).get("groups", []):
                units = sum(_units_per_episode(specs[m]) for m in group["modules"])
                grouped.update(group["modules"])
                n = selected * math.ceil(units / max_units)
                requests += n
                seconds += _vlm_seconds(n, gates["probe"])
            for module in stage["modules"]:
                if module in grouped:
                    continue
                if specs[module].merge_units is not None:
                    n = selected * _units_per_episode(specs[module])
                    requests += n
                    seconds += _vlm_seconds(n, gates["probe"])
                elif module == "task_success":
                    probes = selected * TASK_SUCCESS_PROBES
                    reviews = selected * ENDSTATE_PER_CAMERA * cams
                    requests += probes + reviews
                    seconds += _vlm_seconds(probes, gates["probe"])
                    seconds += _vlm_seconds(reviews, gates["endstate"])
                else:
                    uncounted.append(module)
        elif sid in ("verdict", "final"):
            seconds += AGGREGATE_S
        elif sid == "dedup":
            seconds += selected * DEDUP_S_PER_EPISODE
        elif sid in ("profile", "profile_vlm"):
            for module in stage["modules"]:
                if module == "skill_profile":
                    n = max(0, selected - autolabelled) * CAPTIONS_PER_EPISODE
                    requests += n
                    seconds += _vlm_seconds(n, gates.get("caption", 1))
                elif stage["kind"] == "vlm":
                    uncounted.append(module)
    notes.append(f"rough estimate: every selected episode is assumed to pass the hard gates; "
                 f"{VLM_LATENCY_S:g} s per request at {GATE_UTILISATION:.0%} gate use "
                 "(image-request baseline from v1, 2026-09-07; video latency is not calibrated)")
    if any(s["id"] == "vlm" and "task_success" in s.get("modules", ()) for s in stages):
        notes.append("task_success arbitration and label-guard calls depend on the data "
                     "and are not counted")
    if any(s["id"] in ("profile", "profile_vlm") and "skill_profile" in s.get("modules", ()) for s in stages):
        notes.append("skill_profile text calls (taxonomy, label audit) are per dataset "
                     "and not counted")
    if uncounted:
        notes.append(f"no request model for {sorted(set(uncounted))}; not counted")
    return {"vlm_requests": int(requests), "wall_clock_s": int(round(seconds)), "notes": notes}
