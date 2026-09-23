"""``curation check`` - run modules of one stage over a set of episodes (design doc 02 §3.5).

The most important command. One call runs the modules of **one** stage (D18):
``timestamp_check,kinematic_limits,motion_quality`` (numeric),
``visual_quality,video_action_sync`` (frame: one shared decode per camera),
``task_success`` (vlm), or one dataset-level module, ``dedup`` or
``skill_profile`` (the whole kept set in one call). Mixing stages is a usage
error. Advisory modules (registry 1.4: ``eef_video_consistency``) run in a call
of their own, on every episode given, with their parameters as ``--param``.
Results go to ``<run-dir>/checks/<module>/parts/<part>.jsonl``, one line per
episode as soon as it is done; the highest part wins, ``results.jsonl`` is the
compacted view.

Exit 0 does not mean every episode succeeded: ``--json`` gives per-verdict
counts and the error episodes, and the Daemon sets the module state from them.
Non-zero only when the module as a whole cannot run (4: endpoint down, circuit
breaker, a robot model the kinematic registry lacks), on signals (5 / 130),
on a changed source (6).
"""
from __future__ import annotations

import argparse
import os
import sys

from . import modparams, runctx
from .errors import ModuleFailed, UsageError
from .framework import Context, Result

DATASET_MODULES = ("dedup", "skill_profile")


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "check", parents=parents,
        help="run the modules of one stage over a set of episodes",
        description="Run quality-check modules of one funnel stage (or one dataset-level "
                    "module) over the given episodes. One result line per episode is "
                    "appended to the run directory as soon as it is done.")
    p.add_argument("--modules", required=True, metavar="IDS",
                   help="comma-separated modules of one stage, e.g. "
                        "visual_quality,video_action_sync")
    runctx.add_source(p)
    runctx.add_run_dir(p)
    runctx.add_episodes(p)
    p.add_argument("--part", metavar="NNNN",
                   help="the part to write (default: one more than the highest so far)")
    p.add_argument("--resume", action="store_true",
                   help="skip episodes that already have a result that is not an error")
    p.add_argument("--plan-stage", metavar="FILE",
                   help="this stage of plan.json (gates, concurrency, merge proposal)")
    p.add_argument("--incremental", action="store_true",
                   help="skill_profile: keep the taxonomy, re-file only what changed")
    p.add_argument("--survivors-out", metavar="FILE",
                   help="write the episodes that go on to the next stage, one per line")
    p.add_argument("--pipeline-state", metavar="SQLITE",
                   help=argparse.SUPPRESS)
    p.add_argument("--pipeline-next", metavar="STAGE", choices=("frame", "vlm", "done"),
                   default="done",
                   help=argparse.SUPPRESS)
    runctx.add_vlm(p)
    runctx.add_behaviour(p)
    modparams.add_argument(p)
    p.set_defaults(func=run)


