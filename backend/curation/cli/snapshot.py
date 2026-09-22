"""``curation snapshot`` - fix the source objects a task will read (design doc 02 §3.3).

Lists (never reads) the input and records every object the task needs - all of
``meta/``, the data parquet and videos of the selected episodes, and the data
parquet of the dataset's first 100 episodes, from which v1 resolves the dataset
semantics whatever the selection - with its size and ETag (TOS) or
modification time (local). A selected LeRobot v2 episode whose parquet or any
camera's video is not in the listing is left out, as v1 does (D40): it goes to
``skipped_episodes`` with the keys it lacks, and no command reads it. The document is written to
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
    v2 = fmt.version == "v2"
    skipped, incomplete = [], []
    for ep in meta.episodes:
        if selected is not None and ep.index not in selected:
            continue
        wanted = list(ep.data_keys) + list(ep.video_keys.values())
        missing = [k for k in wanted if k not in listing]
        if missing and v2:                  # v1's _v2_missing: left out (D40)
            skipped.append({"episode_index": ep.index, "missing": sorted(missing)})
            continue
        if missing:
            incomplete.append(ep.index)
        keys.update(k for k in wanted if k in listing)
    for ep in lerobot_meta.semantics_sample(meta):
        if v2 and not lerobot_meta.complete(ep, listing):
            continue                        # v1's sample drops it as well
        keys.update(k for k in ep.data_keys if k in listing)
    if skipped:
        ctx.log("warn", f"{len(skipped)} selected episodes miss their parquet or a camera's "
                        f"video ({episode_sel.preview([s['episode_index'] for s in skipped])})"
                        f"; they are left out like v1 does: not checked, in no list, "
                        f"listed in the report")
    if incomplete:
        ctx.log("warn", f"{len(incomplete)} selected episodes miss data or video files "
                        f"({episode_sel.preview(incomplete)}); LeRobot v3 episodes are not "
                        f"left out (v1 reads them): their checks will fail to read them")
    doc = source_manifest.build(storage.uri, [listing[k] for k in keys], skipped=skipped)
    ctx.check_stop("before writing the manifest")
    try:
        source_manifest.write(args.out, doc)
    except OSError as e:
        raise UsageError(f"--out {args.out}: cannot write it: {e}") from None
    ctx.progress(STAGE, 2, 2)
    n_eps = (len(selected) if selected is not None else len(meta.episodes)) - len(skipped)
    summary = doc["summary"]
    human = (f"{summary['count']} objects, {summary['bytes']} bytes for {n_eps} episodes of "
             f"{storage.uri}\nwrote {args.out} ({summary['digest']})")
    if skipped:
        human += f"\n{len(skipped)} episode(s) left out for missing source files"
    return Result(doc, human=human)
