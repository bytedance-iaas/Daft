"""Drafting a dataset's declaration (design doc 25 §3.3): what the platform can read off the dataset, and what it
can only assume, each assumption named for a person to confirm in the table.

- **mount**: a camera's key or topic with ``wrist`` / ``hand`` / ``gripper_cam`` / ``eye_in_hand`` is on a wrist;
  ``exterior`` / ``external`` / ``front`` / ``side`` / ``top`` / ``third`` / ``overhead`` / ``table`` a third-person
  camera; ``head`` / ``chest`` / ``body`` a moving one (a humanoid's head camera moves with no pose of its own);
  otherwise unknown - asked. The built-in UMI layout's ``/robotN/sensor/cameraN`` is robotN's wrist camera.
- **owner** of a wrist camera: the arm when there is one, ``robotN`` / ``left`` / ``right`` by name, else asked.
- **intrinsics**: a LeRobot ``meta/*calibration*.json`` (``cameras.<camera>`` with ``K`` or ``intrinsics_fx_cx_fy_cy``),
  an mcap ``CameraCalibration`` / ``CameraInfo`` topic (read per recording), else left empty - to be supplied.
- **extrinsics**: a per-row column named like ``camera_extrinsics.<camera>`` with ``x y z roll pitch yaw``; else empty.
- **pose / joints**: as the record mapping's draft (``record_draft``: component names and ``robot_type``); the pose's
  point is the robot's flange when ``robot_type`` names a robot the platform knows, assumed.
- **gripper**: a measured column named ``gripper`` (0 open .. 1 closed assumed).
- **tool**: only pre-filled - the official gripper of the robot ``robot_type`` names (Franka Hand: TCP 0.1034 m along
  z, fingers along y, 0.08 m open), or a pose that already is the TCP (``tcp`` / ``ee`` in its name); assumed.
- **handheld gripper**: the built-in DAS DEMO calibration, every value assumed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable

from . import ASSUMED, DECLARED, FIXED_EXTERNAL, MOVING, SCHEMA_VERSION, WRIST, short_name

WRIST_WORDS = ("wrist", "hand", "gripper_cam", "gripper-cam", "eye_in_hand", "eye-in-hand")
EXTERNAL_WORDS = ("exterior", "external", "front", "side", "top", "third", "overhead", "table")
MOVING_WORDS = ("head", "chest", "body")
UMI_CAMERA = re.compile(r"^/(robot\d+)/sensor/camera\d+/")
SIDE = re.compile(r"(?:^|[^a-z])(left|right)(?:[^a-z]|$)")
ROBOT_N = re.compile(r"(robot\d+)")
#: the official gripper of each robot the platform knows, in its flange frame (design doc 25 §3.2)
TOOLS = {
    "franka_panda": {"model": "franka_hand", "tcp_offset_m": [0.0, 0.0, 0.1034], "finger_axis": "local_y",
                     "max_opening_m": 0.08, "axes": {"from": "tcp", "length_m": 0.06}},
    "franka_fr3": {"model": "franka_hand", "tcp_offset_m": [0.0, 0.0, 0.1034], "finger_axis": "local_y",
                   "max_opening_m": 0.08, "axes": {"from": "tcp", "length_m": 0.06}},
}
POSE_FRAMES = {"franka_panda": "panda_link8", "franka_fr3": "fr3_link8"}
REFERENCE_FRAME = "robot_base"
CLOCK_UNITS = (("_ns", "ns"), ("_us", "us"), ("_ms", "ms"), ("_s", "s"))
CALIBRATION_FILE = re.compile(r"^meta/[^/]*calibration[^/]*\.json$")
UMI_CALIBRATION = "meta/umi_calibration.json"


def _a(code: str, **args) -> dict:
    return {"code": code, **({"args": args} if args else {})}


def mount_of(source: str) -> tuple[str | None, list[dict]]:
    """(the mount a camera's key or topic suggests, the assumption it takes); (None, []) when it says nothing."""
    s = str(source).lower()
    m = UMI_CAMERA.match(str(source))
    if m:
        return WRIST, [_a("mount_from_umi_template", hand=m.group(1))]
    for mount, words in ((WRIST, WRIST_WORDS), (FIXED_EXTERNAL, EXTERNAL_WORDS), (MOVING, MOVING_WORDS)):
        hit = next((w for w in words if w in s), None)
        if hit:
            return mount, [_a("mount_from_keyword", word=hit)]
    return None, []


def owner_of(source: str, wrists: int, arms: int = 1) -> tuple[str | None, list[dict]]:
    """A wrist camera's hand or arm: ``robotN`` / left / right in its name, the only arm, else unknown."""
    s = str(source).lower()
    m = UMI_CAMERA.match(str(source)) or ROBOT_N.search(s)
    if m:
        return m.group(1), [_a("owner_from_name", owner=m.group(1))]
    side = SIDE.search(s.replace(".", "_").replace("/", "_"))
    if side and arms != 1:
        return side.group(1), [_a("owner_from_name", owner=side.group(1))]
    if arms == 1:
        return "arm", [_a("owner_single_arm")]
    return None, []


def _robot(robot_type: Any) -> str | None:
    from ..extensions.eef_consistency.record_draft import _robot as robot

    return robot(robot_type)[0]


# ---------------------------------------------------------------- LeRobot / Lance (info.json)

def _video_keys(info: dict) -> list[str]:
    return [k for k, f in (info.get("features") or {}).items()
            if isinstance(f, dict) and f.get("dtype") in ("video", "image") and "depth" not in k.lower()]


def _names(feat: Any) -> list[str] | None:
    from ..extensions.eef_consistency.record_draft import _names as names

    return names(feat)


def _gripper(features: dict) -> tuple[dict | None, list[dict]]:
    """A measured gripper column: one named ``gripper`` alone beats a ``gripper`` element of a wider vector."""
    alone, inside = [], []
    for key, feat in features.items():
        if not key.startswith("observation.") or "velocity" in key.lower():
            continue
        names = _names(feat)
        if names is None:
            continue
        hits = [i for i, n in enumerate(names) if "gripper" in n or n in ("opening", "width")]
        if len(names) == 1 and (hits or "gripper" in key.lower()):
            alone.append(key)
        elif len(hits) == 1:
            inside.append((key, hits[0]))
    if alone:
        key = sorted(alone, key=lambda k: (k.count("."), k))[0]
        spec = {"key": key}
    elif inside:
        key, i = sorted(inside, key=lambda c: (c[0].count("."), c[0]))[0]
        spec = {"key": key, "index": i}
    else:
        return None, []
    notes = [_a("gripper_from_name", key=spec["key"]), _a("closed_fraction_identity")]
    return {**spec, "closed_fraction": "identity", "assurance": ASSUMED, "assumptions": notes}, notes


def _extrinsic_column(features: dict, camera: str) -> str | None:
    """A per-row camera pose column for ``camera`` (DROID's ``camera_extrinsics.<camera>``): x y z roll pitch yaw."""
    for key, feat in features.items():
        low = key.lower()
        if "extrinsic" not in low or not low.endswith(camera.lower()):
            continue
        names = _names(feat) or []
        shape = (feat or {}).get("shape") or []
        if names[:6] == ["x", "y", "z", "roll", "pitch", "yaw"] or (not names and list(shape) == [6]):
            return key
    return None


def _clock(features: dict, cameras: dict[str, str]) -> dict | None:
    """Recorded clocks: ``timestamp_robot*`` for the robot state, ``timestamp_cam_<camera>*`` per camera."""
    clocks = []
    for key in sorted(features):
        low = key.lower()
        if not low.startswith("timestamp_") or (features[key] or {}).get("dtype") not in ("int64", "int32", "float64", "float32"):
            continue
        unit = next((u for suffix, u in CLOCK_UNITS if low.endswith(suffix)), None)
        if unit is None:
            continue
        if "robot" in low or "state" in low:
            clocks.append({"channel": "robot_state", "key": key, "unit": unit, "clock_id": "recorded", "semantics": "robot state"})
            continue
        cam = next((src for short, src in cameras.items() if short.lower() in low), None)
        if cam:
            clocks.append({"channel": cam, "key": key, "unit": unit, "clock_id": "recorded", "semantics": "camera capture"})
    if not clocks:
        return None
    return {"source_clocks": clocks, "assurance": ASSUMED, "assumptions": [_a("clocks_from_column_names")]}


def _meta_intrinsics(listing: Iterable[str], read: Callable[[str], bytes] | None) -> dict[str, dict]:
    """Intrinsics a LeRobot dataset carries in ``meta/*calibration*.json``: per camera (short name or key), K or
    ``intrinsics_fx_cx_fy_cy``, the model and coefficients, the picture size; a file that cannot be read says nothing."""
    out: dict[str, dict] = {}
    if read is None:
        return out
    for key in sorted(k for k in listing if CALIBRATION_FILE.match(k)):
        try:
            doc = json.loads(read(key))
        except (OSError, ValueError, KeyError):
            continue
        for name, c in ((doc or {}).get("cameras") or {}).items() if isinstance(doc, dict) else ():
            if not isinstance(c, dict):
                continue
            intr: dict[str, Any] = {}
            if c.get("K"):
                intr["K"] = c["K"]
            elif c.get("intrinsics_fx_cx_fy_cy"):
                intr["fx_cx_fy_cy"] = list(map(float, c["intrinsics_fx_cx_fy_cy"]))
            else:
                continue
            model = c.get("model") or (c.get("distortion") or {}).get("model") or "pinhole"
            intr["model"] = {"brown": "opencv_brown", "fisheye": "opencv_fisheye"}.get(model, model)
            if intr["model"] not in ("pinhole", "opencv_brown", "opencv_fisheye"):
                continue
            coef = c.get("distortion_coefficients") or (c.get("distortion") or {}).get("coefficients") or []
            if coef:
                intr["coefficients"] = list(map(float, coef))
            size = c.get("image_size_wh") or ([c["width"], c["height"]] if c.get("width") and c.get("height") else None)
            if size:
                intr["image_size_wh"] = [int(size[0]), int(size[1])]
            intr.update(source="meta_file", assurance=DECLARED)
            out[str(name)] = {"intrinsics": intr, "file": key}
    return out


def draft_lerobot(info: dict, *, listing: Iterable[str] = (), read: Callable[[str], bytes] | None = None,
                  base: str | None = "builtin:lerobot") -> dict:
    """``{"declaration", "unresolved"}`` of a LeRobot (or Lance) dataset from its ``info.json``: no first layer
    (it is read off info.json), the semantics and the calibration the columns and ``meta/`` say, the rest assumed
    or left to a person (``unresolved``: ``[{field, code, args?}]``)."""
    from ..extensions.eef_consistency import record_draft as RD

    features = info.get("features") if isinstance(info.get("features"), dict) else {}
    robot = _robot(info.get("robot_type"))
    unresolved: list[dict] = []
    sem: dict[str, Any] = {"pose": None, "joints": None, "gripper": None}
    pose, notes, why = RD._pose(features)
    if pose is not None:
        pose = {k: v for k, v in pose.items() if k != "reference_frame"}
        frame = POSE_FRAMES.get(robot or "")
        notes = [n for n in notes if n.get("code") not in ("pose_frame_undeclared", "same_base_as_upload")]
        if frame:
            pose["frame_id"] = frame
            notes.append(_a("pose_frame_by_robot", robot=robot, frame=frame))
        else:
            pose["frame_id"] = None
            unresolved.append({"field": "semantics.pose.frame_id", "code": "pose_frame_unknown"})
        notes.append(_a("reference_frame_robot_base"))
        sem["pose"] = {**pose, "reference_frame": REFERENCE_FRAME, "pose_type": "absolute", "assurance": ASSUMED,
                       "assumptions": [{k: v for k, v in n.items() if k != "source"} for n in notes]}
    else:
        unresolved.append({"field": "semantics.pose", **({k: v for k, v in why.items() if k != "source"} if why else
                                                         {"code": "no_named_pose_column"})})
    joints, jnotes, jwhy = RD._joints(features, info.get("robot_type"))
    if joints is not None:
        sem["joints"] = {**{k: v for k, v in joints.items() if k != "reference_frame"}, "reference_frame": REFERENCE_FRAME,
                         "assurance": ASSUMED,
                         "assumptions": [{k: v for k, v in n.items() if k != "source"} for n in jnotes
                                         if n.get("code") != "same_base_as_upload"]}
    grip, _ = _gripper(features)
    sem["gripper"] = grip
    cams = _video_keys(info)
    shorts = {short_name(src): src for src in cams}
    meta = _meta_intrinsics(listing, read)
    handheld_ds = UMI_CALIBRATION in set(listing)
    wrists = 0
    mounts: dict[str, tuple[str | None, list[dict]]] = {}
    for src in cams:
        mounts[src] = mount_of(src) if not handheld_ds else (WRIST, [_a("mount_handheld_dataset")])
        wrists += mounts[src][0] == WRIST
    calib: dict[str, dict] = {}
    for src in cams:
        short = short_name(src)
        mount, mnotes = mounts[src]
        if mount is None:
            unresolved.append({"field": f"calibration.cameras.{src}.mount", "code": "mount_unknown"})
            continue
        entry: dict[str, Any] = {"camera_id": short, "mount": mount}
        notes = list(mnotes)
        if mount == WRIST and handheld_ds:              # export-umi: camera j is hand j (design doc 24 §1)
            owner = f"robot{sorted(cams).index(src)}"
            entry["owner"] = owner
            notes.append(_a("owner_by_order", owner=owner))
        elif mount == WRIST:
            owner, onotes = owner_of(src, wrists)
            entry["owner"] = owner
            notes += onotes
            if owner is None:
                unresolved.append({"field": f"calibration.cameras.{src}.owner", "code": "owner_unknown"})
        hit = meta.get(short) or meta.get(src)
        entry["intrinsics"] = hit["intrinsics"] if hit else None
        if hit:
            notes.append(_a("intrinsics_from_meta", file=hit["file"]))
        elif not handheld_ds:
            unresolved.append({"field": f"calibration.cameras.{src}.intrinsics", "code": "intrinsics_missing"})
        col = _extrinsic_column(features, short) if mount == FIXED_EXTERNAL else None
        if col:
            entry["extrinsics"] = {"mode": "column", "key": col, "layout": "xyz_rpy", "assurance": DECLARED}
            notes.append(_a("extrinsics_column_by_name", key=col))
        else:
            entry["extrinsics"] = None
            if mount == FIXED_EXTERNAL:
                unresolved.append({"field": f"calibration.cameras.{src}.extrinsics", "code": "extrinsics_missing"})
            elif mount == WRIST and not handheld_ds:
                unresolved.append({"field": f"calibration.cameras.{src}.extrinsics", "code": "camera_tcp_missing"})
        entry["media_transform"] = "identity"
        entry["assurance"] = ASSUMED
        entry["assumptions"] = notes
        calib[src] = entry
    tool = None                                         # a handheld gripper's tool is in its own calibration
    if handheld_ds:
        pass
    elif robot in TOOLS:
        tool = {**json.loads(json.dumps(TOOLS[robot])), "assurance": ASSUMED,
                "assumptions": [_a("tool_from_robot_type", robot_type=str(info.get("robot_type")).strip(), robot=robot)]}
    elif pose is not None and re.search(r"(?:^|[._])(tcp|ee|eef)(?:[._]|$)", pose["key"].lower()):
        tool = {"tcp_offset_m": [0.0, 0.0, 0.0], "finger_axis": None, "max_opening_m": None,
                "axes": {"from": "tcp", "length_m": 0.06}, "assurance": ASSUMED,
                "assumptions": [_a("pose_is_tcp", key=pose["key"])]}
    elif pose is not None:
        unresolved.append({"field": "calibration.tool", "code": "tool_unknown"})
    doc: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "base": base, "semantics": sem,
                           "calibration": {"cameras": calib, "tool": tool, "handheld": None},
                           "timing": _clock(features, shorts)}
    return {"declaration": doc, "unresolved": unresolved}


