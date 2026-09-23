"""Static preflight: per sub-item capability derived from the real input (design 12 §5.1).

Nothing here trusts a self-declared ``can_check``: every cell follows from the parsed sample, the
camera mount, the media kind and which physical points an independent observation route covers.
Top level: any sub-item available -> available; all unsupported -> unsupported; else needs_input
(only for what a user can supply: observation seeds, a VLM backend, a trajectory file).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from . import contracts as C
from .load import EefSample, LoadResult, declared_point_ids

DEFAULT_ALLOWED_MOUNTS = ("fixed_external", "wrist")
CORE_SUBITEMS = (C.POSITION, C.ORIENTATION, C.TEMPORAL, C.STATE_MOTION, C.CAMERA_MOTION)
_RANK = {C.AVAILABLE: 0, C.NEEDS_INPUT: 1, C.UNSUPPORTED: 2}


def _cell(av: str, reason: str | None = None, **extra) -> dict[str, Any]:
    return C.subitem(av, reason, **extra)


def _projection_reason(sample: EefSample, camera_id: str) -> str:
    """Why a camera has no declared projection at all."""
    cam = sample.cameras[camera_id]
    if cam.provided:
        return C.PROJECTION_MISSING
    if not sample.eef_mask.any():
        return C.POSE_SEMANTICS_UNKNOWN
    if sample.anchor_missing and not sample.has_absolute_pose:
        return C.ANCHOR_POSE_MISSING
    if cam.calibration(sample) is None:
        return C.CALIBRATION_MISSING
    return C.PROJECTION_MISSING


def camera_capability(sample: EefSample, camera_id: str, *, observable: Iterable[str] | None,
                      allowed_mounts: Iterable[str] = DEFAULT_ALLOWED_MOUNTS) -> dict[str, Any]:
    """Capability of one camera; ``observable`` = point ids the observation provider covers (None: none)."""
    cam = sample.cameras[camera_id]
    media_kind = cam.media["kind"]
    info = {"mount": cam.mount, "media": media_kind,
            "calibration": "declared" if cam.calibration(sample) is not None else "missing",
            "projection": cam.projection_source}
    cells: dict[str, dict] = {}
    allowed = set(allowed_mounts)
    if cam.mount == "moving":
        cells = {k: _cell(C.UNSUPPORTED, C.MOVING_CAMERA_UNSUPPORTED) for k in C.CAMERA_SUBITEMS}
        return {**info, "subitems": cells, "comparable_points": [], "observable_axes": []}
    if cam.mount not in allowed or cam.mount in ("unknown", "not_applicable"):
        cells = {k: _cell(C.UNSUPPORTED, C.CAMERA_MOUNT_NOT_ALLOWED) for k in C.CAMERA_SUBITEMS}
        return {**info, "subitems": cells, "comparable_points": [], "observable_axes": []}
    declared = set(declared_point_ids(sample, camera_id))
    comparable: list[str] = []
    axes: list[str] = []
    if not declared:
        reason = _projection_reason(sample, camera_id)
        cells[C.POSITION] = _cell(C.UNSUPPORTED, reason)
        cells[C.ORIENTATION] = _cell(C.UNSUPPORTED, reason)
    elif observable is None:
        cells[C.POSITION] = _cell(C.NEEDS_INPUT, C.OBSERVATION_SEED_MISSING)
        cells[C.ORIENTATION] = _cell(C.NEEDS_INPUT, C.OBSERVATION_SEED_MISSING)
    else:
        comparable = sorted(declared & set(observable))
        if comparable:
            cells[C.POSITION] = _cell(C.AVAILABLE)
        else:
            cells[C.POSITION] = _cell(C.UNSUPPORTED, C.VISUAL_POINT_MAPPING_MISSING)
        axes = sorted(a for a, d in sample.axes.items()
                      if d["start_point_id"] in comparable and d["end_point_id"] in comparable)
        cells[C.ORIENTATION] = _cell(C.AVAILABLE) if axes else _cell(C.UNSUPPORTED, C.AXIS_MAPPING_MISSING)
    temporal_blocker = None
    if cam.mount == "wrist":
        temporal_blocker = C.WRIST_CAMERA_SPATIAL_ONLY
    elif media_kind != "video":
        temporal_blocker = C.VIDEO_MISSING
    elif sample.timebase != "video_pts":
        temporal_blocker = C.TIMEBASE_INDEX_ONLY
    if temporal_blocker:
        cells[C.TEMPORAL] = _cell(C.UNSUPPORTED, temporal_blocker)
    else:
        cells[C.TEMPORAL] = dict(cells[C.POSITION])          # the lag is measured on the position tracks
    if cam.mount == "wrist":
        cells[C.CAMERA_MOTION] = _cell(C.UNSUPPORTED, C.WRIST_CAMERA_SPATIAL_ONLY)
    elif media_kind != "video":
        cells[C.CAMERA_MOTION] = _cell(C.UNSUPPORTED, C.VIDEO_MISSING)
    else:
        cells[C.CAMERA_MOTION] = _cell(C.AVAILABLE)
    if cam.projection_source == "provided" and camera_id in sample.consistency.get("cameras", {}):
        cells[C.INPUT_CONSISTENCY] = _cell(C.AVAILABLE)
    elif cam.projection_source == "provided":
        cells[C.INPUT_CONSISTENCY] = _cell(C.UNSUPPORTED, _projection_reason_3d(sample, camera_id))
    else:
        cells[C.INPUT_CONSISTENCY] = _cell(C.UNSUPPORTED, C.PROJECTION_MISSING)
    return {**info, "subitems": cells, "comparable_points": comparable, "observable_axes": axes}


def _projection_reason_3d(sample: EefSample, camera_id: str) -> str:
    """Why a provided projection cannot be recomputed."""
    if not sample.eef_mask.any():
        return C.POSE_SEMANTICS_UNKNOWN
    if not sample.has_absolute_pose:
        return C.ANCHOR_POSE_MISSING
    return C.CALIBRATION_MISSING


def state_motion_capability(sample: EefSample) -> dict[str, Any]:
    if not sample.eef_mask.any():
        return _cell(C.UNSUPPORTED, C.POSE_SEMANTICS_UNKNOWN)
    if sample.timebase != "video_pts" and "robot_state" not in sample.clocks:
        return _cell(C.UNSUPPORTED, C.TIMEBASE_INDEX_ONLY)
    if int(sample.eef_mask.sum()) < 16:
        return _cell(C.UNSUPPORTED, C.TOO_FEW_STATES)
    return _cell(C.AVAILABLE)


def _best(cells: Iterable[dict]) -> dict:
    cells = list(cells)
    if not cells:                                        # no camera view at all (plain images)
        return _cell(C.UNSUPPORTED, C.PROJECTION_MISSING)
    return dict(min(cells, key=lambda c: _RANK[c["availability"]]))


def sample_capability(sample: EefSample, *, observable: Mapping[str, Iterable[str]] | None = None,
                      vlm_backend: bool = False,
                      allowed_mounts: Iterable[str] = DEFAULT_ALLOWED_MOUNTS) -> dict[str, Any]:
    """The design 12 §5.1 table for one episode. ``observable`` maps camera id -> observed point ids."""
    cams = {}
    for cid in sample.cameras:
        obs = None if observable is None else observable.get(cid)
        cams[cid] = camera_capability(sample, cid, observable=obs, allowed_mounts=allowed_mounts)
    subitems = {k: _best(c["subitems"][k] for c in cams.values()) for k in C.CAMERA_SUBITEMS}
    subitems[C.STATE_MOTION] = state_motion_capability(sample)
    subitems[C.VLM_REVIEW] = _cell(C.NEEDS_INPUT, C.VLM_BACKEND_MISSING) if not vlm_backend \
        else _cell(C.UNSUPPORTED, "review_not_in_first_cut")
    ordered = {k: subitems[k] for k in C.SUBITEMS}
    core = {k: ordered[k] for k in CORE_SUBITEMS}
    return {"module": C.MODULE_ID, "episode_index": sample.episode_index, "sample_id": sample.sample_id,
            "availability": C.overall_availability(core), "subitems": ordered, "cameras": cams}


def dataset_capability(result: LoadResult | None, episodes: Iterable[int], *,
                       observable: Mapping[int, Mapping[str, Iterable[str]]] | None = None,
                       vlm_backend: bool = False,
                       allowed_mounts: Iterable[str] = DEFAULT_ALLOWED_MOUNTS) -> dict[str, Any]:
    """Module-level preflight over the task's episodes (design 12 §0.5: a missing episode is
    ``unsupported: projection_missing`` for this module only)."""
    episodes = list(episodes)
    if result is None:
        return {"availability": C.NEEDS_INPUT, "reason_code": C.TRAJECTORY_MISSING, "episodes": {},
                "counts": {}}
    if not result.ok:
        return {"availability": C.NEEDS_INPUT, "reason_code": C.TRAJECTORY_INVALID, "episodes": {},
                "counts": {}, "errors": [i.as_dict() for i in result.errors[:20]]}
    per: dict[int, dict] = {}
    for ep in episodes:
        sample = result.samples.get(ep)
        if sample is None:
            per[ep] = {"availability": C.UNSUPPORTED, "reason_code": C.PROJECTION_MISSING}
            continue
        obs = None if observable is None else observable.get(ep, {})
        cap = sample_capability(sample, observable=obs, vlm_backend=vlm_backend, allowed_mounts=allowed_mounts)
        per[ep] = {"availability": cap["availability"],
                   "subitems": {k: v["availability"] for k, v in cap["subitems"].items()},
                   "reasons": sorted({v["reason_code"] for v in cap["subitems"].values() if v["reason_code"]})}
    counts: dict[str, int] = {}
    for v in per.values():
        counts[v["availability"]] = counts.get(v["availability"], 0) + 1
    if counts.get(C.AVAILABLE):
        av, reason = C.AVAILABLE, None
    elif counts.get(C.NEEDS_INPUT):
        av = C.NEEDS_INPUT
        reason = next((r for v in per.values() for r in v.get("reasons", []) if r), C.OBSERVATION_SEED_MISSING)
    else:
        av, reason = C.UNSUPPORTED, C.PROJECTION_MISSING
    extra = [e for e in result.samples if e not in set(episodes)]
    return {"availability": av, "reason_code": reason, "episodes": per, "counts": counts,
            "samples_outside_dataset": sorted(extra)}
