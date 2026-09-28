"""The model's opinion when no gripper reference was given (design doc 12 §10.5, D-E15).

Without observation seeds or a gripper template nothing finds the gripper in the picture, so the CPU
measures nothing. Instead every camera's whole clip is drawn with the declared gripper centre P (red
circle) and direction A (red arrow), frame numbers printed, and sent to the model as one continuous
video (clips longer than ``MAX_CLIP_S`` are cut into consecutive parts, one request each). The model
lists the stretches where P is not on the gripper centre or A does not follow the gripper, each with
the confidence that it does NOT match and its evidence frames. The opinion is advisory: the record
passes and nobody is asked; the report and the Episode tab show it next to the verdict.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
from typing import Callable

import numpy as np

from . import review as R

PROTOCOL = "eef-opinion/1"
PROMPT_VERSION = "eef-opinion-prompt/1"
ANSWER_SCHEMA = "eef/opinion_output.schema.json"
MAX_CLIP_S = 60.0                     # a longer clip is cut into consecutive parts of about this long
FLAG_CONFIDENCE = 0.5                 # a segment at least this sure counts as flagged in the report
STILL_MAX_SIDE = 960                  # evidence stills, full frame
ASPECTS = ("position", "orientation", "both")
NAMES = {"position": "中心", "orientation": "朝向", "both": "中心与朝向"}


def pick_point(sample, camera_id: str) -> str | None:
    """The gripper centre: ``tcp``, else a point described as a centre, else the first declared one."""
    from .load import declared_point_ids

    ids = declared_point_ids(sample, camera_id)
    if not ids:
        return None
    if "tcp" in ids:
        return "tcp"
    for pid in ids:
        meaning = str(((sample.points or {}).get(pid) or {}).get("meaning") or "").lower()
        if "center" in meaning or "centre" in meaning or "中心" in meaning:
            return pid
    return ids[0]


def pick_axis(sample, camera_id: str, frames: list[int]) -> str | None:
    """The gripper's pointing direction: ``z`` (approach) when long enough on screen, else the longest
    axis that is; None when no arrow would show a direction."""
    lengths = {a: R._axis_length(sample, camera_id, a, frames) for a in (sample.axes or {})}
    order = (["z"] if "z" in lengths else []) + sorted(lengths, key=lambda a: -lengths[a])
    return next((a for a in order if lengths[a] >= R.MIN_AXIS_PX), None)


def clip_ranges(frames: list[int], fps: float) -> list[list[int]]:
    """Sample frames cut into consecutive parts of at most ``MAX_CLIP_S`` seconds."""
    per = max(1, int(MAX_CLIP_S * fps))
    return [frames[i:i + per] for i in range(0, len(frames), per)]


def build_prompt(sample, camera_id: str, point_id: str, axis_id: str | None, lo: int, hi: int) -> str:
    window = R.Window(camera_id=camera_id, kind="uniform", frames=[lo, hi], point_id=point_id, axis_id=axis_id)
    marks = R.Marks(None, None, (np.zeros((1, 2)), np.zeros((1, 2))) if axis_id else None)
    legend = "the RED circle labelled P is where the recorded trajectory puts the gripper centre P"
    if axis_id:
        legend += "; the RED arrow labelled A is the recorded pointing direction A of the gripper"
    return "\n".join([
        "You check a recorded robot trajectory against a camera video. The video is continuous; on every frame "
        f"{legend}. The frame number is printed at the top left of every frame.",
        f"Camera {camera_id}. Frames {lo} to {hi}.",
        "Definitions:",
        R.point_text(sample, window, marks),
        "Watch the whole video. Find every stretch of frames where the red circle P is NOT on the gripper centre as "
        "defined" + (", or the red arrow A does NOT follow the way the gripper points" if axis_id else "") + ". "
        "A short, momentary drift and a clearly wrong stretch are both worth reporting; give each its own confidence.",
        "Answer with ONE JSON object and nothing else, exactly these keys:",
        '{"gripper_visible": true|false, "segments": [{"start_frame": int, "end_frame": int, '
        '"aspect": "position|orientation|both", "confidence": 0.0-1.0, "evidence_frames": [1 to 5 frame numbers], '
        '"observation": "what you see, one or two sentences in Chinese"}], "summary": "one sentence in Chinese"}',
        f"Frame numbers are the printed ones, between {lo} and {hi}; start_frame <= end_frame and every evidence "
        "frame lies inside its segment. confidence is how sure you are that the stretch does NOT match (1 = certainly "
        "wrong). aspect: position = P is off the gripper centre; orientation = A does not follow the gripper; both. "
        "If everything matches, return an empty segments list. If the gripper cannot be seen, set gripper_visible "
        "to false and report nothing you cannot see.",
    ])


def check_answer(text: str, lo: int, hi: int) -> tuple[dict | None, dict | None]:
    """(answer, None) or (None, {"code", "message"}): malformed_json | schema_violation | bad_frame."""
    from curation.contracts import schemas

    try:
        doc = json.loads(R._strip_fence(text), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (ValueError, TypeError) as e:
        return None, {"code": "malformed_json", "message": f"not one JSON object: {e}"[:300]}
    if not isinstance(doc, dict):
        return None, {"code": "malformed_json", "message": "not one JSON object"}
    errs = schemas.errors(ANSWER_SCHEMA, doc)
    if errs:
        return None, {"code": "schema_violation", "message": "; ".join(errs[:3])[:300]}
    for i, seg in enumerate(doc["segments"]):
        a, b = seg["start_frame"], seg["end_frame"]
        if not lo <= a <= b <= hi:
            return None, {"code": "bad_frame", "message": f"segment {i}: frames {a}-{b} are not within {lo}-{hi}"}
        outside = [f for f in seg["evidence_frames"] if not a <= f <= b]
        if outside:
            return None, {"code": "bad_frame", "message": f"segment {i}: evidence frames {outside} are not in {a}-{b}"}
    return doc, None


def repair_text(problem: dict, lo: int, hi: int) -> str:
    return (f"Your answer was rejected ({problem['code']}: {problem['message']}). Reply again with ONE JSON object "
            f"with exactly the keys asked for; frames between {lo} and {hi}, evidence frames inside their segment.")


def _render(sample, camera_id: str, marks: R.Marks, media_root: str, lo_media: int, hi_media: int,
            mapping: dict[int, int], max_side: int):
    """(PTS, RGB) of the marked frames lo_media..hi_media, frame numbers printed."""
    import cv2

    from .observations import view_frames

    for fr in view_frames(sample, camera_id, media_root):
        if fr.index < lo_media:
            continue
        if fr.index > hi_media:
            break
        bgr = fr.bgr()
        k = min(1.0, max_side / max(bgr.shape[:2]))
        if k < 1:
            bgr = cv2.resize(bgr, (int(bgr.shape[1] * k), int(bgr.shape[0] * k)))
        f = mapping.get(fr.index)
        if f is not None:
            bgr = R._overlay(bgr, marks, f, k)
        bgr = R._label(bgr, f"frame {f}" if f is not None else f"media frame {fr.index} (no trajectory sample)")
        yield fr.pts_s, cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def build_request(sample, camera_id: str, frames: list[int], point_id: str, axis_id: str | None, *,
                  media_root: str, model: str, options: dict | None = None) -> R.Request:
    """One part of a camera's clip: the prompt and the marked video; cached by what is sent."""
    from ...adapters.video_input import encode_rendered_video

    opts = options or {}
    cam = sample.cameras[camera_id]
    fps = float(cam.media.get("fps") or 0)
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("the opinion video needs the camera's frame rate")
    lo, hi = frames[0], frames[-1]
    window = R.Window(camera_id=camera_id, kind="opinion", frames=list(frames), point_id=point_id, axis_id=axis_id)
    marks = R.marks_for(sample, window, {})
    mapping = {int(v): i for i, v in enumerate(cam.video_frame_index) if v >= 0}
    lo_m, hi_m = int(cam.video_frame_index[lo]), int(cam.video_frame_index[hi])
    text = build_prompt(sample, camera_id, point_id, axis_id, lo, hi)
    clip = encode_rendered_video(f"{camera_id} MARKED", _render(sample, camera_id, marks, media_root, lo_m, hi_m,
                                                                mapping, int(opts.get("max_side", 720))),
                                 fps=fps, end_s=(hi_m + 1) / fps,
                                 max_bytes=int(opts.get("max_bytes", 32 * 1024 * 1024)))
    key = hashlib.sha256(json.dumps({"protocol": PROTOCOL, "prompt": PROMPT_VERSION, "text": text, "model": model,
                                     "fps": float(opts.get("fps", 5)), "video": clip.metadata()},
                                    sort_keys=True).encode()).hexdigest()
    return R.Request(window=window, text=text, images=[], frame_ids=[lo, hi], key=key, videos=[clip])