# ---------------------------------------------------------------- mcap (a probed recording)

def handheld_builtin() -> dict:
    """The built-in DAS DEMO calibration as a declaration's handheld block: every value assumed."""
    from ..extensions.eef_consistency.adapters import umi_mcap as X

    return {"calibration": json.loads(X.BUILTIN_CALIBRATION.read_text(encoding="utf-8")), "builtin": "das_gripper_demo",
            "assumptions": [_a("handheld_builtin_calibration", gripper="das_gripper")]}


def _camera_info_topic(topics: Iterable[str], camera: str) -> str | None:
    """The calibration topic beside a camera topic (same parent path)."""
    parent = camera.rsplit("/", 2)[0] if camera.count("/") >= 2 else ""
    for t in sorted(topics):
        low = t.lower()
        if ("camera_info" in low or "calibration" in low) and parent and t.startswith(parent + "/"):
            return t
    return None


def draft_mcap(mapping: dict | None, topics: dict[str, Any], *, robot_type: Any = None,
               calibrations: dict[str, dict] | None = None) -> dict:
    """``{"declaration", "unresolved"}`` of an mcap dataset: its first layer is the probe's draft mapping
    (``curation.viz.mcap_mapping.draft``), the cameras' mounts and owners by name, intrinsics from the calibration
    topic beside each camera (``calibrations``: topic -> what its first message says), a pose topic for the arm.
    ``topics``: topic -> schema name of the probed recording."""
    from . import normalize

    doc = normalize(mapping) or {"schema_version": SCHEMA_VERSION}
    unresolved: list[dict] = []
    cams = [c["topic"] for c in (mapping or {}).get("cameras") or []]
    umi = (mapping or {}).get("base") == "builtin:umi" or any(UMI_CAMERA.match(t) for t in cams)
    hands = sorted({m.group(1) for t in topics for m in [UMI_CAMERA.match(t) or re.match(r"^/(robot\d+)/vio/", t)] if m})
    calibrations = calibrations or {}
    wrists = sum(1 for t in cams if mount_of(t)[0] == WRIST)
    calib: dict[str, dict] = {}
    for src in cams:
        mount, notes = mount_of(src)
        if mount is None:
            unresolved.append({"field": f"calibration.cameras.{src}.mount", "code": "mount_unknown"})
            continue
        entry: dict[str, Any] = {"camera_id": short_name(src), "mount": mount}
        notes = list(notes)
        if mount == WRIST:
            owner, onotes = owner_of(src, wrists, arms=max(len(hands), 1) if umi else 1)
            entry["owner"] = owner
            notes += onotes
            if owner is None:
                unresolved.append({"field": f"calibration.cameras.{src}.owner", "code": "owner_unknown"})
        info_topic = _camera_info_topic(topics, src)
        got = calibrations.get(info_topic or "")
        if got and (got.get("K") or got.get("fx_cx_fy_cy")):
            entry["intrinsics"] = {**got, "source": "camera_info", "assurance": DECLARED}
            notes.append(_a("intrinsics_from_camera_info", topic=info_topic))
        else:
            entry["intrinsics"] = None
            if info_topic:
                notes.append(_a("intrinsics_from_camera_info", topic=info_topic))
            elif not umi:
                unresolved.append({"field": f"calibration.cameras.{src}.intrinsics", "code": "intrinsics_missing"})
        entry["extrinsics"] = None
        if mount == FIXED_EXTERNAL:
            unresolved.append({"field": f"calibration.cameras.{src}.extrinsics", "code": "extrinsics_missing"})
        entry.update(media_transform="identity", assurance=ASSUMED, assumptions=notes)
        calib[src] = entry
    sem: dict[str, Any] = {"pose": None, "joints": None, "gripper": None}
    if not umi:
        pose_topic = next((t for t, schema in sorted(topics.items())
                           if str(schema or "").split("/")[-1].split(".")[-1] in ("PoseStamped", "PoseInFrame", "Pose")
                           and not t.startswith("/tf")), None)
        if pose_topic:
            schema = str(topics[pose_topic] or "")
            field = "pose" if "PoseStamped" in schema or "PoseInFrame" in schema else None
            sem["pose"] = {"topic": pose_topic, **({"fields": [field]} if field else {}), "layout": "xyz_quat_xyzw",
                           "units": {"position": "m", "angle": "rad"}, "frame_id": None,
                           "reference_frame": REFERENCE_FRAME, "pose_type": "absolute", "assurance": ASSUMED,
                           "assumptions": [_a("pose_from_schema", topic=pose_topic, schema=schema),
                                           _a("quaternion_order", order="xyzw"),
                                           _a("units_by_convention", position="m", angle="rad")]}
            unresolved.append({"field": "semantics.pose.frame_id", "code": "pose_frame_unknown"})
        robot = _robot(robot_type)
        if robot in TOOLS:
            tool = {**json.loads(json.dumps(TOOLS[robot])), "assurance": ASSUMED,
                    "assumptions": [_a("tool_from_robot_type", robot_type=str(robot_type).strip(), robot=robot)]}
        else:
            tool = None
            if sem["pose"] is not None:
                unresolved.append({"field": "calibration.tool", "code": "tool_unknown"})
    else:
        tool = None
    doc["semantics"] = sem
    doc["calibration"] = {"cameras": calib, "tool": tool, "handheld": handheld_builtin() if umi else None}
    doc.setdefault("timing", None)
    return {"declaration": doc, "unresolved": unresolved}


