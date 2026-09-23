"""``check --modules eef_video_consistency`` - the advisory EEF-video consistency runner (design doc 12 §11).

Runs on every episode it is given (``input_scope=all_selected``: the Daemon and ``plan`` hand it the
selection, not the funnel's survivors) and never filters: ``--survivors-out`` lists them all. One
result-record line per episode with ``passed = score = null`` and ``gate = none``; the sub-item
statuses, coverage, segments and diagnosis go into ``details`` (the per-episode ``detail`` payload of
design doc 12 §11.3). Observations, per-frame curves and evidence overlays are written under
``checks/eef_video_consistency/{observations,curves,evidence}/<episode>/``. An episode the file does
not declare gets a line saying ``unsupported: projection_missing``; an episode the module fails on
gets an error line - neither changes keep / drop / held, which never read this module.
"""
from __future__ import annotations

import os
import time

from .errors import ModuleFailed, UsageError

MODULE = "eef_video_consistency"
REVIEW = "eef_video_review"
MOUNTS = {"fixed_external_and_wrist": ("fixed_external", "wrist"), "fixed_external": ("fixed_external",)}


def _unsupported_detail(episode: int, reason: str) -> dict:
    from ..extensions.eef_consistency import contracts as C

    subitems = {k: {"status": C.UNSUPPORTED, "reasons": [reason]} for k in C.SUBITEMS if k != C.VLM_REVIEW}
    return {"schema_version": C.DETAIL_SCHEMA_VERSION, "module_version": C.MODULE_VERSION,
            "assessment_mode": "advisory", "episode_index": episode, "overall": "not_assessable",
            "reasons": [reason], "summary": {k: {"status": v["status"], "cameras_assessable": 0,
                                                 "cameras": 0, "suspect_cameras": []} for k, v in subitems.items()},
            "cameras": {}, "segments": [], "diagnosis": [], "evidence": []}


def run(ctx, args, modules, run_dir: str, storage, episodes: list[int], part: str, guard) -> tuple[dict, list[int]]:
    from ..contracts import modules as registry
    from ..extensions.eef_consistency import load, profile, runner
    from ..extensions.eef_consistency.preflight import seed_dir
    from ..pipeline.check_stage import input_digest
    from ..pipeline.records import (Inflight, PartWriter, compact, derive_verdict, latest_results,
                                    module_dir)
    from . import modparams

    if REVIEW in modules:
        raise ModuleFailed(f"{REVIEW}: the VLM review is not part of the DEMO's first cut; "
                           f"preflight marks it unsupported", {"module": REVIEW})
    params = modparams.with_defaults(MODULE, modparams.parse(getattr(args, "param", None)).get(MODULE))
    try:
        registry.validate_params(MODULE, params)
    except Exception as e:  # noqa: BLE001 - jsonschema's message names the field
        raise UsageError(f"{MODULE}: {getattr(e, 'message', e)} (pass --param {MODULE}.trajectory_json=PATH)") \
            from None
    if storage.remote:
        raise ModuleFailed(f"{MODULE}: the DEMO reads a local LeRobot directory; {storage.uri} is remote",
                           {"input": storage.uri})
    traj = os.path.expanduser(params["trajectory_json"])
    if not os.path.isfile(traj):
        raise UsageError(f"{MODULE}: trajectory.json not found: {traj}")
    result = load.load_bundle(traj, lerobot_root=storage.root, episodes=episodes)
    if not result.ok:
        first = result.errors[0]
        raise ModuleFailed(f"{MODULE}: trajectory.json is invalid: {first.message}",
                           {"errors": [i.as_dict() for i in result.errors[:10]], "sha256": result.sha256})
    lag = float(params["lag_search_s"])
    out_dir = module_dir(run_dir, MODULE)
    cfg = runner.RunConfig(lerobot_root=storage.root, seed_root=seed_dir(params),
                           profile=profile.load(params["threshold_profile"]), out_dir=out_dir,
                           evidence_mode=params["evidence_mode"], allowed_mounts=MOUNTS[params["camera_mounts"]],
                           lag_search_s=(-lag, lag), interpolation_gap_factor=float(params["interpolation_gap_factor"]))
    todo = list(episodes)
    skipped = 0
    if args.resume:
        done = latest_results(run_dir, MODULE)
        todo = [e for e in episodes if not (e in done and done[e]["verdict"] != "error")]
        skipped = len(episodes) - len(todo)
    if guard is not None and todo:
        guard(todo)
    ctx.log("info", f"{MODULE}: trajectory.json sha256 {result.sha256[:12]}, {len(result.samples)} of "
                    f"{len(episodes)} episode(s) declared, profile {params['threshold_profile']}, "
                    f"seeds {cfg.seed_root or 'none'}")
    writer = PartWriter(run_dir, [MODULE], part)
    inflight = Inflight(run_dir, [MODULE], part)
    total = len(episodes)
    done_n = skipped
    drained = False
    try:
        ctx.progress(f"check:{MODULE}", done_n, total)
        for ep in todo:
            ctx.check_stop(f"{MODULE}: {done_n}/{total} episodes done")
            inflight.add(ep)
            t0 = time.perf_counter()
            error = None
            evidence: list[str] = []
            sample = result.samples.get(ep)
            if sample is None:
                detail = _unsupported_detail(ep, "projection_missing")
            else:
                try:
                    detail, _ = runner.run_episode(sample, cfg)
                    evidence = [os.path.relpath(os.path.join(out_dir, e["path"]), run_dir).replace(os.sep, "/")
                                for e in detail.get("evidence", [])]
                except Exception as e:  # noqa: BLE001 - one episode failing never stops the call
                    detail = _unsupported_detail(ep, "execution_failed")
                    detail["overall"] = "error"
                    error = {"kind": "execution",
                             "incidents": [{"step": MODULE, "cause": f"{type(e).__name__}: {e}"[:500]}]}
                    ctx.log("warn", f"{MODULE}: episode {ep} failed: {type(e).__name__}: {e}")
            detail["input_file_sha256"] = result.sha256
            record = {"episode_index": int(ep), "module": MODULE, "verdict": derive_verdict(None, None, error),
                      "passed": None, "score": None, "gate": registry.get(MODULE).gate, "details": detail,
                      "evidence": evidence, "elapsed_s": round(time.perf_counter() - t0, 3), "error": error}
            writer.write(record)
            inflight.remove(ep)
            done_n += 1
            ctx.progress(f"check:{MODULE}", done_n, total, episode_index=ep)
        drained = True
    finally:
        writer.close()
        if drained:
            inflight.clear()
        compact(run_dir, MODULE)
    cur = latest_results(run_dir, MODULE)
    counts = {"total": len(episodes), "pass": 0, "fail": 0, "abstain": 0, "scored": 0, "error": 0}
    errors = []
    for e in episodes:
        verdict = cur[e]["verdict"] if e in cur else "error"
        counts[verdict] += 1
        if verdict == "error":
            errors.append(e)
    entry = {"part": part, "input_digest": input_digest(episodes), "episodes": counts, "error_episodes": errors}
    if args.resume:
        entry["skipped_existing"] = skipped
    return {"schema_version": "1.0", "modules": {MODULE: entry}}, list(episodes)
