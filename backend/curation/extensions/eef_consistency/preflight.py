"""The two EEF modules' entries of ``curation preflight`` (C2 preflight.schema, design 12 §5.1, §11.1).

The file is a module parameter (``trajectory_json``): a path on the command line, an upload handle in
the console (F5.5), which the Daemon turns into a path before it calls the CLI. Without it the module
``needs_input`` (``input_hint.field = trajectory_json``); the console never pre-selects an advisory module,
it is opted into and then asks for the upload; with a valid file it then needs a VLM backend (D49: the
module reviews with a model).
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


def module_entry(base: dict, *, vlm_backend: bool) -> dict:
    """The module's preflight entry (D49): the file first (``consistency_entry``), then a VLM backend -
    the module reviews with a model and cannot run without one."""
    if base.get("availability") != C.AVAILABLE or vlm_backend:
        return dict(base)
    return {**base, "availability": C.NEEDS_INPUT, "reason_code": C.VLM_BACKEND_MISSING,
            "reason": "no VLM backend chosen; pick one (add one first if there is none)",
            "input_hint": {"field": "vlm"}}


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


def consistency_entry(params: dict, *, episodes: Iterable[int], media_exists: Callable[[str], bool] | None,
                      lerobot_root: str | None = None) -> dict:
    """The preflight entry of ``eef_video_consistency`` for the task's episodes."""
    traj = (params.get("trajectory_json") or "").strip()
    if not traj:
        return {"availability": C.NEEDS_INPUT, "reason_code": C.TRAJECTORY_MISSING,
                "reason": "no trajectory.json given: upload one (console) or pass "
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
    for k in CAP.CORE_SUBITEMS + (C.INPUT_CONSISTENCY, C.RECORD):
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
    entry.update(subitems=subitems, episode_counts=counts, notes=notes)
    return entry