def ask_clip(req: R.Request, ask: Callable[[R.Request, list[dict]], str], cache: R.Cache) -> dict:
    """One part: cached answer, or the model with at most one repair turn."""
    lo, hi = req.frame_ids
    hit = cache.get(req.key)
    if hit is not None:
        return {"status": R.ANSWERED, "answer": hit, "attempts": 0, "cache_hit": True}
    history: list[dict] = []
    problem = None
    for attempt in (1, 2):
        try:
            text = ask(req, history)
        except R.ReviewCallError as e:
            return {"status": R.FAILED, "failure": {"code": e.code, "message": str(e)[:300]}, "attempts": attempt,
                    "cache_hit": False}
        answer, problem = check_answer(text, lo, hi)
        if answer is not None:
            cache.put(req.key, answer)
            return {"status": R.ANSWERED, "answer": answer, "attempts": attempt, "cache_hit": False}
        history = [{"role": "assistant", "content": text[:4000]},
                   {"role": "user", "content": repair_text(problem, lo, hi)}]
    return {"status": R.FAILED, "failure": problem, "attempts": 2, "cache_hit": False}


def write_stills(sample, camera_id: str, marks: R.Marks, wanted: set[int], *, media_root: str, directory: str,
                 run_dir: str) -> dict[int, str]:
    """The marked full frames the model cited, as JPEG; paths relative to the run directory."""
    import cv2

    from .observations import view_frames

    cam = sample.cameras[camera_id]
    media = {int(cam.video_frame_index[f]): f for f in wanted if cam.video_frame_index[f] >= 0}
    if not media:
        return {}
    d = pathlib.Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out: dict[int, str] = {}
    for fr in view_frames(sample, camera_id, media_root):
        f = media.get(fr.index)
        if f is not None:
            bgr = fr.bgr()
            k = min(1.0, STILL_MAX_SIDE / max(bgr.shape[:2]))
            if k < 1:
                bgr = cv2.resize(bgr, (int(bgr.shape[1] * k), int(bgr.shape[0] * k)))
            bgr = R._label(R._overlay(bgr, marks, f, k), f"frame {f}")
            path = d / f"frame_{f:06d}.jpg"
            path.write_bytes(R._jpeg(bgr))
            out[f] = os.path.relpath(path, run_dir).replace(os.sep, "/")
        if fr.index >= max(media):
            break
    return out


