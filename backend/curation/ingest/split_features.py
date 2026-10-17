"""Streams a dataset splits over several columns, composed into the one the pipeline reads.

Galaxea's LeRobot export (R1 Lite, 2026-10) has no top-level ``action`` or ``observation.state``: the command is
``action.left_arm`` (6) / ``action.right_arm`` (6) / ``action.left_gripper`` (1) / ``action.right_gripper`` (1) /
``action.chassis.velocities`` (3) / ``action.torso.velocities`` (6), the reading ``observation.state.left_arm`` …
plus ``.velocities`` twins, ``observation.state.chassis.imu``, the end-effector poses. The pipeline reads one
``action`` and one ``observation.state`` per frame (``ingest.schema``), so the reader composes them:

* the sub-columns of a stream are the numeric vector features named ``<stream>.<suffix>``; the ones whose suffix
  says they are rates or sensor readings (``velocities`` / ``velocity`` / ``vel`` / ``imu`` / ``accel`` / ``effort``
  / ``torque`` / ``current`` / ``temperature``) are left out - a velocity command is not a position command, and an
  IMU reading would never let the robot look at rest; once a stream has columns paired with the other stream, its
  unpaired poses (``left_ee_pose``: ``pose`` / ``ee`` / ``tcp`` / ``cartesian``) are left out too - they are the
  joints again, and their x / y / z names would make a joint-space reading pass for an end-effector one;
* the columns an action and a state share by suffix and width (``left_arm`` ↔ ``left_arm``) come first in both,
  in the action's order, so the "command vs reading" checks compare like with like by index; the rest follow;
* every dimension gets a name ``<suffix>.<raw name without its topic directory>``
  (``left_arm.target_joint_state_arm_left.position[0]``), so the semantics layer's name rules (joint / gripper /
  rotation words) still see them.

The composition is a reading rule, not a declaration: ``info.json`` on disk is untouched, the reader's copy of it
carries the virtual feature (``composed_from``) and ``composed_streams`` says what was taken and what was left out;
the preflight reports the same. A dataset that has the top-level stream is read as before.
"""
from __future__ import annotations

import re

import numpy as np

#: the streams the pipeline reads as one column each
STREAMS = ("action", "observation.state")
#: a suffix token that says the column is a rate or a sensor reading, not a position
RATE_TOKENS = frozenset(("velocities", "velocity", "vel", "imu", "accel", "acceleration", "effort", "efforts",
                         "torque", "torques", "current", "currents", "temperature", "temperatures"))
#: a suffix token that says the column is a pose derived from the joints (``left_ee_pose``): left out when the
#: stream already has columns paired with the other stream - its x / y / z and quaternion names would otherwise
#: make a joint-space reading look like an end-effector one, and the "command vs reading" checks step aside
DERIVED_TOKENS = frozenset(("pose", "ee", "eef", "tcp", "cartesian"))
_NUMERIC = ("float", "int", "double")


def _tokens(text: str) -> list[str]:
    return [w for w in re.split(r"[^A-Za-z]+", str(text).lower()) if w]


def _vector(feature: dict) -> int | None:
    """The width of a numeric vector feature, else None."""
    dtype = str(feature.get("dtype") or "")
    if not any(dtype.startswith(p) for p in _NUMERIC):
        return None
    shape = feature.get("shape") or []
    if len(shape) != 1:
        return None
    try:
        return int(shape[0])
    except (TypeError, ValueError):
        return None


def _names_of(feature: dict, width: int) -> list:
    names = feature.get("names") or []
    if isinstance(names, dict):
        names = next(iter(names.values()), [])
    if len(names) == 1 and isinstance(names[0], (list, tuple)):
        names = list(names[0])
    return list(names) if len(names) == width else [None] * width


def short_name(raw, suffix: str, i: int) -> str:
    """``/hdas/feedback_arm_left.position[0]`` of ``left_arm`` -> ``left_arm.feedback_arm_left.position[0]``: the
    topic's directory dropped, its own words kept (``joint`` / ``gripper`` / ``pose`` are what the name rules read);
    a dimension without a name is ``left_arm[0]``."""
    if raw is None or str(raw) == "":
        return f"{suffix}[{i}]"
    s = str(raw).rsplit("/", 1)[-1].strip()
    return f"{suffix}.{s}" if s else f"{suffix}[{i}]"


