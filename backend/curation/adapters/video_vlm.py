"""Video-native task assessment; shared by scoring, camera review and arbitration."""
from __future__ import annotations

import json
import math

from .video_input import VideoClip, video_content

PROTOCOL = "video-task/1"
#: the per-camera picture-defect report riding on every endstate review (camera_defects module)
CAMERA_CHECK_PROTOCOL = "camera-check/1"
CAMERA_CHECK_ITEMS = ("glitch", "shake", "contamination")
CAMERA_CHECK_LEVELS = ("none", "minor", "severe")
CONTAMINATION_KINDS = ("none", "dirt", "smudge", "water", "obstruction", "other")
CAMERA_CHECK_MAX_TIMES = 8
TASK_PROMPT = """Assess the robot manipulation task from the supplied continuous videos.
Task: {instruction}
Camera guidance: {hints}
All cameras show the same episode. Use visible OBJECT evidence and temporal
continuity, not merely plausible arm motions. Check small objects between the
gripper fingers and at the destination. Occlusion is not proof of failure.
For a transient goal (pick up/lift/take out), visible achievement at ANY time
counts, even if the object is later put down. For a persistent goal (put in/on,
open/close), inspect whether the requested resulting state was achieved and
maintained at the end. Do not demand anything beyond the literal instruction.
Do not invent progress curves or scores for unseen moments.
Return one JSON object, with exactly these fields:
{{"verdict":"success|failure|uncertain", "task_type":"transient|persistent",
  "completion":0.0, "reason":"visible evidence and limitations",
  "evidence":[{{"camera":"exact supplied camera name", "start_s":0.0,
               "end_s":1.0, "observation":"what is visibly happening"}}]}}
completion is an estimate between 0 and 1, not confidence. A definite success
or failure MUST cite at least one evidence interval. All evidence timestamps
must be episode-relative seconds within the supplied camera window. If the
objects or the relevant action cannot be observed, return uncertain.
"""
CAMERA_CHECK_PROMPT = """
Also report this camera's own picture defects in ONE extra field "camera_check" of the same JSON
object, next to the five fields above (keep those five exactly as specified):
{"camera_check":{
  "glitch":{"level":"none|minor|severe|unknown","times":[[start_s,end_s]],"note":"..."},
  "shake":{"level":"none|minor|severe|unknown","times":[[start_s,end_s]],"note":"..."},
  "contamination":{"level":"none|minor|severe|unknown",
                   "kind":"none|dirt|smudge|water|obstruction|other","times":[[start_s,end_s]],"note":"..."}}}
glitch: the picture CONTENT is damaged - corrupted, torn, blocky, smeared or garbled frames, wrong
colours in patches, or a frozen picture with artifacts. shake: the WHOLE picture moves as one while
the scene itself stays intact - the view jolts, drifts or wobbles, the framing jumps, and the edges
may show black bars or a changed field of view; the camera body is moving, not the robot or the
objects. A wrist camera moving with the arm is normal, report only jitter beyond that. Decide
between the two by asking whether the scene is still whole: a whole scene that jumps is shake, a
broken-looking scene is glitch; report both only when both are really there. contamination: dirt,
smudges, water drops or an object stuck to or covering the lens. "minor" is visible but leaves the
scene readable; "severe" hides or distorts the scene for part of the episode. times are
episode-relative seconds within this camera's window, like evidence; leave times empty for "none".
Give up to four intervals; when the defect recurs in short bursts across the clip, give the span
that encloses them rather than one burst, and say "intermittent" in the note. When the defect is
there for the whole clip, leave times empty and write "persistent" in the note. Keep every note
under twelve words.
Use "unknown" when you cannot judge an item. This field never changes verdict, completion or
evidence, and it must be valid JSON like the rest of the object."""


def _resolve_camera(name: str, cameras: dict) -> object | None:
    """Exact match first, then a unique dotted-suffix match.

    Models occasionally abbreviate long dataset camera names (doubao-seed-2.0-pro drops a
    leading ``observation.images.``); when the citation still identifies exactly one
    supplied camera it is accepted, ambiguous citations stay invalid.
    """
    name = name.strip()
    if name in cameras:
        return cameras[name]
    matches = [k for k in cameras if k.endswith("." + name)]
    return cameras[matches[0]] if len(matches) == 1 else None


