"""Episode processing times; shared work across modules is counted once per layer."""
import math

from ..contracts import modules


def processing_times(records: dict[str, dict]) -> dict[str, float]:
    times = {}
    for module, record in records.items():
        stage = modules.get(module).stage
        elapsed = record.get("elapsed_s")
        if stage not in ("numeric", "frame", "vlm") or not isinstance(elapsed, (int, float)):
            continue
        if math.isfinite(elapsed) and elapsed >= 0:
            times[stage] = max(times.get(stage, 0.0), elapsed)
    return times
