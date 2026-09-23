"""The two EEF modules' entries of ``curation preflight`` (C2 preflight.schema, design 12 §5.1, §11.1).

The file is a module parameter (``trajectory_json``): a path on the command line, an upload handle in
the console (F5.5), which the Daemon turns into a path before it calls the CLI. Without it the module
``needs_input`` (``input_hint.field = trajectory_json``); the console never pre-selects an advisory module,
it is opted into and then asks for the upload. The VLM review is not provided yet and is unsupported.
"""
from __future__ import annotations

import os
import pathlib
from typing import Callable, Iterable

from . import capability as CAP
from . import contracts as C
from . import load
from .observations import seeded_points

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


def _unsupported(reason: str, code: str, args: dict | None = None) -> dict:
    out = {"availability": C.UNSUPPORTED, "reason": reason, "reason_code": code}
    if args:
        out["reason_args"] = args
    return out


def review_entry() -> dict:
    return _unsupported("the VLM review of EEF-video consistency is not part of the DEMO's first cut",
                        "eef_review_not_available")


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
    observable = {ep: (seeded_points(seeds, s) or {}) for ep, s in result.samples.items()} if seeds else None
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
    if seeds is None:
        notes.append("no observation seeds: position, orientation and time need the P-A seeds "
                     "(--param eef_video_consistency.observation_seeds=DIR)")
    if table["samples_outside_dataset"]:
        notes.append(f"{len(table['samples_outside_dataset'])} sample(s) of the file are not in this dataset")
    if table["availability"] == C.AVAILABLE:
        entry = {"availability": C.AVAILABLE}
    else:
        entry = _unsupported("no selected episode can be assessed from this trajectory.json",
                             table.get("reason_code") or C.PROJECTION_MISSING)
    entry.update(subitems=subitems, episode_counts=counts, notes=notes)
    return entry