def _modules(raw: str) -> tuple[list[str], str]:
    from ..contracts import modules as registry

    mods = [m.strip() for m in str(raw).split(",") if m.strip()]
    if not mods:
        raise UsageError("--modules lists no module")
    unknown = [m for m in mods if m not in registry.ids()]
    if unknown:
        raise UsageError(f"unknown module(s) {unknown}; known: {', '.join(registry.ids())}")
    stages = {registry.get(m).stage for m in mods}
    if len(stages) != 1:
        raise UsageError(f"--modules {','.join(mods)} spans stages {sorted(stages)}; "
                         f"one call runs one stage")
    stage = stages.pop()
    advisory = [m for m in mods if m in registry.advisory_ids()]
    if advisory and len(advisory) != len(mods):
        raise UsageError(f"advisory module(s) {advisory} run in a call of their own (they read every "
                         f"selected episode, not the survivors); leave them out of this call")
    if stage == "post_verdict" and len(mods) != 1:
        raise UsageError("dedup and skill_profile run one at a time (profile reads the "
                         "kept set after dedup)")
    ordered = [m for m in registry.ids() if m in mods]
    return ordered, stage


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import records

    modules, stage = _modules(args.modules)
    run_dir = runctx.run_dir_of(args, create=True)
    if args.part is not None and not (len(args.part) == 4 and args.part.isdigit()):
        raise UsageError(f"--part must be four digits such as 0003, got {args.part!r}")
    plan_stage = runctx.load_plan_stage(args.plan_stage, modules)
    if args.pipeline_state and stage not in ("numeric", "frame", "vlm"):
        raise UsageError("--pipeline-state is only valid for funnel stages")
    storage = runctx.open_input(ctx, args)
    available, info = runctx.dataset_episodes(ctx, storage)
    episodes, warning = runctx.resolve_episodes(args, available)
    if warning:
        ctx.log("warn", warning)
    episodes = runctx.leave_out_skipped(ctx, args, episodes)
    part = args.part or records.next_part(run_dir, modules)
    guard = runctx.source_guard(ctx, args, storage)
    input_dir = storage.root if not storage.remote else storage.uri
    ctx.log("info", f"check {','.join(modules)}: {len(episodes)} episode(s), part {part}")

    from ..contracts import modules as registry

    if all(m in registry.advisory_ids() for m in modules) and stage == "vlm":
        from . import eef_review

        payload, survivors = eef_review.run(ctx, args, modules, run_dir, storage, episodes, part, guard,
                                            plan_stage)
    elif all(m in registry.advisory_ids() for m in modules):
        from . import eef_check

        payload, survivors = eef_check.run(ctx, args, modules, run_dir, storage, episodes, part, guard)
    elif stage in ("numeric", "frame"):
        payload, survivors = _funnel_cpu(ctx, args, modules, run_dir, input_dir, episodes,
                                         part, plan_stage, guard, info)
    elif stage == "vlm":
        payload, survivors = _funnel_vlm(ctx, args, modules, run_dir, input_dir, episodes,
                                         part, plan_stage, guard)
    elif modules == ["dedup"]:
        payload, survivors = _dedup(ctx, args, run_dir, input_dir, episodes, part, guard)
    else:
        payload, survivors = _profile(ctx, args, run_dir, input_dir, episodes, part,
                                      plan_stage, guard)
    if args.survivors_out:
        records.write_text_atomic(os.path.abspath(args.survivors_out),
                                  "".join(f"{e}\n" for e in survivors))
    return Result(payload, human=render(payload))


def _check_embodiment(args, info: dict) -> None:
    from ..registry.registry import EmbodimentRegistry

    emb = args.embodiment_id or str(info.get("robot_type") or "unknown")
    try:
        EmbodimentRegistry().get(emb)
    except Exception:  # noqa: BLE001 - the registry says why in its own words
        raise ModuleFailed(f"kinematic_limits: robot model {emb!r} is not in the embodiment "
                           f"registry; preflight marks it unsupported, leave it out",
                           {"embodiment_id": emb}) from None


def _funnel_cpu(ctx, args, modules, run_dir, input_dir, episodes, part, plan_stage, guard,
                info):
    from ..pipeline.check_stage import StageOptions, StageRun
    from ..registry.registry import EmbodimentRegistry

    cache = getattr(args, "_worker_cache", None)
    prepared = cache.get("cpu") if cache is not None else None
    if prepared is None:
        if "kinematic_limits" in modules:
            _check_embodiment(args, info)
        cfg = runctx.stage_config(ctx, modules)
        registry = EmbodimentRegistry()
        if cache is not None:
            cache["cpu"] = (cfg, registry)
    else:
        cfg, registry = prepared
    opts = StageOptions(run_dir=run_dir, input_dir=input_dir, modules=modules,
                        episodes=episodes, part=part, cfg=cfg, resume=args.resume,
                        concurrency=runctx.cpu_workers(args, plan_stage),
                        embodiment_id=args.embodiment_id, max_episodes=args.max_episodes,
                        verify_source=guard, pipeline_state=args.pipeline_state,
                        pipeline_next=args.pipeline_next,
                        episode_stream=getattr(args, "_episode_stream", None))
    stage = StageRun(ctx, opts, registry)
    return stage.run(), stage.survivors()


def _merge_strategy(ctx, plan_stage, cfg, modules):
    """The VLM request merge strategy of this call (W6, doc 04 §4.2): the plan stage's
    proposal unless ``vlm.merge.enabled`` is off. Only modules that declare merge units
    take part; task_success is an evidence chain and never does (D23), so its requests
    always go out one by one, which is strategy ``none``."""
    from ..contracts import modules as registry
    from ..planner.merge import NoMerge, merge_enabled, strategy_for_stage

    try:
        strategy = strategy_for_stage(plan_stage, enabled=merge_enabled(cfg))
    except ValueError as e:
        raise UsageError(f"--plan-stage: {e}") from None
    mergeable = [m for m in modules if getattr(registry.get(m), "merge_units", None)]
    if not isinstance(strategy, NoMerge) and not mergeable:
        ctx.log("warn", f"the plan proposes merging requests, but none of {', '.join(modules)} "
                        f"declares merge units: every request goes out on its own")
    return strategy