def parse_assessment(text: str, clips: list[VideoClip]) -> dict:
    from .vlm_client import strip_reasoning

    raw = strip_reasoning(text).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    dropped = ""
    try:
        answer = json.loads(raw)
    except ValueError:
        # The defect report is a rider: a syntax error inside it must not cost the verdict.
        # Cut that one field out and read the five core fields; only if the rest is broken too
        # does this go back for a repair (and then fails as it did before the rider existed).
        without = _without_camera_check(raw)
        if without is None:
            raise
        answer = json.loads(without)
        dropped = "camera_check was not valid JSON and was dropped; the verdict is unaffected"
    if not isinstance(answer, dict) or set(answer) - {"camera_check"} != {
            "verdict", "task_type", "completion", "reason", "evidence"}:
        raise ValueError("video assessment has invalid fields")
    if answer["verdict"] not in ("success", "failure", "uncertain"):
        raise ValueError("invalid video verdict")
    if answer["task_type"] not in ("transient", "persistent"):
        raise ValueError("invalid video task type")
    score = answer["completion"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("video completion must be between 0 and 1")
    if not isinstance(answer["reason"], str) or not answer["reason"].strip():
        raise ValueError("video assessment needs a reason")
    evidence = answer["evidence"]
    if not isinstance(evidence, list) or (answer["verdict"] != "uncertain" and not evidence):
        raise ValueError("definite video verdict needs evidence")
    cameras = {c.camera: c for c in clips}
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"camera", "start_s", "end_s", "observation"}:
            raise ValueError("invalid video evidence fields")
        clip = _resolve_camera(item["camera"], cameras)
        if clip is None:
            raise ValueError(
                f"video evidence cites an unknown camera: {item['camera']!r}; "
                f"the supplied cameras are {sorted(cameras)}")
        times = [item["start_s"], item["end_s"]]
        if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in times):
            raise ValueError("invalid video evidence timestamp")
        # The prompt advertises the window end rounded to 6 decimals; models echo that or round
        # further (48.866667 -> 48.867), so compare with a small tolerance — evidence times are
        # display-grade, and a sub-0.05 s overshoot is a rounding artifact, not a real violation.
        eps = 0.05
        if not clip.start_s - eps <= times[0] <= times[1] <= clip.end_s + eps:
            raise ValueError("video evidence timestamp is outside the episode")
        if not isinstance(item["observation"], str) or not item["observation"].strip():
            raise ValueError("video evidence needs an observation")
    if dropped:
        answer["camera_check"] = parse_camera_check(None, clips)
        answer["camera_check"]["problems"] = [dropped]
    elif "camera_check" in answer:
        answer["camera_check"] = parse_camera_check(answer["camera_check"], clips)
    return answer


