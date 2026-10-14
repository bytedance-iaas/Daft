"""What the dataset itself says about its arms and its cameras - the other half of MV-2.

Whether a camera's identity is consistent is a COMPARISON: the picture against what the dataset
claims that camera is. Without the claim there is nothing to compare, and the model is left to guess
from a camera's name - which on FastUMI (two handheld grippers, two wrist cameras, no third-person
view) made it answer "single-arm setup", so the check never ran.

The claim has two sources, both facts rather than guesses:

* the dataset declaration (C7 ``dataset-declaration/1.0``, design doc 25 §3) the task froze, whose
  ``calibration.cameras`` says each camera's ``mount`` (wrist / fixed_external) and ``owner`` (the
  hand or arm it rides);
* the dataset's own metadata, whose action and state channel names say how many arms there are and
  what they are called (``left_x … left_gripper, right_x … right_gripper`` is two arms, named left
  and right, whatever ``robot_type`` says - FastUMI leaves that null).

Nothing here guesses: a camera the declaration does not cover, and a channel layout with no arm in
it, are simply left out of the claim, and the request then asks the model for nothing about them.
"""
from __future__ import annotations

#: the arm words a channel prefix may carry; ``robotN`` is the mcap / GenRobot spelling
_SIDES = ("left", "right")
MOUNTS = ("wrist", "fixed_external")


def _names_of(info: dict, key: str) -> list[str]:
    """The channel names of ``info['features'][key]``, flat (LeRobot writes a dict of groups)."""
    names = ((info.get("features") or {}).get(key) or {}).get("names") or []
    if isinstance(names, dict):
        names = [n for group in names.values() for n in (group if isinstance(group, list) else [group])]
    return [str(n) for n in names if isinstance(n, (str, int, float))]


def arms_of(info: dict | None) -> list[str]:
    """The arms the dataset's own channel names show, in the order they appear.

    ``left_x … right_gripper`` -> ``["left", "right"]``; ``robot0_* / robot1_*`` -> ``["robot0",
    "robot1"]``; a flat ``x, y, z, roll, pitch, yaw, gripper`` -> ``[]`` (one arm, nothing to tell
    apart). The names are the dataset's, so this is what it claims, not what a picture suggests.
    """
    if not isinstance(info, dict):
        return []
    out: list[str] = []
    for key in ("action", "observation.state"):
        for name in _names_of(info, key):
            head = name.split("_", 1)[0].strip().lower()
            if head in _SIDES or (head.startswith("robot") and head[5:].isdigit()):
                if head not in out:
                    out.append(head)
        if len(out) > 1:
            return out
    return out if len(out) > 1 else []


def cameras_of(declaration: dict | None) -> dict[str, dict]:
    """Per camera source, what the declaration says it is: ``{mount, owner}``; empty without one."""
    if not isinstance(declaration, dict):
        return {}
    cams = ((declaration.get("calibration") or {}).get("cameras") or {})
    out: dict[str, dict] = {}
    for source, spec in cams.items() if isinstance(cams, dict) else []:
        if not isinstance(spec, dict):
            continue
        mount = str(spec.get("mount") or "")
        owner = spec.get("owner")
        said = {}
        if mount in MOUNTS:
            said["mount"] = mount
        if isinstance(owner, str) and owner.strip():
            said["owner"] = owner.strip()
        if said:
            out[str(source)] = said
    return out


def channels_of(info: dict | None, arms: list[str]) -> dict[str, list[int]]:
    """Per arm, which action columns move it - the numbers MV-2 is measured from.

    ``left_x … left_yaw`` are column 0..5 of a FastUMI action, ``right_*`` 7..12. Grippers are left
    out: closing a gripper does not swing the camera bolted beside it, so counting it would only add
    noise to the arm's speed.
    """
    out: dict[str, list[int]] = {arm: [] for arm in arms}
    for i, name in enumerate(_names_of(info or {}, "action")):
        head = name.split("_", 1)[0].strip().lower()
        if head in out and not name.strip().lower().endswith("gripper"):
            out[head].append(i)
    return {arm: cols for arm, cols in out.items() if len(cols) >= 2}


def build(declaration: dict | None, info: dict | None) -> dict:
    """The dataset's claim: arms, each arm's action columns, and what it says each camera is."""
    arms = arms_of(info)
    return {"arms": arms, "channels": channels_of(info, arms),
            "cameras": cameras_of(declaration)}


def text(claim: dict | None, cameras: list[str]) -> str:
    """The claim as the two lines the request carries, limited to the cameras it sends; "" when the
    dataset says nothing worth comparing against."""
    claim = claim or {}
    arms = [a for a in (claim.get("arms") or []) if isinstance(a, str)]
    said = claim.get("cameras") or {}
    lines: list[str] = []
    if len(arms) > 1:
        lines.append(f"This robot has {len(arms)} arms, which the dataset's own channel names call "
                     f"{' and '.join(arms)}.")
    elif arms:
        lines.append("This robot has one arm.")
    rows = []
    for cam in cameras:
        spec = said.get(cam) or {}
        parts = []
        if spec.get("mount") == "wrist":
            parts.append("a wrist camera")
        elif spec.get("mount") == "fixed_external":
            parts.append("a fixed third-person camera")
        if spec.get("owner"):
            parts.append(f"on {spec['owner']}")
        if parts:
            rows.append(f"{cam} is declared {' '.join(parts)}")
    if rows:
        lines.append("The dataset declares: " + "; ".join(rows) + ".")
    return " ".join(lines)
