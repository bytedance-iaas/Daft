"""``curation aggregate`` - verdicts from the module results (design doc 02 §3.6).

Pure computation in seconds, recomputed in full every time:

* ``--phase funnel``: the six funnel checks -> ``verdicts.jsonl`` (keep / drop /
  held per episode, v1's ``pipeline/verdict.py`` rules plus D35) and
  ``keep.txt`` (the input of dedup and skill_profile), into
  ``revisions/r<NNNN>/`` with ``--revision``, else into ``<run-dir>/funnel/``.
  ``keep.txt`` follows the applied human decisions: a discarded episode or one
  judged failed leaves it, a restored appeal joins it (``decided_in`` /
  ``decided_out`` count them); dedup is not run again after an adjudication;
* ``--phase final --revision N``: adds dedup, skill_profile and the applied
  human decisions and writes ``passed`` / ``reject`` / ``held`` (disjoint and
  complete) and the ``review`` view into ``revisions/r<NNNN>/``. A revision that
  already has ``commit.json`` is never written again.

The selected modules come from ``--modules`` (else ``<run-dir>/plan.json``); the
episodes from ``--episodes`` (else every episode any module has a result for).
With ``--input`` the delivered task text of annotated episodes is read from the
dataset's metadata; without it only captions and human relabels are listed.
"""
from __future__ import annotations

import argparse
import os

from . import runctx
from .errors import UsageError
from .framework import Context, Result

SCHEMA_VERSION = "1.0"


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "aggregate", parents=parents,
        help="compute the verdicts: funnel (keep/drop/held) or final (the four lists)",
        description="Recompute the verdicts of a run directory from its module results.")
    runctx.add_run_dir(p)
    p.add_argument("--phase", required=True, choices=("funnel", "final"))
    p.add_argument("--revision", type=int, metavar="N",
                   help="the result revision to write (required for --phase final)")
    p.add_argument("--modules", metavar="IDS",
                   help="the task's selected modules (default: from plan.json)")
    p.add_argument("--episodes", metavar="EXPR",
                   help="the task's episodes (default: every episode with a result)")
    p.add_argument("--input", metavar="URI",
                   help="the dataset, to list the delivered task text of annotated episodes")
    p.add_argument("--source", choices=("tos", "public", "local"), default=None)
    p.add_argument("--embodiment-id", metavar="ID")
    p.add_argument("--max-episodes", type=int, metavar="N")
    p.add_argument("--selection", metavar="EXPR",
                   help="accepted like the other reading commands (mcap / lance); the task "
                        "texts aggregate reads do not depend on it")
    p.set_defaults(func=run)


def _episodes(args, run_dir: str, modules: list[str]) -> list[int]:
    from ..pipeline.records import latest_results
    from ..pipeline.tasktext import load_autolabel

    given = runctx.read_episode_file(args.episodes)
    if given is not None:
        return given
    found: set[int] = set(load_autolabel(run_dir))
    for m in modules:
        found |= set(latest_results(run_dir, m))
    if not found:
        raise UsageError("no episode has a result yet; pass --episodes")
    return sorted(found)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import aggregate as agg
    from ..pipeline.records import committed_revisions, revision_dir

    run_dir = runctx.run_dir_of(args)
    if args.revision is not None and args.revision < 1:
        raise UsageError("--revision starts at 1")
    if args.phase == "final" and args.revision is None:
        raise UsageError("--phase final needs --revision N (the Daemon allocates it)")
    if args.revision is not None and args.revision in committed_revisions(run_dir):
        raise UsageError(f"revision {args.revision} is committed (commit.json); a result "
                         f"revision is never rewritten - use a new one")
    modules = runctx.selected_modules(args, run_dir)
    episodes = _episodes(args, run_dir, modules)
    # left out for missing source files (D40, as v1 does): in no list, not in total
    from ..pipeline.skipped import all_skipped

    skipped = all_skipped(run_dir)
    left_out = [e for e in episodes if e in skipped]
    episodes = [e for e in episodes if e not in skipped]
    cfg = runctx.stage_config(ctx, modules)
    state = agg.RunState(run_dir, modules, episodes, cfg)
    out_dir = (revision_dir(run_dir, args.revision) if args.revision is not None
               else os.path.join(run_dir, "funnel"))
    ctx.progress(f"aggregate:{args.phase}", 0, 1)
    if args.phase == "funnel":
        from ..pipeline.adjudication import Decisions

        lines = agg.funnel(state)
        decided = agg.decide_all(state, Decisions.of(run_dir), lines)
        keep = [e for e, d in decided.items() if d.kept]
        files = agg.write_funnel(out_dir, lines, keep)
        counts = {"total": len(lines), "keep": 0, "drop": 0, "held": 0}
        for ln in lines:
            counts[ln.verdict] += 1
        machine_keep = {ln.episode_index for ln in lines if ln.verdict == "keep"}
        counts["decided_in"] = len(set(keep) - machine_keep)
        counts["decided_out"] = len(machine_keep - set(keep))
        if counts["decided_in"] or counts["decided_out"]:
            ctx.log("info", f"keep.txt follows the applied human decisions: "
                            f"{counts['decided_in']} in, {counts['decided_out']} out")
    else:
        from ..pipeline.adjudication import Decisions
        from ..pipeline.dataset_stages import load_profile
        from ..pipeline.records import write_json_atomic

        decisions = Decisions.of(run_dir)
        task_text = _task_text(ctx, args, run_dir, episodes)
        profile = load_profile(run_dir) if "skill_profile" in modules else None
        result = agg.final(state, args.revision, decisions, task_text,
                           (profile or {}).get("label_audit"))
        files = agg.write_final(out_dir, result)
        write_json_atomic(os.path.join(out_dir, "adjudications.json"),
                          {"applied": decisions.ids()})
        counts = {"total": len(episodes), "passed": result["passed"]["count"],
                  "reject": result["reject"]["count"], "held": result["held"]["count"],
                  "review": result["review"]["count"]}
    counts["skipped"] = len(left_out)
    ctx.progress(f"aggregate:{args.phase}", 1, 1)
    rel = {k: os.path.relpath(v, run_dir).replace(os.sep, "/") for k, v in files.items()}
    payload = {"schema_version": SCHEMA_VERSION, "phase": args.phase,
               "revision": args.revision, "counts": counts, "files": rel}
    return Result(payload, human=render(payload))


def _task_text(ctx, args, run_dir: str, episodes: list[int]):
    from ..pipeline.rows import index_of
    from ..pipeline.tasktext import TaskText

    instructions: dict[int, str] = {}
    if args.input:
        src = runctx.open_source(ctx, args)
        instructions = {index_of(r["episode_id"]): str(r.get("instruction") or "")
                        for r in runctx.meta_rows(src, episodes, args, what="aggregate")}
    return TaskText(run_dir, instructions)


def render(payload: dict) -> str:
    c = payload["counts"]
    where = f" (revision {payload['revision']})" if payload["revision"] else ""
    if payload["phase"] == "funnel":
        text = (f"funnel{where}: {c['total']} episodes - keep {c['keep']}, drop {c['drop']}, "
                f"held {c['held']}")
        if c.get("decided_in") or c.get("decided_out"):
            text += (f"; keep.txt after the human decisions: {c['decided_in']} in, "
                     f"{c['decided_out']} out")
        return text
    text = (f"final{where}: {c['total']} episodes - passed {c['passed']}, reject "
            f"{c['reject']}, held {c['held']}; {c['review']} to review")
    if c.get("skipped"):
        text += f"; {c['skipped']} left out for missing source files"
    return text
