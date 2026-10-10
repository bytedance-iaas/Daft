"""Episode processing times; shared work across modules is counted once per layer."""
import math

from ..contracts import modules


def processing_times(records: dict[str, dict]) -> dict[str, float]:
    times = {}
    for module, record in records.items():
        # a module of two halves (registry 5.3) says what each stage took: vlm_prep, then vlm when asked
        halves = (record.get("details") or {}).get("halves")
        parts = halves.items() if isinstance(halves, dict) else [(modules.get(module).stage, record.get("elapsed_s"))]
        for stage, elapsed in parts:
            if stage not in ("integrity", "numeric", "frame", "vlm_prep", "vlm") or not isinstance(elapsed, (int, float)):
                continue
            if math.isfinite(elapsed) and elapsed >= 0:
                times[stage] = max(times.get(stage, 0.0), elapsed)
    return times