def _funnel_vlm(ctx, args, modules, run_dir, input_dir, episodes, part, plan_stage, guard):
    from ..pipeline import funnel
    from ..pipeline.check_stage import StageOptions, StageRun, TaskClients
    from ..pipeline.rows import index_of
    from ..pipeline.tasktext import TaskText
    from ..registry.registry import EmbodimentRegistry

    cache = getattr(args, "_worker_cache", None)
    prepared = cache.get("vlm") if cache is not None else None
    if prepared is None:
        gates = runctx.vlm_gates(args, plan_stage)
        cfg = runctx.stage_config(ctx, modules, gates=gates, args=args)
        _merge_strategy(ctx, plan_stage, cfg, modules)
    else:
        gates, cfg = prepared["gates"], prepared["cfg"]
    if guard is not None:
        guard([])                        # metadata and the semantics sample, read next
    instructions = {index_of(r["episode_id"]): str(r.get("instruction") or "")
                    for r in runctx.meta_rows(input_dir, episodes, args,
                                              what="check:task_success")}
    if prepared is None:
        task_text = TaskText(run_dir, instructions)
        session = runctx.VlmSession(ctx, args, cfg, "task_success", run_dir)
        session.__enter__()
        try:
            from ..adapters.vlm_client import vlm_completion_from_config

            vlm_completion = vlm_completion_from_config(cfg)
            try:
                cam_voter = funnel.build_endstate_voter(cfg, gates)
            except Exception as e:  # noqa: BLE001 - v1: warn and judge on the score alone
                cam_voter = None
                ctx.log("warn", f"per-camera review unavailable ({type(e).__name__}: {e}); "
                                "task_success judges on the score layer alone")
            try:
                arb_deps = funnel.build_arbitration_deps(cfg, gates)
            except Exception as e:  # noqa: BLE001 - v1: abstentions stay with people
                arb_deps = None
                ctx.log("warn", f"evidence arbitration unavailable ({type(e).__name__}: {e})")
        except BaseException:
            session.__exit__(*sys.exc_info())
            raise
        clients = TaskClients(vlm_completion, cam_voter, arb_deps)
        if cache is not None:
            cache["vlm"] = {"gates": gates, "cfg": cfg, "task_text": task_text,
                            "session": session, "clients": clients}
    else:
        task_text = prepared["task_text"]
        task_text.instructions.update(instructions)
        clients = prepared["clients"]
    opts = StageOptions(run_dir=run_dir, input_dir=input_dir, modules=modules,
                        episodes=episodes, part=part, cfg=cfg, resume=args.resume,
                        concurrency=int(gates["episode"]),
                        embodiment_id=args.embodiment_id, max_episodes=args.max_episodes,
                        task_clients=clients, task_text=task_text,
                        evidence_mode=str(cfg.get("pipeline", {})
                                          .get("evidence_frames", "flagged")),
                        verify_source=guard, pipeline_state=args.pipeline_state,
                        pipeline_next=args.pipeline_next,
                        episode_stream=getattr(args, "_episode_stream", None))
    stage = StageRun(ctx, opts, EmbodimentRegistry())
    try:
        payload = stage.run()
    finally:
        if cache is None:
            session.__exit__(*sys.exc_info())
    return payload, stage.survivors()


def _dedup(ctx, args, run_dir, input_dir, episodes, part, guard):
    from ..pipeline.dataset_stages import run_dedup

    if args.resume:
        ctx.log("info", "--resume: dedup always runs on the whole kept set")
    if guard is not None:
        guard(episodes)
    payload = run_dedup(ctx, run_dir, input_dir, episodes, part,
                        embodiment_id=args.embodiment_id)
    dups = {e for e, rec in _latest(run_dir, "dedup").items() if rec["verdict"] == "fail"}
    errors = set(payload["modules"]["dedup"]["error_episodes"])
    left_out = {s["episode_index"] for s in
                payload["modules"]["dedup"].get("skipped_missing_source") or []}
    return payload, [e for e in episodes if e not in dups and e not in errors
                     and e not in left_out]


def _latest(run_dir, module):
    from ..pipeline.records import latest_results

    return latest_results(run_dir, module)


