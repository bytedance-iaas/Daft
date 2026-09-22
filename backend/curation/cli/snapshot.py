"""``curation snapshot`` - fix the source objects a task will read (design doc 02 §3.3).

Lists (never reads) the input and records every object the task needs - all of
``meta/``, the data parquet and videos of the selected episodes, and the data
parquet of the dataset's first 100 episodes, from which v1 resolves the dataset
semantics whatever the selection - with its size and ETag (TOS) or
modification time (local). The document is written to
``--out`` atomically and printed with ``--json``
(``docs/contracts/cli/source-manifest.schema.json``). Later commands given it
as ``--source-manifest`` refuse to read anything that changed (exit 6, D27).
"""
from __future__ import annotations

import argparse

from . import episodes as episode_sel
from . import inputs, lerobot_meta, source_manifest
from .errors import InputUnreachable, UsageError
from .framework import Context, Result
from .lerobot_meta import MetaError
from .storage import is_remote

STAGE = "snapshot"


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "snapshot", parents=parents,
        help="freeze the list of source objects a task will read",
        description="List the source objects a task will read (meta files plus the parquet "
                    "and videos of the selected episodes) with their sizes and ETags, and "
                    "write them to --out. Nothing is downloaded.")
    inputs.add_arguments(p)
    p.add_argument("--episodes", metavar="EXPR",
                   help="episode indices: 34, 10-20, 3,10-12 or @file (default: all)")
    p.add_argument("--out", required=True, metavar="FILE",
                   help="where to write source_manifest.json (a local path)")
    p.set_defaults(func=run)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    if is_remote(args.out):
        raise UsageError("--out must be a local path (the run directory); the Daemon uploads it")
    requested = episode_sel.parse(args.episodes)
    storage = inputs.open_input(ctx, args)
    ctx.progress(STAGE, 0, 2)
    listing = storage.list()
    if not listing:
        raise InputUnreachable(f"nothing found at {storage.uri}", {"uri": storage.uri})
    ctx.log("info", f"listed {len(listing)} objects under {storage.uri}")
    ctx.check_stop("after listing the input")

    fmt = lerobot_meta.detect_format(listing)
    if fmt.kind != "lerobot":
        raise UsageError(f"{storage.uri} is not a LeRobot dataset ({fmt.kind}: {fmt.note}); "
                         f"run preflight first")
    try:
        info = lerobot_meta.load_info(storage)
        fmt.codebase_version = str(info.get("codebase_version") or "") or None
        fmt.version = lerobot_meta.version_of(fmt.codebase_version or "")
        if fmt.version is None:
            raise MetaError(f"codebase_version {fmt.codebase_version!r} is not LeRobot v2/v3")
        problems = lerobot_meta.check_layout(listing, fmt.codebase_version)
        if problems:
            raise MetaError(problems[0])
        meta = lerobot_meta.read_dataset(storage, listing, info, fmt)
    except MetaError as e:
        raise UsageError(f"{storage.uri}: {e}; run preflight for the full list") from None
    ctx.progress(STAGE, 1, 2)

    selected, warning = episode_sel.reconcile(requested, [ep.index for ep in meta.episodes])
    if warning:
        ctx.log("warn", warning)
    keys = set(lerobot_meta.meta_keys(listing))
    incomplete = []
    for ep in meta.episodes:
        if selected is not None and ep.index not in selected:
            continue
        wanted = list(ep.data_keys) + list(ep.video_keys.values())
        present = [k for k in wanted if k in listing]
        if len(present) < len(wanted):
            incomplete.append(ep.index)
        keys.update(present)
    for ep in lerobot_meta.semantics_sample(meta):
        keys.update(k for k in ep.data_keys if k in listing)
    if incomplete:
        ctx.log("warn", f"{len(incomplete)} selected episodes miss data or video files "
                        f"({episode_sel.preview(incomplete)}); they are skipped at run time")
    doc = source_manifest.build(storage.uri, [listing[k] for k in keys])
    ctx.check_stop("before writing the manifest")
    try:
        source_manifest.write(args.out, doc)
    except OSError as e:
        raise UsageError(f"--out {args.out}: cannot write it: {e}") from None
    ctx.progress(STAGE, 2, 2)
    n_eps = len(selected) if selected is not None else len(meta.episodes)
    summary = doc["summary"]
    human = (f"{summary['count']} objects, {summary['bytes']} bytes for {n_eps} episodes of "
             f"{storage.uri}\nwrote {args.out} ({summary['digest']})")
    return Result(doc, human=human)
