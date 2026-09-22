"""``curation adjudicate-apply`` - apply this task's human decisions (design doc 02 §3.9).

The first step of v1's ``rejudge``: decisions are recorded as applied - no model
call, no export (D9) - and the command names what runs next:

* ``rerun_task_success``: relabelled episodes without a human task verdict,
  judged again by ``check --modules task_success`` with the new label;
* ``profile_resync``: episodes the skill profile has to re-file or drop
  (``check --modules skill_profile --incremental``).

``decisions.json`` (``cli/decisions.schema.json``) holds only this task's
decisions not applied yet (D32); a decision already applied is skipped by id,
so running it twice changes nothing. v1's priorities are applied by
``aggregate --phase final``: "discard" beats any task verdict, a relabel with a
human task verdict is not re-judged, appeals only for task_success rejects,
"unsure" changes nothing.
"""
from __future__ import annotations

import argparse

from . import runctx
from .errors import UsageError
from .framework import Context, Result


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "adjudicate-apply", parents=parents,
        help="apply this task's human decisions (no model call, no export)",
        description="Record the task's human decisions as applied and list what has to "
                    "run next (task_success re-judging, skill profile re-sync).")
    runctx.add_run_dir(p)
    p.add_argument("--decisions", required=True, metavar="FILE",
                   help="decisions.json exported by the Daemon (this task's only)")
    p.set_defaults(func=run)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import adjudication

    run_dir = runctx.run_dir_of(args)
    doc = runctx.read_json(args.decisions, "--decisions")
    if not isinstance(doc, dict) or doc.get("schema_version") != "1.0" \
            or not isinstance(doc.get("decisions"), list):
        raise UsageError(f"--decisions {args.decisions}: not a decisions.json "
                         f"(schema_version 1.0 with a decisions list)")
    try:
        payload = adjudication.apply(run_dir, doc,
                                     appeal_admissible=_appeal_admissible(ctx, run_dir, doc))
    except adjudication.DecisionError as e:
        raise UsageError(f"--decisions {args.decisions}: {e}") from None
    ctx.log("info", f"applied {payload['applied']} decision(s), skipped "
                    f"{payload['skipped_already_applied']} already applied")
    return Result(payload, human=render(payload))


def _appeal_admissible(ctx: Context, run_dir: str, doc: dict):
    """``episode -> bool``: a reject a person may appeal now, from the results and the
    decisions already applied (D42); None when the file has no appeal."""
    eps = sorted({int(d["episode_index"]) for d in doc.get("decisions") or []
                  if isinstance(d, dict) and d.get("line") == "reject_appeal"
                  and isinstance(d.get("episode_index"), int)})
    if not eps:
        return None
    from ..pipeline import aggregate as agg
    from ..pipeline.adjudication import Decisions

    modules = runctx.selected_modules(argparse.Namespace(modules=None), run_dir)
    state = agg.RunState(run_dir, modules, eps, runctx.stage_config(ctx, modules))
    decided = agg.decide_all(state, Decisions.of(run_dir))
    return lambda ep: ep in decided and decided[ep].appeal_target is not None


def render(p: dict) -> str:
    lines = [f"applied {p['applied']} decision(s) ({p['skipped_already_applied']} already "
             f"applied)"]
    if p["rerun_task_success"]:
        lines.append("  re-judge task_success: " + ", ".join(map(str, p["rerun_task_success"])))
    if p["profile_resync"]:
        lines.append("  re-sync skill profile: " + ", ".join(map(str, p["profile_resync"])))
    return "\n".join(lines)
