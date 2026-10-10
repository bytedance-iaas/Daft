"""The review step of ``check --modules eef_video_consistency`` (design doc 12 §10, D-E12, D-E14).

Since D49 the review is part of the EEF module: for every episode ``eef_check.run`` measures with the
CPU first, then ``review_episode`` asks the model about the episode's windows (``review.py``: one point
and at most one axis each) and returns the windows with their answers or why there is none, and
``combine`` merges them with the CPU (design doc 25 §7). Requests go through the shared VLM client
(``vlm_client.hedged_request`` and ``requests.post`` looked up at call time), so the transport policy
(retry, hedging, reasoning effort), usage booking under call kind ``eef_review`` and the parity tape
work as for every other model call. Answers are cached under ``checks/eef_video_consistency/cache/``.
"""
from __future__ import annotations

import os

MODULE = BASE = "eef_video_consistency"
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
    video_options = dict(vlm.get("video") or {})

    def ask(req, history):
        from ..adapters.video_input import video_content

        if not req.videos:
            raise R.ReviewCallError("video_missing", "EEF review requires continuous videos")
        content = [{"type": "text", "text": req.text}] + video_content(
            req.videos, fps=float(video_options.get("fps", 5)))
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

    ask.media_input = "video"
    ask.video_options = video_options
    return ask


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


def plan_review(sample, base: dict, *, run_dir: str, media_root: str, model: str | None, per_camera: int,
                frames_per_window: int, video: bool = True, video_options: dict | None = None) -> dict:
    """The CPU half of the review (design doc 23 §2.1): every compared camera's windows chosen, their frames
    decoded and marked into requests, nobody asked. ``{"cameras": {id: row}, "requests": [(camera, window index,
    Request)], "truncated"}``: a camera with nothing to review has its final row; a window whose frames or video
    could not be had is a failed window. ``video``: continuous RAW / MARKED clips with the stills (what the
    configured asker sends, ``make_asker``)."""
    import numpy as np

    from ..extensions.eef_consistency import review as R
    from ..extensions.eef_consistency import runner
    from ..pipeline.records import module_dir

    bd = base.get("details") or {}
    cams: dict[str, dict] = {}
    requests: list = []
    truncated = False
    ep = f"{sample.episode_index:06d}"
    for cid, cam in (bd.get("cameras") or {}).items():
        points = list(cam.get("compared_points") or [])
        curves = os.path.join(module_dir(run_dir, BASE), "curves", ep, f"{cid}.parquet")
        if not points or not os.path.isfile(curves) or cid not in sample.cameras:
            cams[cid] = {"status": R.NOT_REVIEWED, "reasons": ["nothing_compared" if not points else "curves_missing"],
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
                                        frames_per_window=frames_per_window, axes=sample.axes)
        truncated |= cut
        need = {int(vf[f]): f for w in windows for f in w.frames if vf[f] >= 0}
        frames: dict[int, np.ndarray] = {}
        if need:
            for fr in runner._frames(sample, cid, media_root):
                if fr.index in need:
                    frames[need[fr.index]] = fr.bgr()
                if len(frames) == len(need):
                    break
        rows = []
        for w in windows:
            w.frames = [f for f in w.frames if f in frames]
            if not w.frames:
                rows.append({**w.as_dict(), "status": R.FAILED, "attempts": 0, "cache_hit": False,
                             "failure": {"code": "frames_unreadable", "message": "no frame of the window decoded"}})
                continue
            req = R.build_request(sample, w, frames, observed, model=str(model))
            if video:
                try:
                    R.attach_videos(req, sample, observed, media_root, options=video_options or {})
                except Exception as exc:
                    rows.append({**w.as_dict(), "status": R.FAILED, "attempts": 0, "cache_hit": False,
                                 "failure": {"code": "video_unreadable", "message": str(exc)[:300]}})
                    continue
            requests.append((cid, len(rows), req))
            rows.append(None)                       # the window's row once it is asked
        cams[cid] = {"windows": rows, "cut": cut}
    return {"cameras": cams, "requests": requests, "truncated": truncated, "episode": ep}


