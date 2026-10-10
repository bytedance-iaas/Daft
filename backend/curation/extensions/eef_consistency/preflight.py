"""The two EEF modules' entries of ``curation preflight`` (C2 preflight.schema, design 12 §5.1, §11.1).

The file is a module parameter (``trajectory_json``): a path on the command line, an upload handle in
the console (F5.5), which the Daemon turns into a path before it calls the CLI. The file is optional only where
the platform can compute the trajectory: ``cli/preflight`` generates it from the dataset and passes that (design
doc 24); a handheld gripper's raw mcap carries its own (``handheld``, design doc 22 §5.4, D80: derived when the
module runs, with ``gripper_calibration`` or the built-in DAS DEMO calibration); on any other dataset the module
``needs_input`` (``input_hint.field = trajectory_json``) and the console asks for the upload. The console never
pre-selects an advisory module; with a valid file it then needs a VLM backend (D49: the module reviews with a
model).
"""
from __future__ import annotations

import os
import pathlib
from typing import Callable, Iterable

from . import capability as CAP
from . import contracts as C
from . import load
from . import template as TP
from .observations import seeded_points

MODULE_ID = C.MODULE_ID
MOUNTS = {"fixed_external_and_wrist": ("fixed_external", "wrist"), "fixed_external": ("fixed_external",)}


def seed_dir(params: dict) -> str | None:
    """``observation_seeds``, else ``observations_seed/`` next to the trajectory file if it exists."""
    given = (params.get("observation_seeds") or "").strip()
    if given:
        return os.path.expanduser(given)
    traj = (params.get("trajectory_json") or "").strip()
    if not traj:
        return None
    cand = pathlib.Path(os.path.expanduser(traj)).parent / "observations_seed"
    return str(cand) if cand.is_dir() else None


def template_path(params: dict) -> str | None:
    """``gripper_template`` (a file), else ``gripper_template.json`` next to the trajectory file if it exists."""
    given = (params.get("gripper_template") or "").strip()
    if given:
        return os.path.expanduser(given)
    traj = (params.get("trajectory_json") or "").strip()
    if not traj:
        return None
    cand = pathlib.Path(os.path.expanduser(traj)).parent / "gripper_template.json"
    return str(cand) if cand.is_file() else None


def _unsupported(reason: str, code: str, args: dict | None = None) -> dict:
    out = {"availability": C.UNSUPPORTED, "reason": reason, "reason_code": code}
    if args:
        out["reason_args"] = args
    return out


#: the reminder of an available module without a model backend (design doc 25 D84): a note, never needs_input
NOTE_VLM_BACKEND_MISSING = ("vlm_backend_missing: no VLM backend chosen - the module runs on the CPU's measurement alone, "
                            "the model's channel missing (single_source: no_vlm_backend); pick one to have its opinion")


#: with the model off and no gripper reference nothing measures a fixed third-person camera (design doc 25 §4.3)
NOTE_THIRD_PERSON_UNMEASURED = ("use_vlm is off and there is no gripper reference: the fixed third-person cameras are "
                                "not assessed (cannot tell); give a gripper reference or turn the model on")


def module_entry(base: dict, *, vlm_backend: bool, use_vlm: bool = True) -> dict:
    """The module's preflight entry: what ``consistency_entry`` (or the source's entry) says. Since registry 5.2 (design
    doc 25 D84) the module no longer needs a model: without a backend it is still available, with a reminder - the
    model's channel is then missing; with 「使用 VLM 辅助」 off nothing is said."""
    if base.get("availability") != C.AVAILABLE or vlm_backend or not use_vlm:
        return dict(base)
    return {**base, "notes": [*base.get("notes", []), NOTE_VLM_BACKEND_MISSING]}


def bundle_cameras(result) -> list[dict]:
    """The cameras of a trajectory bundle as the C2 preflight lists them: mount, hand, whether a projection can be
    computed or was given (``drawable``)."""
    out: dict[str, dict] = {}
    for s in result.samples.values():
        hands = ((s.sample or {}).get("umi") or {}).get("camera_hands") or {}
        for cid, cam in s.cameras.items():
            media = cam.media or {}
            src = media.get("topic") or next((p for p in str(media.get("uri") or "").split("/")
                                              if p.startswith("observation.images.")), None)
            c = out.setdefault(cid, {"source": src, "camera_id": cid, "mount": cam.mount, "owner": hands.get(cid),
                                     "drawable": False, "reason": None})
            if cam.mount == "moving":
                c["reason"] = "moving_camera_unsupported"
                continue
            if cam.provided or cam.calibration(s) is not None:
                c["drawable"], c["reason"] = True, None
            elif not c["drawable"]:
                c["reason"] = C.PROJECTION_MISSING
    return list(out.values())


