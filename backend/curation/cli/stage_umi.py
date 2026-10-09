"""``curation stage-umi`` - a raw UMI / TRUMI session made into a LeRobot dataset (design doc 24 §5).

A customer's handheld-gripper data often comes as the SLAM pipeline left it: ``dataset_plan.pkl`` and
``demos/`` (each demo's ``raw_video.mp4`` and ``camera_trajectory.csv``, the mapping's ``tx_slam_tag.json``,
the grippers' ``gripper_range.json``). The platform reads LeRobot, so this command writes one - with its
``trajectory.json`` - into a new local directory: the camera calibration is read from the session
(``adapters.umi.derive_calibration``), nobody writes one. A remote session is only read: the objects the
export needs are copied into a temporary directory first (nothing else of the session is fetched, and
nothing is written to the source). ``--json`` prints ``docs/contracts/cli/stage-umi.schema.json``.
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile

from . import inputs
from .errors import InputUnreachable, UsageError
from .framework import Context, Result
from .storage import is_remote

STAGE = "stage-umi"
PLAN = "dataset_plan.pkl"


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "stage-umi", parents=parents,
        help="make a raw UMI session a LeRobot dataset (calibration read from the session)",
        description="Read a raw UMI / TRUMI session (dataset_plan.pkl + demos/) and write it as a LeRobot "
                    "v2.1 dataset with its trajectory.json into --out. The source is only read.")
    inputs.add_arguments(p)
    p.add_argument("--out", required=True, metavar="DIR", help="a new local directory for the LeRobot dataset")
    p.add_argument("--instruction", default=None, help="the task text (a raw session has none)")
    p.add_argument("--max-side", type=int, default=960, help="the long side of the written videos")
    p.set_defaults(func=run)


def is_session(listing) -> bool:
    """A raw UMI / TRUMI session: the plan at the root and the demos beside it."""
    return PLAN in listing and any(k.startswith("demos/") for k in listing)


def needed_keys(listing, plan_cameras: list[str]) -> list[str]:
    """The session objects the export reads: the plan, each used demo's video, CSV and SLAM log, the mappings'
    ``tx_slam_tag.json``, the grippers' ``gripper_range.json`` and any intrinsics file."""
    demos = {p.split("/", 1)[0] for p in plan_cameras}
    out = {PLAN}
    for key in listing:
        parts = key.split("/")
        if len(parts) == 3 and parts[0] == "demos":
            d, name = parts[1], parts[2]
            if d in demos and name in ("raw_video.mp4", "camera_trajectory.csv", "slam_stdout.txt"):
                out.add(key)
            elif d.startswith("mapping_") and name == "tx_slam_tag.json":
                out.add(key)
            elif d.startswith("gripper_calibration_") and name == "gripper_range.json":
                out.add(key)
        if "intrinsics" in parts[-1] and parts[-1].endswith(".json") and len(parts) <= 2:
            out.add(key)
    return sorted(out)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..extensions.eef_consistency.adapters import umi

    out = os.path.abspath(args.out)
    if is_remote(args.out):
        raise UsageError("--out must be a local directory")
    if os.path.exists(out):
        raise UsageError(f"--out already exists: {out}")
    storage = inputs.open_input(ctx, args)
    ctx.progress(STAGE, 0, 3)
    listing = storage.list()
    if not listing:
        raise InputUnreachable(f"nothing found at {storage.uri}", {"uri": storage.uri})
    if not is_session(listing):
        raise UsageError(f"{storage.uri} is not a raw UMI session (no {PLAN} with demos/ beside it)")
    tmp = None
    copied = 0
    try:
        if storage.remote:
            tmp = tempfile.mkdtemp(prefix="stage-umi-")
            storage.download(PLAN, os.path.join(tmp, PLAN))
            plans = umi.read_plan(os.path.join(tmp, PLAN))
            keys = needed_keys(listing, [c["video_path"] for p in plans for c in p["cameras"]])
            for i, key in enumerate(keys):
                ctx.check_stop("while copying the session")
                dest = os.path.join(tmp, *key.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if not os.path.isfile(dest):
                    storage.download(key, dest)
                copied += int(listing[key].size)
                ctx.log("debug", f"copied {key} ({i + 1}/{len(keys)})")
            root = tmp
        else:
            root = storage.root
        ctx.progress(STAGE, 1, 3)
        cfg = umi.derive_calibration(root, dataset_id=os.path.basename(str(storage.uri).rstrip("/")),
                                     instruction=args.instruction)
        ctx.log("info", f"calibration from the session: {cfg['provenance']['source']}")
        try:
            result = umi.export(root, cfg, out, max_side=int(args.max_side))
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)          # no half-written dataset left behind
            raise
        ctx.progress(STAGE, 3, 3)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    payload = {"schema_version": "1.0", "input": str(storage.uri), "out": out, "episodes": int(result["episodes"]),
               "cameras": len(cfg["cameras"]),
               "calibration": {"source": cfg["provenance"]["source"].split(";")[0],
                               "intrinsics": {cid: c["source"]["intrinsics"] for cid, c in cfg["cameras"].items()}},
               "downloaded_bytes": copied}
    return Result(payload, f"{storage.uri} -> {out}: {payload['episodes']} episode(s), {payload['cameras']} camera(s)")
