"""Enums, reason codes and small records shared by the EEF-video consistency module.

Design: docs/design/12-eef-video-consistency.md. Input contract: docs/contracts/eef/.
Nothing here performs I/O.
"""
from __future__ import annotations

import dataclasses
from typing import Any

SCHEMA_VERSION = "eef-video/1.0.0"
CONTAINER = "trajectory-bundle/1.0"
MODULE_ID = "eef_video_consistency"
REVIEW_MODULE_ID = "eef_video_review"
MODULE_VERSION = "0.1.0-demo"
#: Version of the per-episode ``detail`` payload written into result records.
DETAIL_SCHEMA_VERSION = "eef-detail/0.1"

#: Keys that carry evaluation truth; any occurrence rejects an input file (design 12 §3.7, D-E5).
FORBIDDEN_KEYS = frozenset({"truth", "ground_truth", "corruption_type", "fault"})

#: Provided and recomputed projections may differ by the source quantisation only (design 12 §3.7).
REPROJECTION_TOLERANCE_PX = 0.05

# --- capability ------------------------------------------------------------------------------

AVAILABLE, NEEDS_INPUT, UNSUPPORTED = "available", "needs_input", "unsupported"
AVAILABILITY = (AVAILABLE, NEEDS_INPUT, UNSUPPORTED)

POSITION = "position_2d"
ORIENTATION = "orientation_2d"
TEMPORAL = "temporal_alignment"
STATE_MOTION = "state_motion"
CAMERA_MOTION = "camera_motion"
VLM_REVIEW = "vlm_review"
INPUT_CONSISTENCY = "input_consistency"
#: Sub-items of the preflight capability table and of the per-camera assessment (design 12 §5.1).
SUBITEMS = (POSITION, ORIENTATION, TEMPORAL, STATE_MOTION, CAMERA_MOTION, VLM_REVIEW, INPUT_CONSISTENCY)
#: Sub-items measured per camera (D-E4); state_motion is per episode.
CAMERA_SUBITEMS = (POSITION, ORIENTATION, TEMPORAL, CAMERA_MOTION, INPUT_CONSISTENCY)

# --- status ----------------------------------------------------------------------------------

OK, SUSPECT, UNKNOWN, ERROR = "ok", "suspect", "unknown", "error"
STATUSES = (OK, SUSPECT, UNKNOWN, UNSUPPORTED, ERROR)

# --- reason codes (design 12 §5.1; the catalogue is open, additions are listed in the doc) ------

PROJECTION_MISSING = "projection_missing"
POSE_SEMANTICS_UNKNOWN = "pose_semantics_unknown"
ANCHOR_POSE_MISSING = "anchor_pose_missing"
CALIBRATION_MISSING = "calibration_missing"
CALIBRATION_DIRECTION_AMBIGUOUS = "calibration_direction_ambiguous"
VIDEO_MISSING = "video_missing"
MEDIA_TRANSFORM_UNKNOWN = "media_transform_unknown"
CLOCK_ALIGNMENT_UNKNOWN = "clock_alignment_unknown"
VISUAL_POINT_MAPPING_MISSING = "visual_point_mapping_missing"
AXIS_MAPPING_MISSING = "axis_mapping_missing"
MOVING_CAMERA_UNSUPPORTED = "moving_camera_unsupported"
WRIST_CAMERA_SPATIAL_ONLY = "wrist_camera_spatial_only"
DECODE_FAILED = "decode_failed"
VLM_BACKEND_MISSING = "vlm_backend_missing"
# additions of the first cut (documented in design 12 §5.1 revision notes)
TRAJECTORY_MISSING = "trajectory_missing"
EEF_BASE_UNAVAILABLE = "eef_base_unavailable"          # the review: the module it reviews is unusable
TRAJECTORY_INVALID = "trajectory_invalid"
OBSERVATION_SEED_MISSING = "observation_seed_missing"   # no seeds and no gripper template (F5.8)
TEMPLATE_INVALID = "template_invalid"
TIMEBASE_INDEX_ONLY = "timebase_index_only"
CAMERA_MOUNT_NOT_ALLOWED = "camera_mount_not_allowed"
INPUT_INCONSISTENT = "input_inconsistent"
THRESHOLD_UNCALIBRATED = "threshold_uncalibrated"
COVERAGE_INSUFFICIENT = "coverage_insufficient"
ORIENTATION_NOT_OBSERVABLE = "orientation_not_observable"
LAG_NOT_IDENTIFIABLE = "lag_not_identifiable"
BACKGROUND_SUPPORT_INSUFFICIENT = "background_support_insufficient"
TRACKING_UNSTABLE = "tracking_unstable"
TOO_FEW_STATES = "too_few_states"
EXECUTION_FAILED = "execution_failed"


@dataclasses.dataclass(frozen=True)
class Issue:
    """One validation finding, located as precisely as the input allows (design 12 §3.7)."""

    severity: str                      # "error" rejects the file; "warning" is reported and kept
    code: str
    message: str
    episode_index: int | None = None
    sample_id: str | None = None
    frame_index: int | None = None
    camera_id: str | None = None
    point_id: str | None = None
    path: str | None = None            # JSON path inside the bundle, e.g. samples/3/frames/12/eef

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


def subitem(availability: str, reason_code: str | None = None, **extra: Any) -> dict[str, Any]:
    """A capability cell: ``{"availability": ..., "reason_code": ...}`` plus optional detail."""
    assert availability in AVAILABILITY, availability
    return {"availability": availability, "reason_code": reason_code, **extra}


def overall_availability(cells: dict[str, dict[str, Any]]) -> str:
    """Any sub-item available -> available; all unsupported -> unsupported; else needs_input."""
    values = [c["availability"] for c in cells.values()]
    if AVAILABLE in values:
        return AVAILABLE
    if values and all(v == UNSUPPORTED for v in values):
        return UNSUPPORTED
    return NEEDS_INPUT