def applicable_params(cameras: list[dict] | None) -> list[str]:
    """The module's parameters that apply to the dataset (design doc 25 §4.2): retired (``deprecated``) ones never,
    those with ``x-applies-when: third_person_camera`` (the gripper reference) only where a camera is a fixed
    third-person one - or where the cameras are not known (``cameras`` empty)."""
    from ...contracts import modules as registry

    props = registry.get(MODULE_ID).param_schema.get("properties") or {}
    third = not cameras or any(c.get("mount") == "fixed_external" for c in cameras)
    return [k for k, p in props.items() if not p.get("deprecated")
            and (p.get("x-applies-when") != "third_person_camera" or third)]


def _record_check(params: dict, root: str | None):
    """(the capability's ``record`` - the mapping, ``{"missing": [...]}``, ``{"invalid": text, "code": ...}``,
    None without one - and a note). A local dataset's columns (LeRobot ``meta/info.json``) or topics
    (the first mcap file's summary) are checked; a remote one's are checked when the module runs."""
    from . import record as RC

    path = (params.get("record_mapping") or "").strip()
    if not path:
        return None, None
    try:
        mapping = RC.load_mapping(os.path.expanduser(path))
    except (RC.RecordMappingError, OSError, ValueError) as exc:           # only the record item goes
        code = C.ROBOT_MODEL_UNKNOWN if isinstance(exc, RC.RobotModelUnknown) else C.RECORD_MAPPING_INVALID
        return {"invalid": str(exc), "code": code}, f"record mapping unusable: {exc}"
    kinds = ", ".join(sorted(mapping.sources))
    note = f"record mapping {mapping.sha256[:12]}…: {kinds}"
    if not root or not os.path.isdir(root):
        return mapping, note + " (columns / topics checked when the module runs)"
    missing: list[str] = []
    mcaps = sorted(n for n in os.listdir(root) if n.endswith(".mcap"))
    if mcaps:
        from mcap.reader import make_reader

        with open(os.path.join(root, mcaps[0]), "rb") as fh:
            summary = make_reader(fh).get_summary()
        topics = {ch.topic for ch in (summary.channels.values() if summary else [])}
        missing = [s.topic for s in mapping.sources.values() if s.topic and s.topic not in topics]
        missing += [s.key for s in mapping.sources.values() if s.key]          # columns name no mcap topic
    else:
        columns = RC.LeRobotRecords(root).columns()
        if columns is not None:
            missing = [c for s in mapping.sources.values() for c in (s.key, s.quaternion_key) if c and c not in columns]
            missing += [s.topic for s in mapping.sources.values() if s.topic]  # topics name no LeRobot column
    return ({"missing": missing}, note + f"; not in the dataset: {', '.join(missing)}") if missing else (mapping, note)


def derived_entry(params: dict, *, episodes: Iterable[int], lerobot_root: str | None = None,
                  calibration: dict | None = None) -> dict:
    """No trajectory.json on a handheld gripper's mcap (design doc 22 §5.4): the trajectory comes from each recording
    when the module runs. Available unless the calibration is unusable or a gripper reference is given (a handheld
    gripper only gets the model's opinion); what each sub-item can be is what a wrist camera on its own hand gives.
    The calibration: an old task's ``gripper_calibration`` file, else the dataset declaration's (``calibration``,
    design doc 25 §3.2), else the built-in DAS DEMO one."""
    from .adapters import umi_mcap as X

    if seed_dir(params) or template_path(params):
        return _unsupported("UMI action overlays use advisory video opinion; omit gripper seeds/templates",
                            C.TRAJECTORY_INVALID)
    cal = os.path.expanduser((params.get("gripper_calibration") or "").strip())
    try:
        cfg = X.read_calibration(cal) if cal else X.check_calibration(calibration, "the declaration's handheld calibration") \
            if calibration is not None else X.read_calibration(X.BUILTIN_CALIBRATION)
    except (X.ExportError, OSError) as exc:
        return _unsupported(f"the gripper calibration is unusable: {exc}", C.CALIBRATION_INVALID, {"path": cal or None})
    episodes = sorted(set(int(e) for e in episodes))
    own = {C.POSITION: C.OWN_HAND_CAMERA, C.ORIENTATION: C.OWN_HAND_CAMERA, C.TEMPORAL: C.WRIST_CAMERA_SPATIAL_ONLY,
           C.CAMERA_MOTION: C.WRIST_CAMERA_SPATIAL_ONLY, C.STATE_MOTION: C.POSE_SEMANTICS_UNKNOWN,
           C.INPUT_CONSISTENCY: C.PROJECTION_MISSING}
    subitems = {k: {"availability": C.UNSUPPORTED, "reason_code": r} for k, r in own.items()}
    subitems[C.EGO_MOTION] = {"availability": C.AVAILABLE, "reason_code": None}
    record, record_note = _record_check(params, lerobot_root)
    subitems[C.RECORD] = {"availability": C.AVAILABLE, "reason_code": None} if record is not None and not isinstance(record, dict) \
        else {"availability": C.UNSUPPORTED,
              "reason_code": C.RECORD_MAPPING_MISSING if record is None else record.get("code") or C.RECORD_COLUMNS_MISSING}
    which = (f"the uploaded calibration ({cfg['gripper']})" if cal
             else f"the dataset declaration's calibration ({cfg['gripper']}; {', '.join(X.assumed(cfg)) or 'nothing'} "
                  f"assumed)" if calibration is not None
             else f"the built-in DAS DEMO calibration ({', '.join(X.assumed(cfg))} assumed; no intrinsics fallback: "
                  f"a recording without camera_info needs an uploaded calibration with one)")
    notes = [f"no trajectory.json: the platform derives each episode's trajectory from the handheld gripper's recording "
             f"(design doc 22 §5.4) with {which}",
             "no observation seeds and no gripper template: the model gives an advisory opinion on each wrist camera's "
             "whole clip and the camera's own motion is compared with the poses; no episode is passed, rejected or "
             "asked on its account"]
    if record_note:
        notes.append(record_note)
    return {"availability": C.AVAILABLE, "subitems": subitems, "episode_counts": {C.AVAILABLE: len(episodes)},
            "notes": notes}


