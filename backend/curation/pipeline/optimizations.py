"""Opt-in v1 execution policies; public function signatures stay unchanged."""
from __future__ import annotations

FLAGS = (
    "usage_accounting", "dedicated_executor", "streaming_funnel", "checkpoint",
    "resume", "frame_cache", "action_hash_reuse", "parallel_video_hash",
    "caption_reuse", "skip_strong_review", "probe_early_stop", "probe_batch",
    "review_merge", "review_two_cams", "skip_review", "judge_n3", "caption_merge",
    "thinking_off_probe", "thinking_off_endstate", "thinking_off_judge",
    "thinking_off_grounder", "batch_probe", "ray_workers",
)
# Register capabilities only when their implementation and tests land.
IMPLEMENTED: frozenset[str] = frozenset({"dedicated_executor", "streaming_funnel",
                                       "checkpoint", "resume", "action_hash_reuse",
                                       "parallel_video_hash", "frame_cache"})
DEPENDENCIES = {
    "checkpoint": {"streaming_funnel"}, "resume": {"checkpoint"},
    "probe_batch": {"probe_early_stop"}, "caption_merge": {"review_merge"},
}
CONFLICTS = (
    ("skip_review", "skip_strong_review"), ("skip_review", "review_merge"),
    ("skip_review", "review_two_cams"), ("caption_merge", "skip_review"),
    ("caption_merge", "skip_strong_review"), ("batch_probe", "probe_early_stop"),
    ("batch_probe", "probe_batch"),
)


def _flags(cfg: dict) -> dict[str, bool]:
    from .config import ConfigError

    values = cfg.get("pipeline", {}).get("optimizations", {})
    if not isinstance(values, dict):
        raise ConfigError("pipeline.optimizations must be a mapping of boolean flags")
    for key, value in values.items():
        if key not in FLAGS:
            raise ConfigError(f"Unknown optimization: {key}")
        if type(value) is not bool:
            raise ConfigError(f"pipeline.optimizations.{key} must be true/false")
    return {name: values.get(name, False) for name in FLAGS}


def _validate_execution(cfg: dict, *, v2: bool = False) -> dict[str, bool]:
    from .config import ConfigError

    flags = _flags(cfg)
    active = {key for key, value in flags.items() if value}
    if active & {'dedicated_executor', 'streaming_funnel'}:
        concurrency = cfg.get('pipeline', {}).get('vlm_episode_concurrency', 8)
        if type(concurrency) is not int or concurrency < 1:
            raise ConfigError('vlm_episode_concurrency must be a positive integer')
    for key in active:
        missing = DEPENDENCIES.get(key, set()) - active
        if missing:
            raise ConfigError(f"{key} requires: {', '.join(sorted(missing))}")
    for left, right in CONFLICTS:
        if left in active and right in active:
            raise ConfigError(f"Conflicting optimizations: {left}, {right}")
    unavailable = active if v2 else active - IMPLEMENTED
    if unavailable:
        raise ConfigError("Optimizations unavailable for this entry point: "
                          + ', '.join(sorted(unavailable)))
    return flags


def _cli_overrides(enables, disables) -> list[str]:
    from .config import ConfigError

    on, off = set(enables or []), set(disables or [])
    unknown = (on | (off - {"all"})) - set(FLAGS)
    if unknown:
        raise ConfigError("Unknown optimization: " + ', '.join(sorted(unknown)))
    if on & off or ("all" in off and on):
        raise ConfigError("Cannot enable and disable the same optimization (or disable all)")
    if "all" in off:
        off = set(FLAGS)
    return [f"pipeline.optimizations.{key}={'true' if key in on else 'false'}"
            for key in FLAGS if key in on or key in off]
