"""``curation plan`` - the execution plan (design doc 02 §3.2, 04 §3).

Pure computation, no network: the stages, each stage's concurrency and gates,
and the VLM merge proposal, from a preflight result, the selected modules and
episodes and the upper bounds (D31). The Daemon calls the same library
(``curation.planner.build_plan``); this command makes a plan reproducible from
the command line. ``--out`` also writes it (the run directory's ``plan.json``).
"""
from __future__ import annotations

import argparse
import os

from . import runctx
from .errors import UsageError
from .framework import Context, Result


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "plan", parents=parents,
        help="derive the execution plan: stages, concurrency, gates, merge proposal",
        description="Build the execution plan of a task from a preflight result. Nothing "
                    "is read from the dataset and no model is called.")
    p.add_argument("--preflight", required=True, metavar="FILE",
                   help="the output of curation preflight --json")
    p.add_argument("--modules", required=True, metavar="IDS", help="selected modules")
    p.add_argument("--episodes", metavar="EXPR",
                   help="selected episodes (default: all of the preflight's episode_count)")
    p.add_argument("--unlabeled", metavar="EXPR",
                   help="episodes without a task text, when known (exact autolabel stage)")
    p.add_argument("--cpu-cores", type=int, metavar="N",
                   help="CPU cores of the node (default: this machine's)")
    p.add_argument("--vlm-parallelism", type=int, metavar="N",
                   help="parallelism of the chosen model or backend")
    p.add_argument("--backend-parallelism", type=int, metavar="N",
                   help="parallelism configured on the backend, when it differs")
    p.add_argument("--task-cpu-concurrency", type=int, metavar="N",
                   help="the task's upper bound on CPU concurrency (params.limits)")
    p.add_argument("--task-vlm-parallelism", type=int, metavar="N",
                   help="the task's upper bound on VLM parallelism (params.limits)")
    p.add_argument("--running-tasks", type=int, default=1, metavar="N",
                   help="tasks running when this one starts (the VLM budget is shared)")
    p.add_argument("--site-config", metavar="FILE",
                   help="the planner's site settings (concurrency / vlm blocks, YAML or JSON)")
    p.add_argument("--out", metavar="FILE", help="also write the plan here (plan.json)")
    p.set_defaults(func=run)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline.records import write_json_atomic
    from ..planner import PlanError, PlanLimits, SiteConfig, build_plan

    preflight = runctx.read_json(args.preflight, "--preflight")
    if not isinstance(preflight, dict) or "dataset" not in preflight:
        raise UsageError(f"--preflight {args.preflight}: not a preflight result")
    count = int((preflight.get("dataset") or {}).get("episode_count") or 0)
    episodes = runctx.read_episode_file(args.episodes)
    if episodes is not None:
        bad = [e for e in episodes if e >= count]
        if bad:
            raise UsageError(f"--episodes: {len(bad)} index(es) beyond the dataset's "
                             f"{count} episodes (first {bad[0]})")
    unlabeled = runctx.read_episode_file(args.unlabeled)
    site = {}
    if args.site_config:
        import yaml

        try:
            with open(args.site_config, encoding="utf-8") as fh:
                site = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as e:
            raise UsageError(f"--site-config {args.site_config}: {e}") from None
    try:
        limits = PlanLimits(cpu_concurrency=args.task_cpu_concurrency,
                            vlm_parallelism=args.task_vlm_parallelism,
                            model_parallelism=args.vlm_parallelism,
                            backend_parallelism=args.backend_parallelism,
                            cpu_cores=args.cpu_cores, running_tasks=args.running_tasks)
        plan = build_plan(preflight, [m.strip() for m in args.modules.split(",") if m.strip()],
                          episodes, limits, SiteConfig.from_mapping(site),
                          unlabeled_episodes=unlabeled)
    except (PlanError, ValueError) as e:
        raise UsageError(f"cannot plan: {e}") from None
    if args.out:
        write_json_atomic(os.path.abspath(args.out), plan)
    return Result(plan, human=render(plan))


def render(plan: dict) -> str:
    lines = [f"VLM parallelism {plan['vlm_parallelism']} "
             f"(bound by {plan['limits']['vlm_parallelism']['bound_by']}), CPU concurrency "
             f"{plan['limits']['cpu_concurrency']['value']}"]
    for st in plan["stages"]:
        what = ", ".join(st.get("modules") or []) or st.get("phase", st.get("command", ""))
        extra = f" gates {st['gates']}" if st.get("gates") else ""
        extra += f" x{st['concurrency']}" if st.get("concurrency") else ""
        lines.append(f"  {st['id']:<10} {what} <- {st.get('episodes', '')}{extra}")
    est = plan["estimates"]
    lines.append(f"estimate: {est['vlm_requests']} VLM requests, ~{int(est['wall_clock_s'])}s")
    return "\n".join(lines)