def composition(features: dict) -> dict:
    """{stream: {"columns": [...], "left_out": [...], "paired": [...], "dim": n, "names": [...]}} for every stream
    the features split instead of declaring; {} when nothing is split."""
    out: dict = {}
    for stream in STREAMS:
        if stream in features:
            continue
        prefix = stream + "."
        subs = [(k, _vector(f)) for k, f in features.items() if k.startswith(prefix) and isinstance(f, dict)]
        subs = [(k, w) for k, w in subs if w]
        if not subs:
            continue
        kept = [k for k, _ in subs if not (set(_tokens(k[len(prefix):])) & RATE_TOKENS)]
        if not kept:
            continue
        out[stream] = {"columns": kept, "left_out": [k for k, _ in subs if k not in kept],
                       "widths": {k: w for k, w in subs}}
    if "action" in out and "observation.state" in out:
        a, s = out["action"], out["observation.state"]
        s_by = {k[len("observation.state."):]: k for k in s["columns"]}
        paired = [k[len("action."):] for k in a["columns"]
                  if k[len("action."):] in s_by and a["widths"][k] == s["widths"][s_by[k[len("action."):]]]]
        a["columns"] = [f"action.{p}" for p in paired] + [k for k in a["columns"] if k[len("action."):] not in paired]
        s["columns"] = [s_by[p] for p in paired] + [k for k in s["columns"] if k[len("observation.state."):] not in paired]
        a["paired"] = s["paired"] = list(paired)
        if paired:
            for stream, c in (("action", a), ("observation.state", s)):
                derived = [k for k in c["columns"][len(paired):] if set(_tokens(k[len(stream) + 1:])) & DERIVED_TOKENS]
                c["columns"] = [k for k in c["columns"] if k not in derived]
                c["left_out"] = c["left_out"] + derived
    for stream, c in out.items():
        names = []
        for k in c["columns"]:
            suffix = k[len(stream) + 1:]
            w = c["widths"][k]
            names.extend(short_name(n, suffix, i) for i, n in enumerate(_names_of(features[k], w)))
        c.setdefault("paired", [])
        c["dim"] = len(names)
        c["names"] = names
        del c["widths"]
    return out


def compose(info: dict) -> dict:
    """Give ``info`` (the reader's copy) the virtual features its split streams make up; returns the composition.
    Idempotent."""
    if "composed_streams" in info:
        return info["composed_streams"]
    feats = info.get("features") or {}
    comp = composition(feats)
    for stream, c in comp.items():
        feats[stream] = {"dtype": "float32", "shape": [c["dim"]], "names": list(c["names"]),
                         "composed_from": list(c["columns"])}
    info["composed_streams"] = comp
    return comp


def stream_array(frame, info: dict, stream: str, dtype=np.float32) -> np.ndarray:
    """``[T, dim]`` of a stream of a pandas frame: the column itself, or the composed sub-columns side by side."""
    comp = (info.get("composed_streams") or {}).get(stream)
    if not comp:
        return np.stack(frame[stream].to_numpy()).astype(dtype)
    parts = []
    for col in comp["columns"]:
        v = frame[col].to_numpy()
        arr = np.stack(v) if v.dtype == object else np.asarray(v)
        if arr.ndim == 1:
            arr = arr[:, None]
        parts.append(arr.astype(dtype))
    return np.concatenate(parts, axis=1)


def describe(comp: dict) -> list[str]:
    """One line per composed stream, for the preflight's warnings."""
    out = []
    for stream, c in comp.items():
        line = f"{stream} is composed from {len(c['columns'])} columns ({', '.join(c['columns'])}), {c['dim']} dimensions"
        if c.get("left_out"):
            line += f"; left out as rates, sensor readings or derived poses: {', '.join(c['left_out'])}"
        out.append(line)
    return out
