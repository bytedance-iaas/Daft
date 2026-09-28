"""A draft of the record mapping from a LeRobot dataset's metadata (design 12 §8.7, D-E17).

``info.json`` already says half of what the record comparison needs: every column's component names
(``x, y, z, roll, pitch, yaw``, ``joint_0 ... joint_6``) and the ``robot_type``. What it does not say -
units, the Euler convention, which point a pose describes, which column is the measurement, the robot's
link lengths - is filled in by convention, and every such step is listed as an assumption (a code and
its arguments) for a person to confirm in the form before the draft is used; nothing here is applied
unseen. A source that cannot be drafted unambiguously is left out, with the reason.

Rules: only ``observation.*`` columns (measured; ``action.*`` is the command, which leads the measurement)
and none with ``velocity`` in the name; a pose is ``x y z roll pitch yaw`` or ``x y z`` plus a quaternion,
possibly a contiguous run inside a longer vector (then a ``slice``); joints are the robot's number of
``joint_N`` names in a row, a column of only those names preferred over a longer one; the robot comes
from ``robot_type``; units are metres and radians; the pose's point is not declared (``frame_id: null``)
and both records are in the upload's base (``reference_frame: "@upload"``).
"""
from __future__ import annotations

import re
from typing import Any

from . import record as RC
from . import robots as RB

#: component names of a pose, the layout they mean and the assumption that reading takes
POSE_NAMES: tuple[tuple[tuple[str, ...], str, dict], ...] = (
    (("x", "y", "z", "roll", "pitch", "yaw"), "xyz_rpy_xyz_extrinsic", {"code": "euler_extrinsic_xyz"}),
    (("x", "y", "z", "qx", "qy", "qz", "qw"), "xyz_quat_xyzw", {"code": "quaternion_order", "args": {"order": "xyzw"}}),
    (("x", "y", "z", "qw", "qx", "qy", "qz"), "xyz_quat_wxyz", {"code": "quaternion_order", "args": {"order": "wxyz"}}),
)
JOINT_NAME = re.compile(r"^(?:panda_)?joint_?(\d+)$")
NUMERIC = ("float16", "float32", "float64")


def _names(feature: Any) -> list[str] | None:
    """A feature's component names (a list, or LeRobot's ``{"axes": [...]}`` / ``{"motors": [...]}``)."""
    if not isinstance(feature, dict) or feature.get("dtype") not in NUMERIC:
        return None
    names = feature.get("names")
    if isinstance(names, dict):
        names = next((v for v in names.values() if isinstance(v, list)), None)
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        return None
    return [n.strip().lower() for n in names]


def _measured(key: str) -> bool:
    return key.startswith("observation.") and "velocity" not in key.lower()


def _command(key: str) -> bool:
    return key == "action" or key.startswith("action.")


def _run(names: list[str], pattern: tuple[str, ...]) -> int | None:
    n = len(pattern)
    return next((i for i in range(len(names) - n + 1) if tuple(names[i:i + n]) == pattern), None)


def _joint_run(names: list[str], joints: int) -> int | None:
    """Where the robot's ``joints`` joint names stand in a row with consecutive numbers - only when the
    column holds exactly that many joint names (a two-arm column is not one arm's)."""
    at = [i for i, n in enumerate(names) if JOINT_NAME.match(n)]
    if len(at) != joints or at != list(range(at[0], at[0] + joints)):
        return None
    nums = [int(JOINT_NAME.match(names[i]).group(1)) for i in at]
    return at[0] if nums == list(range(nums[0], nums[0] + joints)) else None


def _robot(robot_type: Any) -> tuple[str | None, dict | None]:
    t = robot_type.strip().lower() if isinstance(robot_type, str) else ""
    if not t:
        return None, {"code": "no_robot_type", "source": "joints"}
    if "fr3" in t:
        return "franka_fr3", None
    if "franka" in t or "panda" in t:
        return "franka_panda", None
    return None, {"code": "robot_not_built_in", "source": "joints",
                  "args": {"robot_type": robot_type.strip(), "known": list(RB.ROBOTS)}}