def _time(sample, camera_id: str, f: int) -> float | None:
    if sample.t is not None and np.isfinite(sample.t[f]):
        return round(float(sample.t[f]), 3)
    ts = sample.cameras[camera_id].video_timestamp_s
    return round(float(ts[f]), 3) if np.isfinite(ts[f]) else None


def opinion_episode(sample, *, media_root: str, ask, cache: R.Cache, model: str, out_dir: str, run_dir: str,
                    allowed_mounts, options: dict | None = None) -> dict:
    """Every participating camera's clip asked in parts; the segments with their evidence stills.
    ``{"status", "cameras": {id: {...}}, "segments", "flagged", "max_confidence", "requests"}``."""
    from .load import declared_track

    allowed = set(allowed_mounts)
    cams: dict[str, dict] = {}
    requests = 0
    for cid in sorted(sample.cameras):
        cam = sample.cameras[cid]
        if cam.mount not in allowed:
            cams[cid] = {"status": "skipped", "reason": f"mount {cam.mount} does not take part"}
            continue
        pid = pick_point(sample, cid)
        track = declared_track(sample, cid, pid) if pid else None
        if track is None:
            cams[cid] = {"status": "skipped", "reason": "no projection of the gripper centre on this camera"}
            continue
        frames = [i for i in range(sample.n_frames)
                  if cam.video_frame_index[i] >= 0 and np.isfinite(track.uv[i]).all()]
        if not frames:
            cams[cid] = {"status": "skipped", "reason": "the gripper centre is never projected into this camera"}
            continue
        aid = pick_axis(sample, cid, frames)
        fps = float(cam.media.get("fps") or 0)
        row: dict = {"status": "answered", "point_id": pid, "axis_id": aid, "clips": [], "segments": []}
        for part in clip_ranges(frames, fps if fps > 0 else 15.0):
            clip: dict = {"start_frame": part[0], "end_frame": part[-1]}
            try:
                req = build_request(sample, cid, part, pid, aid, media_root=media_root, model=model, options=options)
            except Exception as e:  # noqa: BLE001 - one part that cannot be rendered does not stop the others
                clip.update(status=R.FAILED, failure={"code": "video_unreadable", "message": str(e)[:300]})
                row["clips"].append(clip)
                continue
            clip["video"] = req.videos[0].metadata()
            got = ask_clip(req, ask, cache)
            requests += 0 if got.get("cache_hit") else got.get("attempts", 0)
            req.videos.clear()                      # the record keeps metadata, never the video's Base64
            clip.update({k: got[k] for k in ("status", "attempts", "cache_hit") if k in got})
            if got["status"] != R.ANSWERED:
                clip["failure"] = got.get("failure")
                row["clips"].append(clip)
                continue
            a = got["answer"]
            clip.update(gripper_visible=a["gripper_visible"], summary=a["summary"])
            row["clips"].append(clip)
            for seg in a["segments"]:
                row["segments"].append({
                    "start_frame": seg["start_frame"], "end_frame": seg["end_frame"],
                    "start_s": _time(sample, cid, seg["start_frame"]), "end_s": _time(sample, cid, seg["end_frame"]),
                    "aspect": seg["aspect"], "confidence": round(float(seg["confidence"]), 3),
                    "evidence_frames": list(seg["evidence_frames"]), "observation": seg["observation"]})
        answered = [c for c in row["clips"] if c.get("status") == R.ANSWERED]
        row["status"] = "answered" if len(answered) == len(row["clips"]) else "partial" if answered else "failed"
        if row["segments"]:
            window = R.Window(camera_id=cid, kind="opinion", frames=frames, point_id=pid, axis_id=aid)
            stills = write_stills(sample, cid, R.marks_for(sample, window, {}),
                                  {f for s in row["segments"] for f in s["evidence_frames"]}, media_root=media_root,
                                  directory=os.path.join(out_dir, "opinion", f"ep_{sample.episode_index:06d}", cid),
                                  run_dir=run_dir)
            for s in row["segments"]:
                s["evidence"] = [stills[f] for f in s["evidence_frames"] if f in stills]
        cams[cid] = row
    asked = [c for c in cams.values() if c.get("status") != "skipped"]
    segments = [s for c in asked for s in c.get("segments") or []]
    status = ("not_assessable" if not asked else
              "answered" if all(c["status"] == "answered" for c in asked) else
              "failed" if all(c["status"] == "failed" for c in asked) else "partial")
    top = max((s["confidence"] for s in segments), default=None)
    return {"protocol": PROTOCOL, "prompt_version": PROMPT_VERSION, "status": status, "cameras": cams,
            "segments": len(segments), "flagged": bool(top is not None and top >= FLAG_CONFIDENCE),
            "max_confidence": top, "requests": requests}