def declared_entry(params: dict, ready: dict, *, episodes: Iterable[int], joints: bool) -> dict:
    """A robot arm's trajectory generated from the dataset's declaration (design doc 25 §4.1): nothing to read here -
    what each sub-item can be follows from the cameras the declaration lets the platform draw (``ready``:
    ``declared.readiness``); the generation itself, episode by episode, is the module's."""
    seeds, tpath = seed_dir(params), template_path(params)
    mounts = set(MOUNTS[params.get("camera_mounts") or "fixed_external_and_wrist"])
    drawable = [c for c in ready["cameras"] if c["drawable"] and c["mount"] in mounts]
    external = any(c["mount"] == "fixed_external" for c in drawable)
    wrist = any(c["mount"] == "wrist" for c in drawable)
    on = {"availability": C.AVAILABLE, "reason_code": None}
    subitems = {}
    for k in (C.POSITION, C.ORIENTATION, C.TEMPORAL, C.CAMERA_MOTION):
        subitems[k] = on if external else {"availability": C.UNSUPPORTED,
                                           "reason_code": C.WRIST_CAMERA_SPATIAL_ONLY if wrist else C.PROJECTION_MISSING}
    subitems[C.STATE_MOTION] = on
    subitems[C.EGO_MOTION] = on if wrist else {"availability": C.UNSUPPORTED, "reason_code": C.PROJECTION_MISSING}
    subitems[C.INPUT_CONSISTENCY] = {"availability": C.UNSUPPORTED, "reason_code": C.PROJECTION_MISSING}
    subitems[C.RECORD] = on if joints else {"availability": C.UNSUPPORTED, "reason_code": C.RECORD_MAPPING_MISSING}
    episodes = sorted(set(int(e) for e in episodes))
    names = ", ".join(f"{c['camera_id']} ({c['mount']})" for c in drawable) or "none"
    notes = [f"no trajectory.json: the platform generates each episode's trajectory from the dataset's pose record and "
             f"its declaration (design doc 25 §4.1); cameras drawn: {names}"]
    skipped = [c for c in ready["cameras"] if not c["drawable"]]
    if skipped:
        notes.append("cameras left out: " + ", ".join(f"{c['camera_id']} ({c['reason']})" for c in skipped))
    if seeds is None and tpath is None:
        notes.append("no observation seeds and no gripper template: the CPU measures nothing; the model gives an "
                     "advisory opinion on each camera's whole clip (vlm_opinion, design doc 12 §10.5)")
    if joints:
        notes.append("the pose record and the joints' kinematics are compared with each other (record_consistency, "
                     "source internal; design doc 25 §6.1)")
    if not drawable:
        return {**_unsupported("no camera the task takes part with can be drawn from the declaration",
                               C.PROJECTION_MISSING), "subitems": subitems, "notes": notes}
    return {"availability": C.AVAILABLE, "subitems": subitems, "episode_counts": {C.AVAILABLE: len(episodes)},
            "notes": notes}