def answer_review(plan: dict, base: dict, *, run_dir: str, ask, cache, out_dir: str,
                  requests: dict | None = None) -> dict:
    """The model half of the review: each planned window asked (or its cached answer); the review ``detail``.

    The marked crops of a window the model refuted or the CPU disagrees with are written at
    once (the window's ``evidence``); ``requests`` (filled when given) keeps every request
    by its key with its evidence directory, so the verdict can write the others' crops for
    a person's card (:func:`write_card_evidence`)."""
    from ..extensions.eef_consistency import review as R

    bd = base.get("details") or {}
    cams = {cid: dict(row, windows=list(row.get("windows") or [])) for cid, row in plan["cameras"].items()}
    conflicts: list[dict] = []
    evidence: list[str] = []
    for cid, i, req in plan["requests"]:
        w = req.window
        got = R.ask_window(req, ask, cache)
        row = {**w.as_dict(), **got, "request_key": req.key}
        if req.videos:
            row["input_mode"] = "video"
            row["video_inputs"] = [clip.metadata() for clip in req.videos]
            req.videos.clear()   # reports retain still evidence, not every video's Base64
        where = os.path.join(out_dir, "evidence", plan["episode"], cid)
        if requests is not None:
            requests[req.key] = (req, where)
        if got["status"] == R.ANSWERED:
            c = R.conflict(w, got["answer"], ((bd.get("cameras") or {}).get(cid) or {}).get("subitems") or {})
            if c:
                row["conflict"] = c
                conflicts.append({"camera_id": cid, "frames": w.frames, **c})
            if c or got["answer"]["review_status"] == "refute" \
                    or got["answer"]["tracking_target_correct"] == "refute":
                row["evidence"] = R.write_evidence(req, where, run_dir)
                evidence += row["evidence"]
        cams[cid]["windows"][i] = row
    rows: list[dict] = []
    out: dict[str, dict] = {}
    for cid, cam in cams.items():
        if "cut" not in cam:                         # nothing to review on this camera
            out[cid] = cam
            continue
        windows = [r for r in cam["windows"] if r is not None]
        rows += windows
        out[cid] = {"status": R.summarize(windows, cam["cut"])[0], "windows": windows}
    status, summary = R.summarize(rows, plan["truncated"])
    reasons = [] if rows else ["nothing_to_review"]
    return {"schema_version": R.DETAIL_SCHEMA_VERSION, "status": status, "reasons": reasons, "summary": summary,
            "cameras": out, "conflicts": conflicts,
            "tracking_suspect": [dict(camera_id=r["camera_id"], frames=r["frames"]) for r in rows
                                 if r["status"] == R.ANSWERED and r["answer"]["tracking_target_correct"] == "refute"],
            "evidence": evidence}


def review_episode(sample, base: dict, *, run_dir: str, media_root: str, ask, cache, model: str,
                   per_camera: int, frames_per_window: int, out_dir: str,
                   requests: dict | None = None) -> dict:
    """The review ``detail`` of one episode whose CPU record is ``base`` - both halves in one call
    (:func:`plan_review`, then :func:`answer_review`)."""
    plan = plan_review(sample, base, run_dir=run_dir, media_root=media_root, model=model, per_camera=per_camera,
                       frames_per_window=frames_per_window, video=getattr(ask, "media_input", None) == "video",
                       video_options=getattr(ask, "video_options", {}))
    return answer_review(plan, base, run_dir=run_dir, ask=ask, cache=cache, out_dir=out_dir, requests=requests)


def write_card_evidence(review: dict, requests: dict, run_dir: str) -> list[str]:
    """The marked crops of every window of an episode that goes to a person that has none yet
    (design doc 12 D-E13: the card shows what the model was shown); the paths, also added to
    each window's ``evidence`` and the review's."""
    from ..extensions.eef_consistency import review as R

    out: list[str] = []
    for cam in (review.get("cameras") or {}).values():
        for row in cam.get("windows") or []:
            hit = requests.get(row.get("request_key"))
            if hit is None or row.get("evidence"):
                continue
            row["evidence"] = R.write_evidence(hit[0], hit[1], run_dir)
            out += row["evidence"]
    review["evidence"] = list(review.get("evidence") or []) + out
    return out
