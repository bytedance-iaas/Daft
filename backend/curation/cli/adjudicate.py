"""``curation adjudicate-apply`` - apply this task's human decisions (design doc 02 §3.8).

Decisions are recorded as applied - no model call - and the command names what runs next:
``rerun_task_success``, the relabels in force that no person concluded and that task_success
has not judged with their text yet (the ``new_label`` a person sent with their task verdict,
D68), judged again by ``check --modules task_success`` with the new label in the one request
(D39, D71).

``decisions.json`` (``cli/decisions.schema.json``) holds only this task's
decisions not applied yet (D32); a decision already applied is skipped by id,
so running it twice changes nothing. A line without an apply rule here, or an
appeal on an episode with no reject a person may appeal (D42), is refused
(exit 2), never skipped. The priorities are applied by ``aggregate``:
"discard" beats any task verdict, a relabel with a human task verdict
is not re-judged, an appeal lifts the episode's blocking findings and is admitted
only when every one of them is appealable (D42 by finding, design doc 17 §4.4),
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
                    "run next (the relabelled episodes task_success judges again).")
    runctx.add_run_dir(p)
    p.add_argument("--decisions", required=True, metavar="FILE",
                   help="decisions.json exported by the Daemon (this task's only)")
    p.set_defaults(func=run)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import adjudication

    run_dir = runctx.run_dir_of(args)
    doc = runctx.read_json(args.decisions, "--decisions")
    if not isinstance(doc, dict) or doc.get("schema_version") != "2.0" \
            or not isinstance(doc.get("decisions"), list) or set(doc) - {"schema_version", "decisions"}:
        raise UsageError(f"--decisions {args.decisions}: not a decisions.json "
                         f"(schema_version 2.0 with a decisions list and nothing else)")
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
    state = agg.RunState(run_dir, modules, eps)
    decided = agg.decide_all(state, Decisions.of(run_dir))
    return lambda ep: ep in decided and decided[ep].admissible


def render(p: dict) -> str:
    lines = [f"applied {p['applied']} decision(s) ({p['skipped_already_applied']} already "
             f"applied)"]
    if p["rerun_task_success"]:
        lines.append("  re-judge task_success: " + ", ".join(map(str, p["rerun_task_success"])))
    return "\n".join(lines)
