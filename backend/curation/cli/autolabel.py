"""``curation autolabel`` - a task text for episodes without one (design doc 02 §3.4).

v1's pre-funnel caption (``pipeline/run.py``): an episode without a task
annotation cannot be judged for task success, so the model first writes one
sentence about what the robot attempts. Frame sampling and the prompt are v1's
(``dataset_level/caption.py``), unchanged. Episodes that do have an annotation
are skipped; pass the whole selection, the command picks the unlabeled ones.

Writes ``<run-dir>/autolabel/captions.jsonl``, one line per episode
(``cli/autolabel-line.schema.json``): ``ok``, ``unclear`` (the model's honest
"cannot tell"; v1 judges such an episode with an empty task text, and so does
v2) or ``error`` (the call failed after every retry or a camera did not decode,
D33: the episode is held, task_success does not judge it).
"""
from __future__ import annotations

import argparse

from . import runctx
from .framework import Context, Result


def add_parser(sub, parents) -> None:
    p = sub.add_parser(
        "autolabel", parents=parents,
        help="caption the episodes that have no task text (VLM)",
        description="Write one task sentence for every selected episode without a task "
                    "annotation, as v1 does before the funnel.")
    runctx.add_source(p)
    runctx.add_run_dir(p)
    runctx.add_episodes(p)
    p.add_argument("--resume", action="store_true",
                   help="skip episodes that already have a caption that is not an error")
    p.add_argument("--plan-stage", metavar="FILE", help="the autolabel stage of plan.json")
    runctx.add_vlm(p)
    runctx.add_behaviour(p)
    p.set_defaults(func=run)


def run(ctx: Context, args: argparse.Namespace) -> Result:
    from ..adapters import vlm_client
    from ..dataset_level.caption import make_vlm_captioner
    from ..pipeline.dataset_stages import run_autolabel
    from ..pipeline.rows import index_of, meta_rows

    run_dir = runctx.run_dir_of(args, create=True)
    plan_stage = runctx.load_plan_stage(args.plan_stage, [], stage_id="autolabel")
    storage = runctx.open_input(ctx, args)
    available, _info = runctx.dataset_episodes(ctx, storage)
    episodes, warning = runctx.resolve_episodes(args, available)
    if warning:
        ctx.log("warn", warning)
    input_dir = storage.root if not storage.remote else storage.uri
    rows = sorted(meta_rows(input_dir, episodes, embodiment_id=args.embodiment_id,
                            max_episodes=args.max_episodes),
                  key=lambda r: index_of(r["episode_id"]))
    unlabeled = [index_of(r["episode_id"]) for r in rows
                 if not (r.get("instruction") or "").strip()]
    guard = runctx.source_guard(ctx, args, storage)
    if guard is not None and unlabeled:
        guard(unlabeled)
    ctx.log("info", f"autolabel: {len(unlabeled)} of {len(rows)} episode(s) have no task text")
    gates = runctx.vlm_gates(args, plan_stage)
    cfg = runctx.stage_config(ctx, ["task_success"], gates=gates, args=args)
    n_frames = int((cfg.get("skill_profile") or {}).get("n_frames", 8))
    if not unlabeled:
        payload = run_autolabel(ctx, run_dir, [], None, n_frames=n_frames, concurrency=1,
                                resume=args.resume)
        return Result(payload, human=render(payload))
    with runctx.VlmSession(ctx, args, cfg, "autolabel", run_dir):
        v = cfg["checks"]["task_success"]["vlm"]
        captioner = make_vlm_captioner(v["endpoint"], v["model"],
                                       timeout_s=vlm_client.timeout_for("caption", v),
                                       api_key_env=v.get("api_key_env"),
                                       max_in_flight=int(gates["caption"]))
        payload = run_autolabel(ctx, run_dir, rows, captioner, n_frames=n_frames,
                                concurrency=int(gates["caption"]), resume=args.resume)
    return Result(payload, human=render(payload))


def render(payload: dict) -> str:
    c = payload["counts"]
    text = (f"autolabel: {c['total']} unlabeled episode(s) - {c['ok']} captioned, "
            f"{c['unclear']} unclear, {c['error']} error; {payload['captions_file']}")
    if payload["error_episodes"]:
        text += "\n  errors (held until retried): " + ", ".join(
            str(e) for e in payload["error_episodes"][:10])
    return text
