"""Video-native task assessment; shared by scoring, camera review and arbitration."""
from __future__ import annotations

import json
import math

from .video_input import VideoClip, video_content

PROTOCOL = "video-task/1"
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
    answer = json.loads(raw)
    if not isinstance(answer, dict) or set(answer) != {
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
    return answer


def make_video_assessor(endpoint: str, model: str, *, tag: str = "probe",
                        timeout_s: float = 60, api_key_env: str | None = None,
                        max_in_flight: int = 16, thinking: bool | None = None,
                        fps: float = 5.0):
    import requests
    from .vlm_client import SharedGate, _with_thinking, auth_headers

    gate = SharedGate(max_in_flight)
    headers = auth_headers(api_key_env)
    url = endpoint.rstrip("/") + "/chat/completions"

    def assess(clips: list[VideoClip], instruction: str, *, hints: str = "") -> dict:
        from . import vlm_client

        prompt = TASK_PROMPT.format(instruction=instruction, hints=hints or "none")
        if tag == "endstate":
            prompt += "\nIndependently review ONLY this camera; abstain if its view is insufficient."
        elif tag == "arbitration":
            prompt += "\nRe-examine the action and object trajectory carefully; do not guess missing evidence."
        content = [{"type": "text", "text": prompt}] + video_content(clips, fps=fps)
        messages = [{"role": "user", "content": content}]
        for attempt in range(2):
            payload = _with_thinking({"model": model, "temperature": 0.0, "max_tokens": 4096,
                                      "messages": messages}, thinking)
            response = vlm_client.hedged_request(
                lambda hard: requests.post(url, json=payload, headers=headers, timeout=hard),
                tag=tag, timeout_s=timeout_s, gate=gate)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
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