def evidence_paths(opinion: dict) -> list[str]:
    return [p for c in (opinion.get("cameras") or {}).values() for s in c.get("segments") or []
            for p in s.get("evidence") or []]


def summary(results: dict) -> dict:
    """The report's opinion keys: episodes asked, flagged (a segment at least FLAG_CONFIDENCE sure),
    segments by aspect and confidence, failures."""
    n = {"asked": 0, "flagged": 0, "segments": 0, "failed": 0, "not_assessable": 0}
    aspects: dict[str, int] = {}
    buckets = {"<0.3": 0, "0.3–0.5": 0, "0.5–0.7": 0, "0.7–0.9": 0, "≥0.9": 0}
    for rec in results.values():
        d = rec.get("details") or {}
        if d.get("assessment_mode") != "vlm_opinion":
            continue
        op = d.get("opinion") or {}
        n["asked"] += 1
        n["flagged"] += bool(op.get("flagged"))
        n["failed"] += op.get("status") == "failed"
        n["not_assessable"] += op.get("status") == "not_assessable"
        for cam in (op.get("cameras") or {}).values():
            for s in cam.get("segments") or []:
                n["segments"] += 1
                aspects[s["aspect"]] = aspects.get(s["aspect"], 0) + 1
                c = s["confidence"]
                key = "<0.3" if c < 0.3 else "0.3–0.5" if c < 0.5 else "0.5–0.7" if c < 0.7 else \
                    "0.7–0.9" if c < 0.9 else "≥0.9"
                buckets[key] += 1
    if not n["asked"]:
        return {}
    return {"opinion_episodes": n["asked"], "opinion_flagged": n["flagged"], "opinion_segments": n["segments"],
            "opinion_failed": n["failed"], "opinion_not_assessable": n["not_assessable"],
            "opinion_aspects": [{"name": k, "count": v} for k, v in aspects.items() if v],
            "opinion_confidence": [{"name": k, "count": v} for k, v in buckets.items()]}
