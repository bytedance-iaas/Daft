"""The two EEF modules' entries of ``curation preflight`` (C2 preflight.schema, design 12 §5.1, §11.1).

The file is a module parameter (``trajectory_json``): a path on the command line, an upload handle in
the console (F5.5), which the Daemon turns into a path before it calls the CLI. Without it the module
``needs_input`` (``input_hint.field = trajectory_json``); the console never pre-selects an advisory module,
it is opted into and then asks for the upload. The VLM review follows the module it reviews and
then needs a VLM backend.
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


def review_entry(base: dict, *, vlm_backend: bool) -> dict:
    """``eef_video_review`` re-examines what ``eef_video_consistency`` found (F5.6): unusable when
    that module is, asking for the same file when that is missing, then for a VLM backend."""
    if base.get("availability") == C.UNSUPPORTED:
        return _unsupported(f"the EEF-video consistency module it reviews is unavailable: {base.get('reason')}",
                            C.EEF_BASE_UNAVAILABLE, {"base_reason_code": base.get("reason_code")})
    if base.get("availability") == C.NEEDS_INPUT:
        return {k: v for k, v in base.items() if k in ("availability", "reason", "reason_code", "input_hint")}
    if not vlm_backend:
        return {"availability": C.NEEDS_INPUT, "reason_code": C.VLM_BACKEND_MISSING,
                "reason": "no VLM backend chosen; pick one (add one first if there is none)",
                "input_hint": {"field": "vlm"}}
    out = {"availability": C.AVAILABLE}
    if base.get("episode_counts"):
        out["episode_counts"] = base["episode_counts"]
    return out


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
    mounts = MOUNTS[params.get("camera_mounts") or "fixed_external_and_wrist"]
    table = CAP.dataset_capability(result, episodes, observable=observable, allowed_mounts=mounts)
    per = table["episodes"]
    subitems = {}
    for k in CAP.CORE_SUBITEMS + (C.INPUT_CONSISTENCY,):
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
        notes.append("no observation seeds and no gripper template: position, orientation and time need one of them "
                     "(--param eef_video_consistency.observation_seeds=DIR or .gripper_template=FILE)")
    elif template is not None:
        notes.append(f"gripper template {template.sha256[:12]}…: {len(template.entries)} entries ({template.method}); "
                     "a camera that also has seeds keeps the seeds")
    if table["samples_outside_dataset"]:
        notes.append(f"{len(table['samples_outside_dataset'])} sample(s) of the file are not in this dataset")
    if table["availability"] == C.AVAILABLE:
        entry = {"availability": C.AVAILABLE}
    else:
        entry = _unsupported("no selected episode can be assessed from this trajectory.json",
                             table.get("reason_code") or C.PROJECTION_MISSING)
    entry.update(subitems=subitems, episode_counts=counts, notes=notes)
    return entry