def merge(base: dict | None, over: dict | None) -> dict | None:
    """``over`` laid on ``base`` (applying a template or keeping what a person confirmed over a fresh draft): a
    layer or a camera ``over`` has replaces ``base``'s; what it lacks stays."""
    from . import LAYER1, normalize

    if over is None:
        return normalize(base)
    out = normalize(base) or {"schema_version": SCHEMA_VERSION}
    o = normalize(over) or {}
    for k in LAYER1:
        if k in o:
            out[k] = o[k]
    sem = dict(out.get("semantics") or {})
    sem.update({k: v for k, v in (o.get("semantics") or {}).items()})
    if sem:
        out["semantics"] = sem
    cal = dict(out.get("calibration") or {})
    oc = o.get("calibration") or {}
    cams = dict(cal.get("cameras") or {})
    cams.update(oc.get("cameras") or {})
    cal.update({k: v for k, v in oc.items() if k != "cameras"})
    if cams or cal:
        cal["cameras"] = cams
        out["calibration"] = cal
    if "timing" in o:
        out["timing"] = o["timing"]
    out.pop("suspects", None)
    return out


def unresolved_of(doc: dict | None, *, cameras: Iterable[str] = ()) -> list[dict]:
    """What a declaration still leaves to a person (for a confirmed one: what the table asks about)."""
    out: list[dict] = []
    doc = doc or {}
    cal = (doc.get("calibration") or {})
    have = cal.get("cameras") or {}
    for src in cameras:
        c = have.get(src)
        if c is None:
            out.append({"field": f"calibration.cameras.{src}.mount", "code": "mount_unknown"})
            continue
        if c.get("mount") == WRIST and not c.get("owner"):
            out.append({"field": f"calibration.cameras.{src}.owner", "code": "owner_unknown"})
    return out


__all__ = ["draft_lerobot", "draft_mcap", "handheld_builtin", "merge", "mount_of", "owner_of", "unresolved_of"]
