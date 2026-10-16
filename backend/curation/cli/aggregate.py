"""``curation aggregate`` - verdicts from the module results (design doc 02 §3.6, 17 §4).

Pure computation in seconds, recomputed in full every time. The modules' findings are graded by the
task's policy (``run.json``'s ``policy``, frozen at start; the default policy when there is none), the
applied human decisions are added, and ``--revision N`` writes ``passed`` / ``reject`` /
``held`` (disjoint and complete), the ``review`` view (C2 2.0), the policy it used (``policy.json``), the
machine's verdict lines (``verdicts.jsonl``) and the kept set (``keep.txt``) into ``revisions/r<NNNN>/``.
A revision that already has ``commit.json`` is never written again.

The selected modules come from ``--modules`` (else ``<run-dir>/plan.json``); the episodes from
``--episodes`` (else every episode any module has a result for). With ``--input`` the delivered task text
of annotated episodes is read from the dataset's metadata; without it only human relabels are listed.
"""
from __future__ import annotations

import argparse
import os

from . import runctx
from .errors import ModuleFailed, UsageError
from .framework import Context, Result

SCHEMA_VERSION = "1.0"


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "aggregate", parents=parents,
        help="compute the verdicts: the four lists of a result revision",
        description="Recompute the verdicts of a run directory from its module results.")
    runctx.add_run_dir(p)
    p.add_argument("--revision", type=int, metavar="N", required=True,
                   help="the result revision to write")
    p.add_argument("--modules", metavar="IDS",
                   help="the task's selected modules (default: from plan.json)")
    p.add_argument("--episodes", metavar="EXPR",
                   help="the task's episodes (default: every episode with a result)")
    p.add_argument("--input", metavar="URI",
                   help="the dataset, to list the delivered task text of annotated episodes")
    p.add_argument("--source", choices=("tos", "public", "local"), default=None)
    p.add_argument("--embodiment-id", metavar="ID")
    p.add_argument("--declaration", metavar="FILE",
                   help="accepted like the other reading commands; the verdicts do not depend on it")
    p.add_argument("--max-episodes", type=int, metavar="N")
    p.add_argument("--selection", metavar="EXPR",
                   help="accepted like the other reading commands (mcap / lance); the task "
                        "texts aggregate reads do not depend on it")
    p.set_defaults(func=run)


def _episodes(args, run_dir: str, modules: list[str]) -> list[int]:
    from ..pipeline.records import latest_results

    given = runctx.read_episode_file(args.episodes)
    if given is not None:
        return given
    found: set[int] = set()
    for m in modules:
        found |= set(latest_results(run_dir, m))
    if not found:
        raise UsageError("no episode has a result yet; pass --episodes")
    return sorted(found)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import aggregate as agg
    from ..pipeline.records import committed_revisions, revision_dir

    run_dir = runctx.run_dir_of(args)
    if args.revision < 1:
        raise UsageError("--revision starts at 1")
    if args.revision in committed_revisions(run_dir):
        raise UsageError(f"revision {args.revision} is committed (commit.json); a result "
                         f"revision is never rewritten - use a new one")
    modules = runctx.selected_modules(args, run_dir)
    episodes = _episodes(args, run_dir, modules)
    # left out for missing source files (D40): in no list, not in total
    from ..pipeline.skipped import all_skipped

    skipped = all_skipped(run_dir)
    left_out = [e for e in episodes if e in skipped]
    episodes = [e for e in episodes if e not in skipped]
    from ..pipeline.adjudication import Decisions
    from ..pipeline.records import write_json_atomic

    state = agg.RunState(run_dir, modules, episodes)
    out_dir = revision_dir(run_dir, args.revision)
    ctx.progress("aggregate:final", 0, 1)
    decisions = Decisions.of(run_dir)
    result = agg.final(state, args.revision, decisions, _task_text(ctx, args, run_dir, episodes))
    files = agg.write_final(out_dir, result)
    write_json_atomic(os.path.join(out_dir, "adjudications.json"), {"applied": decisions.ids()})
    counts = {"total": len(episodes), "passed": result["passed"]["count"],
              "reject": result["reject"]["count"], "held": result["held"]["count"],
              "review": result["review"]["count"]}
    ctx.log("info", f"policy {state.policy.preset}: {counts['passed']} kept, {counts['reject']} rejected, "
                    f"{counts['held']} held")
    counts["skipped"] = len(left_out)
    ctx.progress("aggregate:final", 1, 1)
    rel = {k: os.path.relpath(v, run_dir).replace(os.sep, "/") for k, v in files.items()}
    payload = {"schema_version": SCHEMA_VERSION, "phase": "final",
               "revision": args.revision, "counts": counts, "files": rel}
    return Result(payload, human=render(payload))


def _task_text(ctx, args, run_dir: str, episodes: list[int]):
    from ..pipeline.rows import index_of
    from ..pipeline.tasktext import TaskText

    instructions: dict[int, str] = {}
    if args.input:
        src = runctx.open_source(ctx, args)
        try:
            rows = runctx.meta_rows(src, episodes, args, what="aggregate")
        except ModuleFailed:
            # one episode the reader cannot open (a corrupt mcap, design doc 30 §3, D100) must not take the task with
            # it: read them one by one; an episode that cannot be read is delivered without task text
            rows, unread = [], []
            for ep in episodes:
                try:
                    rows += runctx.meta_rows(src, [ep], args, what="aggregate")
                except ModuleFailed:
                    unread.append(ep)
            if unread:
                ctx.log("warning", f"task text: {len(unread)} of {len(episodes)} episodes cannot be read and are "
                                   f"delivered without it: {', '.join(map(str, unread[:20]))}"
                                   + (" ..." if len(unread) > 20 else ""))
        instructions = {index_of(r["episode_id"]): str(r.get("instruction") or "") for r in rows}
    return TaskText(run_dir, instructions)


def render(payload: dict) -> str:
    c = payload["counts"]
    where = f" (revision {payload['revision']})"
    text = (f"final{where}: {c['total']} episodes - passed {c['passed']}, reject "
            f"{c['reject']}, held {c['held']}; {c['review']} to review")
    if c.get("skipped"):
        text += f"; {c['skipped']} left out for missing source files"
    return text
