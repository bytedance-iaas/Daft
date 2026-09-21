"""``curation report`` - the report of one result revision (design doc 02 §3.8).

Writes ``report.md``, ``report.json`` (``cli/report.schema.json``), ``perf.json``
and ``tables/*.parquet`` into ``revisions/r<NNNN>/`` - next to the four lists
``aggregate --phase final`` wrote there - and ``commit.json`` last: which parts
of each module's results and which human decisions this revision was built
from. Readers trust only a revision with ``commit.json``; which one is in force
is the Daemon's switch, after it is uploaded and verified (D25). A committed
revision is never written again.
"""
from __future__ import annotations

import argparse
import os

from . import runctx
from .errors import UsageError
from .framework import Context, Result


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "report", parents=parents,
        help="write the report of a result revision and commit it",
        description="Write report.md, report.json, perf.json and the detail tables of a "
                    "result revision, then commit.json last.")
    runctx.add_run_dir(p)
    p.add_argument("--revision", required=True, type=int, metavar="N")
    p.add_argument("--format", default="md,json", metavar="LIST",
                   help="md,json (default); json alone writes a stub report.md")
    p.add_argument("--modules", metavar="IDS",
                   help="the task's selected modules (default: from plan.json)")
    p.add_argument("--subtask-id", metavar="ID",
                   help="the subtask that produced this revision (kept in commit.json)")
    p.set_defaults(func=run)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import reporting
    from ..pipeline.records import committed_revisions

    run_dir = runctx.run_dir_of(args)
    formats = {f.strip() for f in str(args.format).split(",") if f.strip()}
    if not formats <= {"md", "json"} or "json" not in formats:
        raise UsageError("--format takes md and json (json is always written)")
    if args.revision < 1:
        raise UsageError("--revision starts at 1")
    if args.revision in committed_revisions(run_dir):
        raise UsageError(f"revision {args.revision} is committed already; a result revision "
                         f"is never rewritten")
    modules = runctx.selected_modules(args, run_dir)
    try:
        rev = reporting.Revision(run_dir, args.revision, modules)
    except FileNotFoundError as e:
        raise UsageError(str(e)) from None
    ctx.progress("report", 0, 1)
    files = reporting.write(rev, formats=tuple(formats), subtask_id=args.subtask_id)
    ctx.progress("report", 1, 1)

    def rel(p: str) -> str:
        return os.path.relpath(p, run_dir).replace(os.sep, "/")

    payload = {"schema_version": "1.0", "revision": args.revision,
               "files": {"report_md": rel(files["report_md"]),
                         "report_json": rel(files["report_json"]),
                         "perf_json": rel(files["perf_json"]),
                         "tables": [rel(t) for t in files["tables"]],
                         "commit": rel(files["commit"])}}
    human = (f"revision {args.revision}: {payload['files']['report_md']}, "
             f"{len(payload['files']['tables'])} table(s); committed "
             f"({payload['files']['commit']})")
    return Result(payload, human=human)