def _profile(ctx, args, run_dir, input_dir, episodes, part, plan_stage, guard):
    from ..adapters import vlm_client
    from ..dataset_level.caption import make_vlm_captioner
    from ..pipeline.dataset_stages import run_skill_profile
    from ..pipeline.tasktext import load_autolabel, load_relabels, precomputed_captions

    gates = runctx.vlm_gates(args, plan_stage)
    cfg = runctx.stage_config(ctx, ["skill_profile"], gates=gates, args=args)
    episodes, restored = _profile_members(ctx, run_dir, episodes)
    if guard is not None:
        guard(episodes)
    rows = runctx.meta_rows(input_dir, episodes, args, what="check:skill_profile")
    from ..pipeline.dataset_stages import leave_out_missing_source
    from ..pipeline.rows import index_of
    from ..pipeline.skipped import as_list

    got = {index_of(r["episode_id"]) for r in rows}
    missing = leave_out_missing_source(ctx, run_dir, input_dir,
                                       [e for e in episodes if e not in got])
    auto_caps = {f"ep{i:06d}": c
                 for i, c in precomputed_captions(load_autolabel(run_dir)).items()}
    sp = cfg.get("skill_profile") or {}
    with runctx.VlmSession(ctx, args, cfg, "skill_profile", run_dir):
        v = cfg["checks"]["task_success"]["vlm"]
        captioner = make_vlm_captioner(v["endpoint"], v["model"],
                                       timeout_s=vlm_client.timeout_for("caption", v),
                                       api_key_env=v.get("api_key_env"),
                                       max_in_flight=int(sp.get("caption_concurrency", 8)),
                                       thinking=cfg.get("pipeline", {}).get("thinking"))
        llm_ask = vlm_client.make_llm_ask(
            v["endpoint"], v["model"], timeout_s=vlm_client.timeout_for("llm", v),
            api_key_env=v.get("api_key_env"),
            thinking=cfg.get("pipeline", {}).get("thinking"),
            max_in_flight=max(int(sp.get("llm_concurrency", 16)),
                              int(sp.get("audit_concurrency", 16))))
        payload = run_skill_profile(ctx, run_dir, rows, cfg, captioner, llm_ask, auto_caps,
                                    part, incremental=args.incremental,
                                    relabels=load_relabels(run_dir), restored=restored)
    if missing:
        payload["modules"]["skill_profile"]["skipped_missing_source"] = as_list(missing)
    errors = set(payload["modules"]["skill_profile"]["error_episodes"])
    return payload, [e for e in episodes if e not in errors and e not in missing]


def _profile_members(ctx, run_dir: str, episodes: list[int]) -> tuple[list[int], set[int]]:
    """The given episodes skill_profile files, and the ones a person restored.

    The byte copies dedup found are left out: after an adjudication ``--episodes``
    is the new ``keep.txt`` and dedup is not run again, its first result stands
    (v1's rejudge). An episode a person brought into the delivery is never
    deduplicated and is filed from its text (v1's ``_sync_profile``).
    """
    from ..pipeline import aggregate as agg
    from ..pipeline.adjudication import Decisions
    from ..pipeline.records import latest_results

    decisions = Decisions.of(run_dir)
    if not latest_results(run_dir, "dedup") and not decisions.applied:
        return list(episodes), set()
    task = [m for m in runctx.selected_modules(argparse.Namespace(modules=None), run_dir)
            if m in agg.FUNNEL_MODULES or m == "dedup"]
    state = agg.RunState(run_dir, task, episodes, runctx.stage_config(ctx, task))
    members, restored = agg.profile_members(state, decisions)
    left_out = len(episodes) - len(members)
    if left_out:
        ctx.log("info", f"skill_profile: {left_out} episode(s) dedup found to be byte copies "
                        f"are left out")
    return members, restored


def render(payload: dict) -> str:
    lines = []
    for m, entry in payload["modules"].items():
        c = entry["episodes"]
        lines.append(f"{m} (part {entry['part']}): {c['total']} episodes - pass {c['pass']}, "
                     f"fail {c['fail']}, abstain {c['abstain']}, scored {c['scored']}, "
                     f"error {c['error']}")
        if entry["error_episodes"]:
            shown = ", ".join(str(e) for e in entry["error_episodes"][:10])
            more = ", ..." if len(entry["error_episodes"]) > 10 else ""
            lines.append(f"  errors (held until retried): {shown}{more}")
    return "\n".join(lines)
