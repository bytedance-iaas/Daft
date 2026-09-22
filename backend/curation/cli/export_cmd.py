"""``curation export`` - the delivered dataset of a result revision (design doc 02 §3.7).

Exports ``passed.json`` of the revision (``--revision``, default: the newest
committed one) into ``<run-dir>/export/lerobot_curated/``: the episodes waiting
for a human are delivered (v1's conservative pass), the ones waiting for a
retry (``held``) are not. Task texts are written by source - original
annotation, autolabel caption, human relabel - each with ``instruction_source``.
The work is W7's library (``curation.export.incremental.export_run``);
``--incremental`` builds on the previous export and falls back to a full one
when it cannot be trusted (``full_reason``).

``--output`` (the task's ``<run_id>/`` directory of the delivery, ``tos://`` or
a local path) receives the change: ``_COMPLETE`` is removed first, new and
changed files are uploaded, files no longer delivered are deleted, then
``export/manifest.detail.json`` and ``export/manifest.json`` last. ``curation
verify`` reads it back and writes ``_COMPLETE`` again (06 §4.4).
"""
from __future__ import annotations

import argparse
import json
import os
import time

from . import runctx
from .errors import ModuleFailed, SourceChanged, UsageError
from .framework import Context, Result

EXPORT_DIR = "export"
DATASET_DIR = f"{EXPORT_DIR}/lerobot_curated"


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "export", parents=parents,
        help="export the delivered LeRobot dataset of a result revision",
        description="Export the passed episodes of a result revision as a LeRobot dataset, "
                    "incrementally when asked, and sync the change to the delivery.")
    runctx.add_run_dir(p)
    p.add_argument("--input", required=True, metavar="URI", help="the source dataset")
    p.add_argument("--source", choices=("tos", "public", "local"), default=None)
    p.add_argument("--source-manifest", metavar="FILE",
                   help="default: <run-dir>/source_manifest.json when present")
    p.add_argument("--output", metavar="URI",
                   help="the task's directory in the delivery (tos:// or local); "
                        "without it the export stays in the run directory")
    p.add_argument("--revision", type=int, metavar="N",
                   help="the result revision (default: the newest committed one)")
    p.add_argument("--incremental", action="store_true",
                   help="build on the previous export; only what changed is written")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="files written at a time (default 1)")
    p.add_argument("--scratch", metavar="DIR",
                   help="local scratch for encoders (default $CURATION_EXPORT_SCRATCH / temp)")
    p.set_defaults(func=run)


def camera_health(run_dir: str, cfg: dict) -> dict | None:
    """v1's ``meta/curation_camera_health.json`` payload from the sync results."""
    from ..core.checks.video_action_sync import sync_health
    from ..pipeline.records import latest_results

    details = {}
    for ep, rec in latest_results(run_dir, "video_action_sync").items():
        d = rec.get("details") or {}
        if rec["verdict"] != "error" and d.get("per_camera") is not None:
            details[f"ep{int(ep):06d}"] = d
    if not details:
        return None
    lag = float(cfg["checks"].get("video_action_sync", {}).get("params", {})
                .get("lag_tol_s", 0.25))
    return {"dataset": sync_health(details, lag_tol_s=lag), "episodes": details}


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..export.incremental import (ExportError, ExportInputError, SourceChangedError,
                                      export_run)

    run_dir = runctx.run_dir_of(args)
    if args.concurrency < 1:
        raise UsageError("--concurrency must be at least 1")
    storage = runctx.open_input(ctx, args)
    source = storage.root if not storage.remote else storage.uri
    output = None
    if args.output:
        from .storage import open_storage

        output = open_storage(args.output, role="output", region=ctx.output_region)
        if not output.remote:
            os.makedirs(output.root, exist_ok=True)   # a local delivery directory
        output.delete("_COMPLETE")                    # 06 §4.4: incomplete until verified
    last = {"t": 0.0}

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if done >= total or now - last["t"] >= 1.0:
            last["t"] = now
            ctx.progress("export", done, total)

    try:
        outcome = export_run(run_dir, source, revision=args.revision,
                             incremental=bool(args.incremental),
                             source_manifest=args.source_manifest,
                             camera_health=camera_health(run_dir, ctx.config()),
                             scratch_dir=args.scratch, concurrency=args.concurrency,
                             log=lambda level, msg: ctx.log(level if level in
                                                            ("debug", "info", "warn", "error")
                                                            else "info", msg),
                             progress=progress)
    except SourceChangedError as e:
        raise SourceChanged(str(e)) from None
    except ExportInputError as e:
        raise UsageError(str(e)) from None
    except ExportError as e:
        raise ModuleFailed(f"export: {e}") from None
    result = dict(outcome.result)
    if output is not None:
        synced = sync(ctx, run_dir, outcome, output)
        ctx.log("info", f"delivery {output.uri}: {synced['uploaded']} file(s) uploaded, "
                        f"{synced['deleted']} deleted")
    return Result(result, human=render(result, outcome))


def sync(ctx: Context, run_dir: str, outcome, output) -> dict:
    """Mirror the export directory's change to the delivery; manifest.json last."""
    local = os.path.join(run_dir, EXPORT_DIR)
    with open(os.path.join(local, "manifest.detail.json"), encoding="utf-8") as fh:
        wanted = json.load(fh).get("files") or {}
    remote = {k[len(DATASET_DIR) + 1:]: info for k, info in output.list().items()
              if k.startswith(DATASET_DIR + "/")}
    changed = set(outcome.written) | {dst for _src, dst in outcome.renamed}
    uploads = sorted(rel for rel, rec in wanted.items()
                     if rel in changed or rel not in remote
                     or int(remote[rel].size) != int(rec["size"]))
    stale = sorted(rel for rel in remote if rel not in wanted)
    for n, rel in enumerate(uploads, 1):
        ctx.check_stop("while uploading the export")
        output.put_file(f"{DATASET_DIR}/{rel}", os.path.join(local, "lerobot_curated",
                                                             *rel.split("/")))
        ctx.progress("export:upload", n, len(uploads))
    for rel in stale:
        output.delete(f"{DATASET_DIR}/{rel}")
    output.put_file(f"{EXPORT_DIR}/manifest.detail.json",
                    os.path.join(local, "manifest.detail.json"))
    output.put_file(f"{EXPORT_DIR}/manifest.json", os.path.join(local, "manifest.json"))
    return {"uploaded": len(uploads), "deleted": len(stale)}


def render(result: dict, outcome) -> str:
    d = result["diff"]
    how = "incremental" if result["incremental"] else "full"
    text = (f"{how} export of {result['episodes']} episode(s) ({result['format']}): keep "
            f"{d['keep']}, relabel {d['relabel']}, renumber {d['renumber']}, add {d['add']}, "
            f"drop {d['drop']}; videos copied {result['videos_copied']}, re-encoded "
            f"{result['videos_reencoded']}, renamed {result.get('videos_renamed', 0)}")
    if result.get("full_reason"):
        text += f"\n  full export because: {result['full_reason']}"
    return text