def _pick(source: str, found: list[dict], commands: list[str]) -> tuple[dict | None, list[dict], dict | None]:
    """(the chosen candidate, its assumptions, or why none): a column of only the matched names beats
    one where they are a run inside a longer vector; two of the same rank are ambiguous."""
    dedicated = [c for c in found if c["slice"] is None]
    rank = dedicated or found
    if not rank:
        return None, [], None
    if len(rank) > 1:
        return None, [], {"code": "ambiguous", "source": source, "args": {"candidates": [c["key"] for c in rank]}}
    chosen = rank[0]
    notes = []
    others = [c["key"] for c in found if c is not chosen]
    if others:
        notes.append({"code": "picked_dedicated", "source": source, "args": {"used": chosen["key"], "others": others}})
    if chosen["slice"] is not None:
        notes.append({"code": "slice_from_names", "source": source,
                      "args": {"key": chosen["key"], "slice": list(chosen["slice"])}})
    if commands:
        notes.append({"code": "observation_not_action", "source": source,
                      "args": {"used": chosen["key"], "skipped": commands}})
    return chosen, notes, None


def _pose(features: dict) -> tuple[dict | None, list[dict], dict | None]:
    found, commands = [], []
    for key, feat in features.items():
        names = _names(feat)
        if names is None or "velocity" in key.lower():
            continue
        for pattern, layout, note in POSE_NAMES:
            at = _run(names, pattern)
            if at is None:
                continue
            if _command(key):
                commands.append(key)
            elif _measured(key):
                sl = None if len(names) == len(pattern) else (at, at + len(pattern))
                found.append({"key": key, "slice": sl, "layout": layout, "note": note})
            break
    chosen, notes, why = _pick("pose", found, commands)
    if chosen is None:
        return None, [], why or {"code": "no_named_pose_column", "source": "pose"}
    spec = {"key": chosen["key"], **({"slice": list(chosen["slice"])} if chosen["slice"] else {}),
            "layout": chosen["layout"], "units": {"position": "m", "angle": "rad"},
            "frame_id": None, "reference_frame": RC.UPLOAD_FRAME}
    notes = notes + [{**chosen["note"], "source": "pose"},
                     {"code": "units_by_convention", "source": "pose", "args": {"position": "m", "angle": "rad"}},
                     {"code": "pose_frame_undeclared", "source": "pose"},
                     {"code": "same_base_as_upload", "source": "pose"}]
    return spec, notes, None


def _joints(features: dict, robot_type: Any) -> tuple[dict | None, list[dict], dict | None]:
    robot, why = _robot(robot_type)
    if robot is None:
        return None, [], why
    model = RB.get(robot)
    found, commands = [], []
    for key, feat in features.items():
        names = _names(feat)
        if names is None or "velocity" in key.lower():
            continue
        at = _joint_run(names, model.joints)
        if at is None:
            continue
        if _command(key):
            commands.append(key)
        elif _measured(key):
            sl = None if len(names) == model.joints else (at, at + model.joints)
            found.append({"key": key, "slice": sl})
    chosen, notes, why = _pick("joints", found, commands)
    if chosen is None:
        return None, [], why or {"code": "no_named_joint_column", "source": "joints", "args": {"joints": model.joints}}
    spec = {"key": chosen["key"], **({"slice": list(chosen["slice"])} if chosen["slice"] else {}),
            "units": "rad", "robot": robot, "reference_frame": RC.UPLOAD_FRAME}
    notes = [{"code": "robot_from_robot_type", "source": "joints",
              "args": {"robot_type": str(robot_type).strip(), "robot": robot}}] + notes + [
        {"code": "units_by_convention", "source": "joints", "args": {"angle": "rad"}},
        {"code": "joints_tip_frame", "source": "joints",
         "args": {"frame": model.tip_frame, "named": [model.tip_frame, *model.frames]}},
        {"code": "same_base_as_upload", "source": "joints"}]
    return spec, notes, None


def draft(info: dict) -> dict:
    """``{"document", "assumptions", "not_drafted"}`` for a LeRobot ``info.json``: ``document`` is an
    ``eef-mapping/1.1`` mapping (None when neither source could be drafted)."""
    features = info.get("features") if isinstance(info, dict) else None
    features = features if isinstance(features, dict) else {}
    record: dict = {}
    assumptions: list[dict] = []
    not_drafted: list[dict] = []
    for kind, (spec, notes, why) in (("pose", _pose(features)), ("joints", _joints(features, info.get("robot_type")))):
        if spec is None:
            not_drafted.append(why)
        else:
            record[kind] = spec
            assumptions += notes
    document = {"schema_version": "eef-mapping/1.1", "record": record} if record else None
    if document is not None:
        RC.parse_mapping(document)                   # our own draft always parses; a failure here is a bug
    return {"document": document, "assumptions": assumptions, "not_drafted": not_drafted}


def not_drafted(fmt: str) -> dict:
    """The draft entry of a dataset whose columns are not LeRobot's (mcap topics, Lance tables)."""
    return {"document": None, "assumptions": [],
            "not_drafted": [{"code": "format_not_drafted", "args": {"format": fmt}}]}
