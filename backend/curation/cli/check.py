"""``curation check`` - run modules of one stage over a set of episodes (design doc 02 §3.5).

The most important command. One call runs the modules of **one** stage (D18):
``timestamp_check,kinematic_limits,motion_quality`` (numeric),
``visual_quality,video_action_sync`` (frame: one shared decode per camera),
``task_success`` (vlm), ``data_integrity`` (integrity, first; design doc 14), or one
dataset-level module, ``dedup`` or
the whole kept set in one call. Mixing stages is a usage
error. The modules v2 runs itself (``data_integrity``, ``eef_video_consistency``)
take their parameters as ``--param``.
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

DATASET_MODULES = ("dedup",)


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
    p.add_argument("--survivors-out", metavar="FILE",
                   help="write the episodes that go on to the next stage, one per line")
    p.add_argument("--pipeline-state", metavar="SQLITE",
                   help=argparse.SUPPRESS)
    p.add_argument("--pipeline-next", metavar="STAGE",
                   choices=("numeric", "frame", "vlm", "dedup", "done"), default="done",
                   help=argparse.SUPPRESS)
    halves = p.add_mutually_exclusive_group()
    halves.add_argument("--prep", action="store_true",
                        help="run the modules' CPU halves (registry 5.3: their prep_stage, vlm_prep): measure, render "
                             "and keep each episode's requests for the model half; with no model to ask (--no-vlm, "
                             "use_vlm=false) the CPU half writes the records itself. No model is called")
    halves.add_argument("--prepared", action="store_true",
                        help="the model half of modules whose CPU half ran in vlm_prep: read the kept requests, ask, "
                             "merge and write the records")
    runctx.add_vlm(p)
    runctx.add_behaviour(p)
    modparams.add_argument(p)
    p.set_defaults(func=run)


def _modules(raw: str, prep: bool = False) -> tuple[list[str], str]:
    from ..contracts import modules as registry

    mods = [m.strip() for m in str(raw).split(",") if m.strip()]
    if not mods:
        raise UsageError("--modules lists no module")
    unknown = [m for m in mods if m not in registry.ids()]
    if unknown:
        raise UsageError(f"unknown module(s) {unknown}; known: {', '.join(registry.ids())}")
    if prep:                                           # the CPU halves (registry 5.3)
        whole = [m for m in mods if not registry.get(m).prep_stage]
        if whole:
            raise UsageError(f"--prep: {', '.join(whole)} has no CPU half of its own (no prep_stage)")
    stages = {registry.get(m).prep_stage if prep else registry.get(m).stage for m in mods}
    if len(stages) != 1:
        raise UsageError(f"--modules {','.join(mods)} spans stages {sorted(stages)}; "
                         f"one call runs one stage")
    stage = stages.pop()
    for m in mods:
        host = registry.get(m).rides_on
        if host and host not in mods:
            raise UsageError(f"{m} is answered inside {host}'s model requests and runs with it: "
                             f"--modules {host} brings it along; it cannot run on its own")
    mods = registry.with_riders(mods)                  # a host's riders always run with it
    ordered = [m for m in registry.ids() if m in mods]
    return ordered, stage


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..pipeline import records

    modules, stage = _modules(args.modules, prep=getattr(args, "prep", False))
    run_dir = runctx.run_dir_of(args, create=True)
    if args.part is not None and not (len(args.part) == 4 and args.part.isdigit()):
        raise UsageError(f"--part must be four digits such as 0003, got {args.part!r}")
    # a module of two halves is in two stages of the plan: the call names its own
    plan_stage = runctx.load_plan_stage(args.plan_stage, modules,
                                        stage_id=stage if getattr(args, "prep", False) or getattr(args, "prepared", False)
                                        else None)
    runctx.apply_thread_limit()
    if args.pipeline_state and stage not in ("integrity", "numeric", "frame", "vlm_prep", "vlm", "dedup"):
        raise UsageError("--pipeline-state is only valid for a block's segments")
    src = runctx.open_source(ctx, args)
    storage = src.storage
    available, info = runctx.dataset_episodes(ctx, src)
    episodes, warning = runctx.resolve_episodes(args, available)
    if warning:
        ctx.log("warn", warning)
    episodes = runctx.leave_out_skipped(ctx, args, episodes)
    part = args.part or records.next_part(run_dir, modules)
    guard = runctx.source_guard(ctx, args, src)
    ctx.log("info", f"check {','.join(modules)}: {len(episodes)} episode(s), part {part}")

    from ..contracts import modules as registry

    eef = [m for m in modules if "eef_input" in registry.get(m).needs]
    if eef and src.kind == "lance":
        raise ModuleFailed(f"{', '.join(eef)}: EEF-video consistency reads LeRobot and mcap "
                           f"datasets only, not {src.kind}; preflight marks it unsupported, leave it out",
                           {"modules": eef, "format": src.kind})
    if stage == "integrity":
        payload, survivors = _integrity(ctx, args, modules, run_dir, src, episodes, part,
                                        plan_stage, guard)
    elif stage in ("numeric", "frame", "dedup"):
        payload, survivors = _funnel_cpu(ctx, args, modules, run_dir, src, episodes,
                                         part, plan_stage, guard, info)
    else:
        payload, survivors = _funnel_vlm(ctx, args, modules, run_dir, src, episodes,
                                         part, plan_stage, guard)
    if args.survivors_out:
        records.write_text_atomic(os.path.abspath(args.survivors_out),
                                  "".join(f"{e}\n" for e in survivors))
    if src.container and stage in ("numeric", "frame", "vlm"):
        from .containers import write_source_info

        if guard is not None and src.kind == "mcap":
            guard([min(src.numbering())])     # the episode v1 takes the dataset info from
        write_source_info(run_dir, src, args.embodiment_id)
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


def _container_options(args, src) -> dict:
    """StageOptions of an mcap / lance source (D44): the task's selection resolves the
    semantics like v1's one read of it, a remote dataset's episodes are fetched before
    they are read, and the task text comes with each row."""
    if not src.container:
        return {}
    return {"selection": runctx.selection_of(args), "fetch": src.fetch,
            "row_instructions": True, "fmt": src.kind}


def _episode_blocks(src):
    """The vlm stage's per-episode block scope of a remote LeRobot dataset (design doc 23 §3.2): task_success
    and the EEF module read the episode's videos once, only their windows. mcap / lance read their own way."""
    if not src.remote or src.container:
        return None
    from ..streams import blockcache

    return lambda: blockcache.episode(src.storage, src.listing)