def _without_camera_check(raw: str) -> str | None:
    """``raw`` with the ``"camera_check": {...}`` member removed, or None if it is not there.

    Brace matching over the string, quotes and escapes respected, so a broken value inside the
    field is cut out whole. Returns None when the field is absent or its braces do not close,
    in which case the answer is broken somewhere else and the caller reports that.
    """
    key = raw.find('"camera_check"')
    if key < 0:
        return None
    start = raw.find("{", key + len('"camera_check"'))
    if start < 0:
        return None
    depth, in_string, escaped, end = 0, False, False, -1
    for i in range(start, len(raw)):
        ch = raw[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string and ch == "{":
            depth += 1
        elif not in_string and ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    head, tail = raw[:key].rstrip(), raw[end:].lstrip()
    if head.endswith(","):                      # the field was last or in the middle
        head = head[:-1].rstrip()
    elif tail.startswith(","):
        tail = tail[1:].lstrip()
    return head + (" " if head.endswith("{") else "") + tail


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def parse_camera_check(raw, clips: list[VideoClip]) -> dict:
    """The optional ``camera_check`` field, normalised; never raises.

    The three items are always present with ``level`` in none / minor / severe / unknown;
    anything the model got wrong is downgraded to ``unknown`` (or dropped, for a bad time
    interval) and named in ``problems``. A defect report must never cost a repair round
    trip or fail the review it rides on - the task verdict is the review's job.
    """
    out: dict = {item: {"level": "unknown", "times": [], "note": ""} for item in CAMERA_CHECK_ITEMS}
    out["contamination"]["kind"] = "none"
    problems: list[str] = []
    if not isinstance(raw, dict):
        out["problems"] = ["camera_check is not an object"]
        return out
    lo = min((c.start_s for c in clips), default=0.0) - 0.05
    hi = max((c.end_s for c in clips), default=float("inf")) + 0.05
    for item in CAMERA_CHECK_ITEMS:
        got = raw.get(item)
        if not isinstance(got, dict):
            problems.append(f"{item}: missing or not an object")
            continue
        level = str(got.get("level", "")).strip().lower()
        if level in CAMERA_CHECK_LEVELS:
            out[item]["level"] = level
        elif level != "unknown":
            problems.append(f"{item}: level {got.get('level')!r} is not none/minor/severe/unknown")
        times = got.get("times")
        if isinstance(times, list):
            for pair in times:
                if (isinstance(pair, (list, tuple)) and len(pair) == 2
                        and (a := _num(pair[0])) is not None and (b := _num(pair[1])) is not None
                        and lo <= a <= b <= hi):
                    if len(out[item]["times"]) < CAMERA_CHECK_MAX_TIMES:
                        out[item]["times"].append([a, b])
                else:
                    problems.append(f"{item}: time interval {pair!r} dropped")
        elif times is not None:
            problems.append(f"{item}: times is not a list")
        # A span that covers the clip says "the whole time", not "between these two seconds":
        # keep the meaning, drop the false precision (the console then reads 全程).
        span = hi - lo
        if span > 0 and any(b - a >= 0.95 * span for a, b in out[item]["times"]):
            out[item]["whole"] = True
            out[item]["times"] = []
        note = got.get("note")
        if isinstance(note, str):
            out[item]["note"] = note.strip()[:200]
        if item == "contamination":
            kind = str(got.get("kind", "")).strip().lower()
            if kind in CONTAMINATION_KINDS:
                out[item]["kind"] = kind
            else:
                out[item]["kind"] = "none" if out[item]["level"] == "none" else "other"
                if kind:
                    problems.append(f"contamination: kind {got.get('kind')!r} is not known")
    out["problems"] = problems
    return out


def make_video_assessor(endpoint: str, model: str, *, tag: str = "probe",
                        timeout_s: float = 60, api_key_env: str | None = None,
                        max_in_flight: int = 16, thinking: bool | None = None,
                        json_mode: bool = True,
                        fps: float = 5.0):
    import requests
    from .vlm_client import SharedGate, _with_thinking, auth_headers

    gate = SharedGate(max_in_flight)
    headers = auth_headers(api_key_env)
    url = endpoint.rstrip("/") + "/chat/completions"
    constrained = {"json": bool(json_mode)}      # flipped off if the backend refuses the field

    def assess(clips: list[VideoClip], instruction: str, *, hints: str = "") -> dict:
        from . import vlm_client

        prompt = TASK_PROMPT.format(instruction=instruction, hints=hints or "none")
        if tag == "endstate":
            prompt += "\nIndependently review ONLY this camera; abstain if its view is insufficient."
            prompt += CAMERA_CHECK_PROMPT       # the camera_defects report rides on every review
        elif tag == "arbitration":
            prompt += "\nRe-examine the action and object trajectory carefully; do not guess missing evidence."
        content = [{"type": "text", "text": prompt}] + video_content(clips, fps=fps)
        messages = [{"role": "user", "content": content}]

        def ask() -> str:
            """One call. ``response_format`` makes the server constrain decoding to valid JSON,
            which is where broken answers came from; a backend that rejects the field is asked
            plainly from then on (self-hosted engines do not all support it)."""
            payload = _with_thinking({"model": model, "temperature": 0.0, "max_tokens": 4096,
                                      "messages": messages}, thinking)
            if constrained["json"]:
                payload["response_format"] = {"type": "json_object"}
            response = vlm_client.hedged_request(
                lambda hard: requests.post(url, json=payload, headers=headers, timeout=hard),
                tag=tag, timeout_s=timeout_s, gate=gate)
            if (constrained["json"] and getattr(response, "status_code", 200) == 400
                    and "response_format" in (getattr(response, "text", "") or "")):
                constrained["json"] = False
                return ask()
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

        for attempt in range(2):
            text = ask()
            try:
                return parse_assessment(text, clips)
            except (ValueError, TypeError, KeyError) as exc:
                if attempt:
                    raise ValueError(f"invalid video assessment after repair: {exc}") from exc
                messages.extend([{"role": "assistant", "content": text},
                                 {"role": "user", "content": (
                                     f"Invalid answer: {exc}. Return the required JSON; cite only these "
                                     f"cameras: {[c.camera for c in clips]}, and valid times.")}])

    assess.media_input = "video"
    return assess