def consistency_entry(params: dict, *, episodes: Iterable[int], media_exists: Callable[[str], bool] | None,
                      lerobot_root: str | None = None, handheld: bool = False) -> dict:
    """The preflight entry of ``eef_video_consistency`` for the task's episodes. ``handheld``: the dataset is a
    handheld gripper's mcap (the built-in UMI layout), which carries its own trajectory."""
    traj = (params.get("trajectory_json") or "").strip()
    if not traj and handheld:
        return derived_entry(params, episodes=episodes, lerobot_root=lerobot_root)
    if not traj:
        return {"availability": C.NEEDS_INPUT, "reason_code": C.TRAJECTORY_MISSING,
                "reason": "the platform cannot compute the trajectory from this dataset (it records no end-effector "
                          "poses with the cameras' calibration): upload a trajectory.json (console) or pass "
                          "--param eef_video_consistency.trajectory_json=PATH",
                "input_hint": {"field": "trajectory_json"}}
    path = pathlib.Path(os.path.expanduser(traj))
    if not path.is_file():
        return _unsupported(f"trajectory.json not found: {path}", C.TRAJECTORY_MISSING, {"path": str(path)})
    episodes = sorted(set(int(e) for e in episodes))
    result = load.load_bundle(path, lerobot_root=lerobot_root, media_exists=media_exists)
    if not result.ok:
        first = result.errors[0]
        return _unsupported(f"trajectory.json is invalid: {first.message}", C.TRAJECTORY_INVALID,
                            {"errors": [i.as_dict() for i in result.errors[:10]], "sha256": result.sha256})
    seeds = seed_dir(params)
    tpath = template_path(params)
    if any(s.hand_poses for s in result.samples.values()) and (seeds or tpath):
        return _unsupported("UMI action overlays use advisory video opinion; omit gripper seeds/templates",
                            C.TRAJECTORY_INVALID)
    template = None
    if tpath:
        try:
            template = TP.load_template(tpath)
        except (TP.TemplateError, OSError) as exc:
            return _unsupported(f"gripper template is invalid: {exc}", C.TEMPLATE_INVALID, {"path": tpath})
    observable = None
    if seeds or template is not None:
        observable = {}
        for ep, s in result.samples.items():
            obs = dict(seeded_points(seeds, s) or {}) if seeds else {}
            if template is not None:                 # a camera with seeds keeps them
                obs = {**template.observable_points(s.cameras), **obs}
            observable[ep] = obs
    else:
        # no gripper reference (D-E15): the model looks at every declared point, so an episode
        # with a projection can be given an opinion
        observable = {ep: {cid: set(load.declared_point_ids(s, cid)) for cid in s.cameras}
                      for ep, s in result.samples.items()}
    mounts = MOUNTS[params.get("camera_mounts") or "fixed_external_and_wrist"]
    record, record_note = _record_check(params, lerobot_root)
    table = CAP.dataset_capability(result, episodes, observable=observable, allowed_mounts=mounts, record=record)
    per = table["episodes"]
    subitems = {}
    for k in CAP.AVAILABLE_BY + (C.INPUT_CONSISTENCY, C.RECORD):
        cells = [v["subitems"][k] for v in per.values() if "subitems" in v]
        if C.AVAILABLE in cells:
            subitems[k] = {"availability": C.AVAILABLE, "reason_code": None}
            continue
        reasons = [v["subitem_reasons"][k] for v in per.values() if v.get("subitem_reasons", {}).get(k)]
        state = C.NEEDS_INPUT if C.NEEDS_INPUT in cells else C.UNSUPPORTED
        subitems[k] = {"availability": state, "reason_code": reasons[0] if reasons else C.PROJECTION_MISSING}
    counts = table["counts"]
    in_file = [e for e in episodes if e in result.samples]
    notes = [f"trajectory.json sha256 {result.sha256[:12]}…: {len(in_file)} of {len(episodes)} episode(s) "
             f"declared; the others are unsupported (projection_missing)"]
    if result.warnings:
        notes.append(f"{len(result.warnings)} warning(s), first: {result.warnings[0].message}")
    if seeds is None and template is None:
        notes.append("no observation seeds and no gripper template: the CPU measures nothing; the model gives an "
                     "advisory opinion on each camera's whole clip (vlm_opinion, design doc 12 §10.5) and no episode "
                     "is passed, rejected or asked on its account")
    elif template is not None:
        notes.append(f"gripper template {template.sha256[:12]}…: {len(template.entries)} entries ({template.method}); "
                     "a camera that also has seeds keeps the seeds")
    if record_note:
        notes.append(record_note)
    if table["samples_outside_dataset"]:
        notes.append(f"{len(table['samples_outside_dataset'])} sample(s) of the file are not in this dataset")
    if table["availability"] != C.AVAILABLE:
        entry = _unsupported("no selected episode can be assessed from this trajectory.json",
                             table.get("reason_code") or C.PROJECTION_MISSING)
    else:
        entry = {"availability": C.AVAILABLE}
    entry.update(subitems=subitems, episode_counts=counts, notes=notes, _cameras=bundle_cameras(result))
    return entry
