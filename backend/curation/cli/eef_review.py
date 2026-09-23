"""``check --modules eef_video_review`` - the VLM review of EEF-video consistency (design doc 12 §10, F5.6).

Advisory like the module it reviews: it runs on every selected episode, never filters and writes one
result-record line per episode with ``passed = score = null``. It reads what ``eef_video_consistency``
found in the same run directory (its records and per-frame curves) and the same trajectory.json
(``--param eef_video_consistency.trajectory_json=PATH``, which the Daemon passes along with the
review's own parameters), asks the model about uniform and candidate windows (``review.py``) and
records per window the answer or why there is none, conflicts with the CPU and the episode status:
``completed``, ``incomplete`` (some window failed: timeout, malformed answer, unknown frame, a
measured value) or ``not_reviewed`` (nothing to review, the CPU result is missing or stale). None of
it touches keep / drop / held.

Requests go through the shared VLM client (``vlm_client.hedged_request`` and ``requests.post``
looked up at call time), so the transport policy (retry, hedging, reasoning effort), usage booking
under call kind ``eef_review`` and the parity tape work as for every other model call. Answers are
cached under ``checks/eef_video_review/cache/``; ``--resume`` redoes a line made with another file,
another configuration or another CPU result.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

from .errors import ModuleFailed, UsageError

MODULE = "eef_video_review"
BASE = "eef_video_consistency"
TAG = "eef_review"                     # the call kind (C3 1.1: module-id form)
DEFAULT_TIMEOUT_S = 120.0
MAX_TOKENS = 800


def _code(exc: Exception) -> str:
    import requests

    cause = str((getattr(exc, "curation_failure", None) or {}).get("cause") or "")
    if "timeout" in cause.lower() or isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"http_{exc.response.status_code}"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        return "bad_response"
    return cause or "call_failed"


def make_asker(vlm: dict, timeout_s: float, gate):
    """``ask(request, history) -> text`` over the configured endpoint; failures raise
    ``ReviewCallError`` with a short code."""
    import requests

    from ..adapters import vlm_client
    from ..extensions.eef_consistency import review as R

    url = str(vlm["endpoint"]).rstrip("/") + "/chat/completions"
    headers = vlm_client.auth_headers(vlm.get("api_key_env"))
    model = str(vlm["model"])

    def ask(req, history):
        content = [{"type": "text", "text": req.text}] + [
            {"type": "image_url", "image_url": {"url": R.data_url(i["jpeg"])}} for i in req.images]
        payload = {"model": model, "temperature": 0.0, "max_tokens": MAX_TOKENS,
                   "messages": [{"role": "user", "content": content}, *history]}
        try:
            r = vlm_client.hedged_request(
                lambda hard: requests.post(url, json=payload, headers=headers, timeout=hard),
                tag=TAG, timeout_s=timeout_s, gate=gate)
            r.raise_for_status()
            return vlm_client.strip_reasoning(str(r.json()["choices"][0]["message"]["content"]))
        except Exception as e:  # noqa: BLE001 - every failure is a window without an answer
            raise R.ReviewCallError(_code(e), f"{type(e).__name__}: {e}"[:300]) from None

    return ask


def _not_reviewed(ep: int, reason: str, **extra) -> dict:
    from ..extensions.eef_consistency import contracts as C
    from ..extensions.eef_consistency import review as R

    return {"schema_version": R.DETAIL_SCHEMA_VERSION, "module_version": C.MODULE_VERSION,
            "assessment_mode": "advisory", "episode_index": ep, "status": R.NOT_REVIEWED, "reasons": [reason],
            "summary": R.summarize([], False)[1], "cameras": {}, "conflicts": [], "needs_human": False,
            "evidence": [], **extra}


def _curves(path: str, points: list[str]):
    import numpy as np
    import pandas as pd

    df = pd.read_parquet(path)
    declared, observed = {}, {}
    for pid in points:
        if f"decl_u:{pid}" in df:
            declared[pid] = np.stack([df[f"decl_u:{pid}"].to_numpy(float), df[f"decl_v:{pid}"].to_numpy(float)], 1)
        if f"obs_u:{pid}" in df:
            observed[pid] = np.stack([df[f"obs_u:{pid}"].to_numpy(float), df[f"obs_v:{pid}"].to_numpy(float)], 1)
    return declared, observed


def review_episode(sample, base: dict, *, run_dir: str, media_root: str, ask, cache, model: str,
                   per_camera: int, frames_per_window: int, out_dir: str) -> dict:
    """The review ``detail`` of one episode whose CPU record is ``base``."""
    import numpy as np

    from ..extensions.eef_consistency import contracts as C
    from ..extensions.eef_consistency import review as R
    from ..extensions.eef_consistency import runner
    from ..pipeline.records import module_dir

    bd = base.get("details") or {}
    cams_out: dict[str, dict] = {}
    rows: list[dict] = []
    conflicts: list[dict] = []
    evidence: list[str] = []
    truncated = False
    ep = f"{sample.episode_index:06d}"
    for cid, cam in (bd.get("cameras") or {}).items():
        points = list(cam.get("compared_points") or [])
        curves = os.path.join(module_dir(run_dir, BASE), "curves", ep, f"{cid}.parquet")
        if not points or not os.path.isfile(curves) or cid not in sample.cameras:
            cams_out[cid] = {"status": R.NOT_REVIEWED, "reasons": ["nothing_compared" if not points else "curves_missing"],
                             "windows": []}
            continue
        declared, observed = _curves(curves, points)
        vf = sample.cameras[cid].video_frame_index
        shown = np.zeros(sample.n_frames, bool)
        for uv in declared.values():
            shown |= np.isfinite(uv).all(1)
        reviewable = shown & (vf >= 0)
        fps = float(sample.cameras[cid].media.get("fps") or 15.0)
        windows, cut = R.select_windows(bd, cid, reviewable, fps, per_camera=per_camera,
                                        frames_per_window=frames_per_window)
        truncated |= cut
        need = {int(vf[f]): f for w in windows for f in w.frames if vf[f] >= 0}
        frames: dict[int, np.ndarray] = {}
        if need:
            for fr in runner._frames(sample, cid, media_root):
                if fr.index in need:
                    frames[need[fr.index]] = fr.bgr()
                if len(frames) == len(need):
                    break
        out = []
        for w in windows:
            w.frames = [f for f in w.frames if f in frames]
            if not w.frames:
                out.append({**w.as_dict(), "status": R.FAILED, "attempts": 0, "cache_hit": False,
                            "failure": {"code": "frames_unreadable", "message": "no frame of the window decoded"}})
                continue
            req = R.build_request(sample, w, frames, declared, observed, model=model)
            got = R.ask_window(req, ask, cache)
            row = {**w.as_dict(), **got, "request_key": req.key}
            if got["status"] == R.ANSWERED:
                c = R.conflict(w, got["answer"], cam.get("subitems") or {})
                if c:
                    row["conflict"] = c
                    conflicts.append({"camera_id": cid, "frames": w.frames, **c})
                if c or got["answer"]["review_status"] == "refute" \
                        or got["answer"]["tracking_target_correct"] == "refute":
                    evidence += R.write_evidence(req, os.path.join(out_dir, "evidence", ep, cid), run_dir)
            out.append(row)
        rows += out
        cams_out[cid] = {"status": R.summarize(out, cut)[0], "windows": out}
    status, summary = R.summarize(rows, truncated)
    reasons = [] if rows else ["nothing_to_review"]
    return {"schema_version": R.DETAIL_SCHEMA_VERSION, "module_version": C.MODULE_VERSION,
            "assessment_mode": "advisory", "episode_index": sample.episode_index, "sample_id": sample.sample_id,
            "status": status, "reasons": reasons, "summary": summary, "cameras": cams_out,
            "conflicts": conflicts, "needs_human": bool(conflicts),
            "tracking_suspect": [dict(camera_id=r["camera_id"], frames=r["frames"]) for r in rows
                                 if r["status"] == R.ANSWERED and r["answer"]["tracking_target_correct"] == "refute"],
            "evidence": evidence}


def run(ctx, args, modules, run_dir: str, storage, episodes: list[int], part: str, guard,
        plan_stage=None) -> tuple[dict, list[int]]:
    from ..adapters.vlm_client import SharedGate
    from ..contracts import modules as registry
    from ..extensions.eef_consistency import load
    from ..extensions.eef_consistency import review as R
    from ..pipeline.check_stage import input_digest
    from ..pipeline.records import (Inflight, PartWriter, compact, derive_verdict, latest_results,
                                    module_dir)
    from . import eef_check, modparams, runctx

    parsed = modparams.parse(getattr(args, "param", None))
    base_params = modparams.with_defaults(BASE, parsed.get(BASE))
    params = modparams.with_defaults(MODULE, parsed.get(MODULE))
    try:
        registry.validate_params(BASE, base_params)
        registry.validate_params(MODULE, params)
    except Exception as e:  # noqa: BLE001 - jsonschema's message names the field
        raise UsageError(f"{MODULE}: {getattr(e, 'message', e)} (the review reads the file of the module it "
                         f"reviews: pass --param {BASE}.trajectory_json=PATH)") from None
    traj = os.path.expanduser(base_params["trajectory_json"])
    if not os.path.isfile(traj):
        raise UsageError(f"{MODULE}: trajectory.json not found: {traj}")
    media_exists = (lambda key: storage.stat(key) is not None) if storage.remote else None
    result = load.load_bundle(traj, lerobot_root=None if storage.remote else storage.root,
                              media_exists=media_exists, episodes=episodes)
    if not result.ok:
        raise ModuleFailed(f"{MODULE}: trajectory.json is invalid: {result.errors[0].message}",
                           {"errors": [i.as_dict() for i in result.errors[:10]], "sha256": result.sha256})
    gates = runctx.vlm_gates(args, plan_stage)
    cfg = runctx.stage_config(ctx, modules, gates=gates, args=args)
    vlm = cfg["checks"]["task_success"]["vlm"]
    timeout_s = float((vlm.get("timeouts_s") or {}).get(TAG) or DEFAULT_TIMEOUT_S)
    per_camera, per_window = int(params["review_windows_per_camera"]), int(params["review_frames_per_window"])
    out_dir = module_dir(run_dir, MODULE)
    base_done = latest_results(run_dir, BASE)
    todo = list(episodes)
    skipped = 0
    scratch = tempfile.TemporaryDirectory(prefix="eef-media-") if storage.remote else None
    writer = inflight = None
    drained = False
    try:
        with runctx.VlmSession(ctx, args, cfg, MODULE, run_dir):
            model = str(vlm["model"])
            review_config = hashlib.sha256(json.dumps(
                {"windows": per_camera, "frames": per_window, "model": model, "prompt": R.PROMPT_VERSION,
                 "schema": R.ANSWER_SCHEMA, "preprocess": R.PREPROCESS}, sort_keys=True).encode()).hexdigest()

            def base_hash(e: int):
                return ((base_done.get(e) or {}).get("details") or {}).get("config_hash")

            if args.resume:
                done = latest_results(run_dir, MODULE)

                def current(e: int) -> bool:
                    d = (done.get(e) or {}).get("details") or {}
                    return e in done and done[e]["verdict"] != "error" \
                        and d.get("input_file_sha256") == result.sha256 and d.get("review_config") == review_config \
                        and d.get("base_config_hash") == base_hash(e)

                todo = [e for e in episodes if not current(e)]
                skipped = len(episodes) - len(todo)
            if guard is not None and todo:
                guard(todo)
            ask = make_asker(vlm, timeout_s, SharedGate(max(1, int(gates.get("arbitration", 1)))))
            cache = R.Cache(os.path.join(out_dir, "cache"))
            ctx.log("info", f"{MODULE}: trajectory.json sha256 {result.sha256[:12]}, model {model}, "
                            f"{per_camera} window(s) of up to {per_window} frame(s) per camera and kind")
            writer = PartWriter(run_dir, [MODULE], part)
            inflight = Inflight(run_dir, [MODULE], part)
            total, done_n = len(episodes), skipped
            ctx.progress(f"check:{MODULE}", done_n, total)
            for ep in todo:
                ctx.check_stop(f"{MODULE}: {done_n}/{total} episodes done")
                inflight.add(ep)
                t0 = time.perf_counter()
                error = None
                evidence: list[str] = []
                sample = result.samples.get(ep)
                base = base_done.get(ep)
                if sample is None:
                    detail = _not_reviewed(ep, "projection_missing")
                elif base is None:
                    detail = _not_reviewed(ep, "base_missing")
                elif base["verdict"] == "error" or (base.get("details") or {}).get("overall") == "error":
                    detail = _not_reviewed(ep, "base_error")
                elif (base.get("details") or {}).get("input_file_sha256") != result.sha256:
                    detail = _not_reviewed(ep, "base_stale")
                else:
                    try:
                        if scratch is not None:
                            eef_check._fetch_media(storage, sample, scratch.name)
                        detail = review_episode(sample, base, run_dir=run_dir,
                                                media_root=scratch.name if scratch else storage.root, ask=ask,
                                                cache=cache, model=model, per_camera=per_camera,
                                                frames_per_window=per_window, out_dir=out_dir)
                        evidence = list(detail.get("evidence") or [])
                    except Exception as e:  # noqa: BLE001 - one episode failing never stops the call
                        detail = _not_reviewed(ep, "execution_failed")
                        error = {"kind": "execution",
                                 "incidents": [{"step": MODULE, "cause": f"{type(e).__name__}: {e}"[:500]}]}
                        ctx.log("warn", f"{MODULE}: episode {ep} failed: {type(e).__name__}: {e}")
                detail.update(input_file_sha256=result.sha256, review_config=review_config,
                              base_config_hash=base_hash(ep),
                              vlm={"model": model, "prompt_version": R.PROMPT_VERSION, "answer_schema": R.ANSWER_SCHEMA,
                                   "timeout_s": timeout_s, "call_kind": TAG})
                record = {"episode_index": int(ep), "module": MODULE, "verdict": derive_verdict(None, None, error),
                          "passed": None, "score": None, "gate": registry.get(MODULE).gate, "details": detail,
                          "evidence": evidence, "elapsed_s": round(time.perf_counter() - t0, 3), "error": error}
                writer.write(record)
                inflight.remove(ep)
                done_n += 1
                ctx.progress(f"check:{MODULE}", done_n, total, episode_index=ep)
            drained = True
    finally:
        if writer is not None:
            writer.close()
            if drained:
                inflight.clear()
            compact(run_dir, MODULE)
        if scratch is not None:
            scratch.cleanup()
    cur = latest_results(run_dir, MODULE)
    counts = {"total": len(episodes), "pass": 0, "fail": 0, "abstain": 0, "scored": 0, "error": 0}
    errors = []
    for e in episodes:
        verdict = cur[e]["verdict"] if e in cur else "error"
        counts[verdict] += 1
        if verdict == "error":
            errors.append(e)
    digest = hashlib.sha256(json.dumps({"episodes": input_digest(episodes), "trajectory": result.sha256,
                                        "config": review_config, "base": [base_hash(e) for e in episodes]},
                                       sort_keys=True).encode()).hexdigest()
    entry = {"part": part, "input_digest": f"sha256:{digest}", "episodes": counts, "error_episodes": errors}
    if args.resume:
        entry["skipped_existing"] = skipped
    return {"schema_version": "1.0", "modules": {MODULE: entry}}, list(episodes)