def _funnel_cpu(ctx, args, modules, run_dir, src, episodes, part, plan_stage, guard,
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
    opts = StageOptions(run_dir=run_dir, input_dir=src.input_dir, modules=modules,
                        params=modparams.parse(getattr(args, "param", None)),
                        episodes=episodes, part=part, cfg=cfg, resume=args.resume,
                        # dedup keeps its streaming state in one process, so it judges one at a
                        # time (D70); its cost is the read, which overlaps the other segments
                        concurrency=1 if "dedup" in modules else runctx.cpu_workers(args, plan_stage),
                        embodiment_id=args.embodiment_id, max_episodes=args.max_episodes,
                        verify_source=guard, pipeline_state=args.pipeline_state,
                        pipeline_next=args.pipeline_next,
                        episode_stream=getattr(args, "_episode_stream", None),
                        **_container_options(args, src))
    stage = StageRun(ctx, opts, registry)
    return stage.run(), stage.survivors()


def _integrity(ctx, args, modules, run_dir, src, episodes, part, plan_stage, guard):
    """The data integrity module (design doc 14): v2's own gate, first in the funnel. Its
    judge does the dataset-level work once per process (a pipeline worker keeps it)."""
    from ..extensions.integrity import MODULE_ID, IntegrityJudge
    from ..pipeline.check_stage import StageOptions, StageRun
    from ..pipeline.records import latest_results
    from ..registry.registry import EmbodimentRegistry

    cache = getattr(args, "_worker_cache", None)
    judge = cache.get("integrity") if cache is not None else None
    if judge is None:
        params = modparams.with_defaults(MODULE_ID, modparams.parse(getattr(args, "param", None)).get(MODULE_ID))
        judge = IntegrityJudge(ctx, src, ctx.config(), params, run_dir,
                               selection=runctx.selection_of(args), max_episodes=args.max_episodes)
        if guard is not None:
            guard([])                    # the metadata the dataset-level checks read
        judge.open(list(episodes))
        if cache is not None:
            cache["integrity"] = judge
    else:
        judge.rebind(src)
    stale = judge.stale(latest_results(run_dir, MODULE_ID, list(episodes)))
    opts = StageOptions(run_dir=run_dir, input_dir=src.input_dir, modules=modules,
                        params=modparams.parse(getattr(args, "param", None)),
                        episodes=episodes, part=part, cfg=ctx.config(), resume=args.resume,
                        concurrency=runctx.cpu_workers(args, plan_stage),
                        embodiment_id=args.embodiment_id, max_episodes=args.max_episodes,
                        verify_source=guard, pipeline_state=args.pipeline_state,
                        pipeline_next=args.pipeline_next,
                        episode_stream=getattr(args, "_episode_stream", None),
                        integrity=judge, stale={MODULE_ID: stale},
                        **_container_options(args, src))
    stage = StageRun(ctx, opts, EmbodimentRegistry())
    payload = stage.run()
    if MODULE_ID in payload.get("modules", {}):
        # decode_test and the thresholds are part of the module's input
        payload["modules"][MODULE_ID]["input_digest"] = judge.input_digest(list(opts.episodes))
    return payload, stage.survivors()


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


def _funnel_vlm(ctx, args, modules, run_dir, src, episodes, part, plan_stage, guard):
    from ..pipeline import funnel
    from ..pipeline.check_stage import StageOptions, StageRun, TaskClients
    from ..pipeline.rows import index_of
    from ..pipeline.tasktext import TaskText
    from ..registry.registry import EmbodimentRegistry

    from ..contracts import modules as registry

    eef_mods = [m for m in modules if "eef_input" in registry.get(m).needs]   # D49: the EEF gate
    has_task = "task_success" in modules
    # registry 5.3 (design doc 23 §2.1): the CPU half alone (vlm_prep) asks nobody; the model half reads what it kept
    cpu_half, model_half = getattr(args, "prep", False), getattr(args, "prepared", False)
    if has_task and getattr(args, "no_vlm", False):
        raise UsageError("--no-vlm: task_success needs a model; the flag is for a stage whose modules can go without one")
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
    instructions: dict[int, str] = {}
    if has_task and not src.container:   # mcap / lance: each row brings its own
        instructions = {index_of(r["episode_id"]): str(r.get("instruction") or "")
                        for r in runctx.meta_rows(src, episodes, args,
                                                  what="check:task_success")}
    if prepared is None:
        task_text = TaskText(run_dir, instructions)
        judge = None
        if eef_mods:
            from .eef_check import EefJudge

            judge = EefJudge(ctx, args, run_dir, src, cfg, gates)
            judge.mode = "prep" if cpu_half else "ask" if model_half else "full"
        # design doc 25 D84: the EEF module asks a model only when 「使用 VLM 辅助」 is on and there is one (--no-vlm:
        # the task has none); alone in the stage without one, the stage opens no session (its model channel is
        # missing, said why)
        endpoint = bool((cfg["checks"]["task_success"]["vlm"] or {}).get("endpoint")) \
            and not getattr(args, "no_vlm", False)
        if judge is not None and not (judge.use_vlm and endpoint):
            from ..extensions.eef_consistency import combine as CB

            judge.vlm_missing = CB.VLM_OFF if not judge.use_vlm else CB.NO_BACKEND
        model_needed = (has_task or (judge is not None and judge.vlm_missing is None)) and not cpu_half
        session = runctx.VlmSession(ctx, args, cfg, "task_success" if has_task else eef_mods[0], run_dir,
                                    by_tag={"eef_review": eef_mods[0]} if eef_mods else None) if model_needed else None
        if session is not None:
            session.__enter__()
        try:
            if judge is not None:
                judge.open()
            from ..adapters.vlm_client import camera_check_from_config, vlm_completion_from_config

            # D71 / D73: the judgement answers for every camera in its one request; there is no
            # review client and no label guard (the slots stay for v1's shape of TaskClients).
            # An episode without a task text gets the picture-defect request instead.
            vlm_completion = vlm_completion_from_config(cfg) if has_task else None
            cameras = camera_check_from_config(cfg) if has_task else None
        except BaseException:
            if session is not None:
                session.__exit__(*sys.exc_info())
            raise
        clients = TaskClients(vlm_completion, None, None, cameras=cameras)
        if cache is not None:
            cache["vlm"] = {"gates": gates, "cfg": cfg, "task_text": task_text,
                            "session": session, "clients": clients, "eef": judge}
    else:
        task_text = prepared["task_text"]
        task_text.instructions.update(instructions)
        clients = prepared["clients"]
        judge = prepared.get("eef")
        if judge is not None:
            judge.rebind(src)            # this batch's source (mcap: its local copy)
    opts = StageOptions(run_dir=run_dir, input_dir=src.input_dir, modules=modules,
                        params=modparams.parse(getattr(args, "param", None)),
                        episodes=episodes, part=part, cfg=cfg, resume=args.resume,
                        concurrency=int(gates["episode"]),
                        embodiment_id=args.embodiment_id, max_episodes=args.max_episodes,
                        task_clients=clients, task_text=task_text,
                        evidence_mode=str(cfg.get("pipeline", {})
                                          .get("evidence_frames", "flagged")),
                        verify_source=guard, pipeline_state=args.pipeline_state,
                        pipeline_next=args.pipeline_next,
                        episode_stream=getattr(args, "_episode_stream", None),
                        eef=judge, stale={judge.module: judge.stale(episodes)} if judge is not None else None,
                        prep=cpu_half,
                        prepared=judge.kept if cpu_half and judge is not None and judge.vlm_missing is None else None,
                        episode_blocks=_episode_blocks(src),
                        **_container_options(args, src))
    stage = StageRun(ctx, opts, EmbodimentRegistry())
    try:
        payload = stage.run()
    finally:
        if cache is None:
            if session is not None:
                session.__exit__(*sys.exc_info())
            if judge is not None:
                judge.close()
    if judge is not None and judge.module in payload.get("modules", {}):
        # the file, seeds, template, configuration and model are part of the module's input
        payload["modules"][judge.module]["input_digest"] = judge.input_digest(list(opts.episodes))
    return payload, stage.survivors()


def render(payload: dict) -> str:
    lines = []
    for m, entry in payload["modules"].items():
        c = entry["episodes"]
        if "ok" in c:                                   # C2 2.0: judged / not, and the findings
            found = ", ".join(f"{code} {n}" for code, n in (entry.get("findings") or {}).items())
            lines.append(f"{m} (part {entry['part']}): {c['total']} episodes - ok {c['ok']}, "
                         f"error {c['error']}" + (f"; findings: {found}" if found else ""))
        else:
            lines.append(f"{m} (part {entry['part']}): {c['total']} episodes - pass {c['pass']}, "
                         f"fail {c['fail']}, abstain {c['abstain']}, scored {c['scored']}, "
                         f"error {c['error']}")
        if entry["error_episodes"]:
            shown = ", ".join(str(e) for e in entry["error_episodes"][:10])
            more = ", ..." if len(entry["error_episodes"]) > 10 else ""
            lines.append(f"  errors (held until retried): {shown}{more}")
    return "\n".join(lines)
