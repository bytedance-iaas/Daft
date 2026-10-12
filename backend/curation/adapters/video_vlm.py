"""Video-native task assessment: the one request that judges an episode and every camera (D71)."""
from __future__ import annotations

import json
import math

from .video_input import VideoClip, video_content

#: ONE request judges the episode AND every camera (design 13, D71): no review request per
#: camera, no arbitration, and since D73 no label guard either. The per-camera answers come out of
#: the same reasoning pass, so they are not an independent second signature - what guards a
#: rejection is the evidence it must cite and sending everything less than a clear answer to a
#: person. Records judged under the earlier two-pass protocol (``video-task/1``) are stale:
#: ``check --resume`` judges them again. An episode without a task text gets the picture-defect
#: request alone (``CAMERAS_ONLY_PROMPT``): same cameras, same defect items, no verdict.
PROTOCOL = "video-task/2"
#: the per-camera picture-defect report answered inside that same request (camera_defects module).
#: Bump it whenever the wording below changes: it is what makes records judged with the old prompt
#: stale, so ``check --resume`` judges them again. ``camera-check/1`` and ``1.1`` were the two-pass
#: review's; ``2`` is the single-pass block's; ``3`` adds occlusion and the camera's view (what the
#: camera physically is, against its name); ``4`` tells occlusion from the manipulation itself and
#: from glitch, and has the kind read from the picture before the name; ``5`` names what occlusion
#: is NOT (lighting, the scene's own content, a view pointed elsewhere), gives it its own share,
#: place and length, and asks the cameras whether they agree on which arm is which.
CAMERA_CHECK_PROTOCOL = "camera-check/5"
CAMERA_CHECK_ITEMS = ("glitch", "shake", "contamination", "occlusion")
CAMERA_CHECK_LEVELS = ("none", "minor", "severe")
CONTAMINATION_KINDS = ("none", "dirt", "smudge", "water", "obstruction", "other")
OCCLUSION_SOURCES = ("none", "arm", "body", "person", "object", "cable", "other")
#: where in the picture the occluding thing sits at its worst moment
OCCLUSION_REGIONS = ("none", "left", "right", "top", "bottom", "center", "whole")
CAMERA_CHECK_MAX_TIMES = 8
#: the camera's view, read from the picture alone and held against the camera's name (MV-2)
VIEW_KINDS = ("wrist", "fixed")
VIEW_ARMS = ("left", "right", "single")
VIEW_MATCHES = ("yes", "no")
VIEW_FIELDS = ("kind", "arm", "matches_name", "same_as", "note")
#: whether the cameras agree on which physical arm is which (MV-2, episode level): one camera can
#: only be held against its own name, so a crossed PAIR is only visible across cameras - which this
#: one request can do, since every camera's video is in it and they share a clock.
ARM_VERDICTS = ("consistent", "crossed")
#: How long one video request may take grows with what it carries (2026-10-08): the model's
#: latency is close to proportional to the frames it is sent (cameras x seconds x fps - DROID's
#: three cameras over 18 s at 5 fps, about 270 frames, take some 80 s), so a timeout fixed for
#: image requests (60 s) made the hedge fire on nearly every long episode and waste a request.
#: The configured timeout stays the floor; the request's own size raises it, up to a cap.
TIMEOUT_PER_FRAME_S = 0.5
TIMEOUT_MAX_S = 600.0


def request_timeout(clips: list[VideoClip], fps: float, floor_s: float) -> float:
    """The timeout of one request over ``clips`` sampled at ``fps``: never under ``floor_s``,
    half a second per frame sent, never over :data:`TIMEOUT_MAX_S`."""
    frames = sum(max(0.0, float(c.end_s) - float(c.start_s)) * float(fps) for c in clips)
    return float(min(TIMEOUT_MAX_S, max(float(floor_s), TIMEOUT_PER_FRAME_S * frames)))


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
#: what the three picture-defect items mean. Shared word for word by the two requests below (the
#: judgement's per-camera block and the defects-only request), so both ask exactly the same question.
CAMERA_CHECK_DEFS = """glitch: the picture CONTENT is damaged - corrupted, torn, blocky, smeared or
garbled frames, wrong colours in patches, or a frozen picture with artifacts. shake: the WHOLE
picture moves as one while the scene itself stays intact - the view jolts, drifts or wobbles, the
framing jumps, and the edges may show black bars or a changed field of view; the camera body is
moving, not the robot or the objects. A wrist camera moving with the arm is normal, report only
jitter beyond that. Decide between the two by asking whether the scene is still whole: a whole
scene that jumps is shake, a broken-looking scene is glitch; report both only when both are really
there. contamination: dirt, smudges, water drops or an object stuck to or covering the lens.
"minor" is visible but leaves the scene readable; "severe" hides or distorts the scene for part of
the episode. occlusion: for part of this clip something BETWEEN the lens and the scene hides the
working area - the gripper, the object being handled, or the place it goes. What that looks like: a
patch of the picture is taken over by something much nearer the lens (out of focus, over-large,
running off the frame edge) while the rest of the picture carries on unchanged behind it. Report it
only when all three hold: the thing is no part of the manipulation, it hides the WORK rather than
empty background, and it lasts long enough that the action cannot be followed. The following are
NOT occlusion and stay at "none", however much of the picture they take or however dark they are:
(a) any change of LIGHT - a lamp switched on or off, a shadow, auto-exposure, the room dimming.
    The whole picture changes together and nothing is in front of the lens. If the other cameras
    change at the same moment, it is the light; a real occlusion shows on ONE camera.
(b) the robot's own arm, gripper or the object it carries, even filling the frame - that is the
    task being done; a wrist camera filled by what the gripper holds is normal, and a held object
    leaving the frame is not occlusion either.
(c) a person, a hand or an object simply BEING in the scene at working distance, an operator at the
    bench included; a person counts only when they pass between the lens and the work.
(d) a view that is dark, empty, far from the work or pointed elsewhere - nothing is covering it.
Tell occlusion from glitch by the picture around it: a shape covering part of a picture that is
otherwise whole and sharp is occlusion (something in front of the lens), however straight its
edges; a glitch is damage INSIDE the picture - blocks of wrong pixels, tearing, smearing, garbled
colours. Never report the same stretch as both. "minor" hides the work briefly or in part, "severe"
hides it for a stretch long enough that the action cannot be followed. Three numbers go with it,
all about the worst moment: source says what hides the work; coverage is the fraction of the
picture that thing takes up then, 0 to 1; region says where it sits - left, right, top, bottom,
center, or whole when it covers nearly everything. duration_s is how many seconds of this clip are
hidden in all. Say in the note what the thing is. times are episode-relative seconds within that camera's own window, like evidence;
leave times empty for "none". Give up to four intervals; when the defect recurs in short bursts
across the clip, give the span that encloses them rather than one burst, and say "intermittent" in
the note. When the defect is there for the whole clip, leave times empty and write "persistent" in
the note. view: what THIS camera physically is. Decide kind from the picture BEFORE you read the
camera's name, and never from the name: when the whole room swings and turns as the arm moves
while the gripper fingers stay in the same place in the frame, it is "wrist", whatever it is
called; when the room stays still and the arm travels through it, it is "fixed", whatever it is
called. Say in the note what you saw. arm is the arm the camera is mounted on or mainly looks at,
"single" when the robot has one arm. matches_name compares that kind with the supplied name: "no"
when a name saying wrist, hand or gripper sits on a picture whose room stays still, when a name
saying exterior, front, side, top, overhead or external sits on a picture that swings with the
arm, or when left/right in the name is on the other arm of a two-arm robot; "yes" when the name
agrees; Left or right in the name of a single-arm robot or of a stereo pair says nothing about
arms: that part is "unknown" rather than a guess, and the kind still counts. same_as names
another supplied camera when the two pictures are the same recording, else leave it empty. Keep
every note under twelve words. Use "unknown" when you cannot judge an item. These items never
change verdict, completion or evidence."""

#: the one question that cannot be asked of a single camera: are the arms crossed BETWEEN cameras?
ARMS_DEFS = """arms: whether the cameras agree on which physical arm is which. This is about the set
of cameras, not one of them, so answer it once for the episode. Do it only for a robot with two
arms and at least two cameras whose names name an arm (left / right, or one wrist camera per arm):
find a stretch where ONE arm alone moves, follow that same physical arm in every camera, and hold it
against each camera's name. A camera that rides or faces the other arm than its name says is
crossed - give its exact supplied name in "crossed" and answer "crossed". Answer "consistent" only
when you followed one moving arm across the cameras and every name agreed. Answer "unknown" for a
one-arm robot, when no camera's name names an arm, when only one camera was supplied, or when no
stretch shows one arm moving alone; never settle it from the names alone. Say in the note what
moved, where you saw it and when. This never changes verdict, completion or evidence."""

#: the episode-level block that rides next to ``cameras`` in both requests below
ARMS_SHAPE = """"arms":{"identity":"consistent|crossed|unknown",
          "crossed":["<exact supplied camera name>"],"note":"..."}"""

#: the members of one camera's ``camera_check``: the four picture-defect items and the view
CAMERA_CHECK_SHAPE = """"glitch":{"level":"none|minor|severe|unknown","times":[[start_s,end_s]],"note":"..."},
     "shake":{"level":"none|minor|severe|unknown","times":[[start_s,end_s]],"note":"..."},
     "contamination":{"level":"none|minor|severe|unknown",
                      "kind":"none|dirt|smudge|water|obstruction|other",
                      "times":[[start_s,end_s]],"note":"..."},
     "occlusion":{"level":"none|minor|severe|unknown",
                  "source":"none|arm|body|person|object|cable|other",
                  "coverage":0.0,"region":"none|left|right|top|bottom|center|whole",
                  "duration_s":0.0,"times":[[start_s,end_s]],"note":"..."},
     "view":{"kind":"wrist|fixed|unknown","arm":"left|right|single|unknown",
             "matches_name":"yes|no|unknown","same_as":"","note":"..."}"""

#: the single-pass shape: ONE request judges the episode and every camera. The five core fields keep
#: their meaning - they are the episode's verdict - and each camera says what it alone shows, so a
#: finding can still be attributed to one camera without a request per camera.
CAMERAS_PROMPT = """
Add ONE more field "cameras" to the same JSON object, next to the five fields above (keep those
five exactly as specified). Give one entry for EVERY camera supplied above, under its exact name,
and one more field "arms" answered once for the whole set:
{"cameras":{"<exact supplied camera name>":{
   "verdict":"success|failure|uncertain",
   "reason":"what THIS camera alone shows, under fifteen words",
   "camera_check":{
     """ + CAMERA_CHECK_SHAPE + """}}},
 """ + ARMS_SHAPE + """}
Judge each camera on what THAT camera shows and nothing else: a camera that cannot see the objects
or the relevant action is "uncertain" for that camera, even when another camera settles the episode.
Do not copy the episode verdict into every camera. The episode's own verdict stays in the five
fields above and is yours to decide from all the cameras together.
""" + CAMERA_CHECK_DEFS + """
""" + ARMS_DEFS + """
The whole answer must be one valid JSON object."""

#: the request an episode WITHOUT a task text gets (D73): the same cameras and the same defect
#: items and view, no verdict - there is no task to judge, so nothing is asked about success or failure.
CAMERAS_ONLY_PROMPT = """Inspect the supplied continuous videos for picture defects only. This episode
has no task description, so there is no task to judge: do not assess success or failure, do not
describe what the robot does. All cameras show the same episode.
Return one JSON object with exactly two fields: "cameras", with one entry for EVERY camera supplied
above under its exact name, and "arms", answered once for the whole set:
{"cameras":{"<exact supplied camera name>":{
   "camera_check":{
     """ + CAMERA_CHECK_SHAPE + """}}},
 """ + ARMS_SHAPE + """}
""" + CAMERA_CHECK_DEFS + """
""" + ARMS_DEFS + """
The whole answer must be one valid JSON object."""


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
    """The model's answer, validated. The ``cameras`` block is required (a missing one costs the one
    repair round, like a missing core field, because the per-camera verdicts are read by the
    decision), but lenient inside it: a
    camera the model got wrong reads ``unavail`` and is named in ``problems``, never an error."""
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
    if not isinstance(answer, dict) or set(answer) - {"cameras", "arms"} != {
            "verdict", "task_type", "completion", "reason", "evidence"}:
        raise ValueError("video assessment has invalid fields")
    if "cameras" not in answer:
        raise ValueError("the single-pass answer needs a per-camera block")
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
    answer["cameras"] = parse_cameras(answer.get("cameras"), clips)
    answer["arms"] = parse_arms(answer.get("arms"), clips)
    if dropped:
        for entry in answer["cameras"].values():
            entry["camera_check"].setdefault("problems", []).append(dropped)
    return answer


def parse_cameras(raw, clips: list[VideoClip]) -> dict:
    """``{camera: {verdict, reason, camera_check}}`` for every supplied camera; never raises.

    One entry per clip, always, so the decision reads a complete table: a camera the model left
    out, or answered badly, reads ``unavail`` - which the decision counts as neither a success nor
    a failure vote. Each camera's defect times are bounded by THAT camera's own window.
    """
    answers = raw if isinstance(raw, dict) else {}
    known = {c.camera: c for c in clips}
    out: dict = {}
    for clip in clips:
        got = answers.get(clip.camera)
        if got is None:                       # models shorten long feature names, as in evidence
            for name, value in answers.items():
                if isinstance(name, str) and _resolve_camera(name, known) is clip:
                    got = value
                    break
        entry = {"verdict": "unavail", "reason": "", "camera_check": parse_camera_check(None, [clip])}
        problems: list[str] = []
        if not isinstance(got, dict):
            problems.append("the model gave no entry for this camera")
        else:
            verdict = str(got.get("verdict", "")).strip().lower()
            if verdict in ("success", "failure", "uncertain"):
                entry["verdict"] = verdict
            else:
                problems.append(f"verdict {got.get('verdict')!r} is not success/failure/uncertain")
            reason = got.get("reason")
            entry["reason"] = reason.strip()[:200] if isinstance(reason, str) else ""
            entry["camera_check"] = parse_camera_check(got.get("camera_check"), [clip])
        if problems:
            entry["camera_check"].setdefault("problems", []).extend(problems)
        out[clip.camera] = entry
    extra = [n for n in answers if isinstance(n, str) and _resolve_camera(n, known) is None]
    if extra and out:
        next(iter(out.values()))["camera_check"].setdefault("problems", []).append(
            f"the answer named cameras that were not supplied: {sorted(extra)[:4]}")
    return out


def parse_cameras_only(text: str, clips: list[VideoClip]) -> dict:
    """The answer to :data:`CAMERAS_ONLY_PROMPT`: ``{"cameras": {camera: {verdict, reason,
    camera_check}}}`` in the shape of :func:`parse_cameras` - ``verdict`` is ``unavail`` for every
    camera, since nothing was judged - so the camera_defects rider reads both answers alike. An
    answer without a ``cameras`` object costs the one repair round; inside it, lenient."""
    from .vlm_client import strip_reasoning

    raw = strip_reasoning(text).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    answer = json.loads(raw)
    if not isinstance(answer, dict) or not isinstance(answer.get("cameras"), dict):
        raise ValueError("the picture-defect answer needs a cameras object")
    answers, known = answer["cameras"], {c.camera: c for c in clips}
    out: dict = {}
    for clip in clips:
        got = answers.get(clip.camera)
        if got is None:
            for name, value in answers.items():
                if isinstance(name, str) and _resolve_camera(name, known) is clip:
                    got = value
                    break
        check = got.get("camera_check") if isinstance(got, dict) else None
        entry = {"verdict": "unavail", "reason": "", "camera_check": parse_camera_check(check, [clip])}
        if not isinstance(got, dict):
            entry["camera_check"].setdefault("problems", []).append("the model gave no entry for this camera")
        out[clip.camera] = entry
    return {"cameras": out, "arms": parse_arms(answer.get("arms"), clips)}


def empty_arms() -> dict:
    """The episode's arm identity when the model said nothing about it."""
    return {"identity": "unknown", "crossed": [], "note": "", "problems": []}


def parse_arms(raw, clips: list[VideoClip]) -> dict:
    """The episode-level ``arms`` block, normalised; never raises.

    ``crossed`` must name cameras that were actually supplied (the same lenient resolution evidence
    gets), and a ``crossed`` verdict that names none is not actionable - it reads ``unknown`` with
    the slip in ``problems``, so a vague answer never becomes a finding.
    """
    out = empty_arms()
    problems: list[str] = out["problems"]
    if not isinstance(raw, dict):
        if raw is not None:
            problems.append("arms is not an object")
        return out
    identity = str(raw.get("identity", "")).strip().lower()
    if identity in ARM_VERDICTS:
        out["identity"] = identity
    elif identity != "unknown":
        problems.append(f"arms: identity {raw.get('identity')!r} is not {'/'.join(ARM_VERDICTS)}/unknown")
    known = {c.camera: c for c in clips}
    names = raw.get("crossed")
    if isinstance(names, list):
        for name in names[:8]:
            clip = _resolve_camera(name, known) if isinstance(name, str) else None
            if clip is None:
                problems.append(f"arms: crossed names {name!r}, which was not supplied")
            elif clip.camera not in out["crossed"]:
                out["crossed"].append(clip.camera)
    elif names is not None:
        problems.append("arms: crossed is not a list")
    note = raw.get("note")
    if isinstance(note, str):
        out["note"] = note.strip()[:200]
    if out["identity"] == "crossed" and not out["crossed"]:
        out["identity"] = "unknown"
        problems.append("arms: crossed, but no supplied camera was named")
    return out


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


def empty_view() -> dict:
    """A camera's ``view`` when the model said nothing about it."""
    return {"kind": "unknown", "arm": "unknown", "matches_name": "unknown", "same_as": "", "note": ""}


def parse_view(raw, problems: list[str]) -> dict:
    """The ``view`` member of a camera's ``camera_check``, normalised; never raises. Enum values the
    model got wrong read ``unknown`` and are named in ``problems``; ``same_as`` is kept as the model
    wrote it (which supplied camera it names is settled by the rider, which knows them all)."""
    out = empty_view()
    if not isinstance(raw, dict):
        problems.append("view: missing or not an object")
        return out
    for field, allowed in (("kind", VIEW_KINDS), ("arm", VIEW_ARMS), ("matches_name", VIEW_MATCHES)):
        value = str(raw.get(field, "")).strip().lower()
        if value in allowed:
            out[field] = value
        elif value != "unknown":
            problems.append(f"view: {field} {raw.get(field)!r} is not one of {'/'.join(allowed)}/unknown")
    same_as = raw.get("same_as")
    if isinstance(same_as, str):
        out["same_as"] = same_as.strip()[:120]
    elif same_as is not None:
        problems.append("view: same_as is not a string")
    note = raw.get("note")
    if isinstance(note, str):
        out["note"] = note.strip()[:200]
    return out


def parse_camera_check(raw, clips: list[VideoClip]) -> dict:
    """The optional ``camera_check`` field, normalised; never raises.

    The four items are always present with ``level`` in none / minor / severe / unknown, and so
    is ``view``; anything the model got wrong is downgraded to ``unknown`` (or dropped, for a bad
    time interval) and named in ``problems``. A defect report must never cost a repair round
    trip or fail the review it rides on - the task verdict is the review's job.
    """
    out: dict = {item: {"level": "unknown", "times": [], "note": ""} for item in CAMERA_CHECK_ITEMS}
    out["contamination"]["kind"] = "none"
    out["occlusion"].update(source="none", region="unknown", coverage=None, duration_s=None, share=None)
    out["view"] = empty_view()
    problems: list[str] = []
    if not isinstance(raw, dict):
        out["problems"] = ["camera_check is not an object"]
        return out
    lo = min((c.start_s for c in clips), default=0.0) - 0.05
    hi = max((c.end_s for c in clips), default=float("inf")) + 0.05
    window = hi - lo - 0.1                      # the clip's own length, without the tolerance
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
        if item == "occlusion":
            source = str(got.get("source", "")).strip().lower()
            if source in OCCLUSION_SOURCES:
                out[item]["source"] = source
            else:
                out[item]["source"] = "none" if out[item]["level"] == "none" else "other"
                if source:
                    problems.append(f"occlusion: source {got.get('source')!r} is not known")
            region = str(got.get("region", "")).strip().lower()
            if region in OCCLUSION_REGIONS:
                out[item]["region"] = region
            else:
                out[item]["region"] = "none" if out[item]["level"] == "none" else "unknown"
                if region:
                    problems.append(f"occlusion: region {got.get('region')!r} is not known")
            # How much of the picture it takes at its worst (the model's own estimate) and how long
            # it lasts: both are asked for, both are repaired from what else is known, so the
            # report always has a number to quote.
            coverage = _num(got.get("coverage"))
            if coverage is not None and 0.0 <= coverage <= 1.0:
                out[item]["coverage"] = round(coverage, 3)
            else:
                out[item]["coverage"] = 0.0 if out[item]["level"] == "none" else None
                if got.get("coverage") is not None:
                    problems.append(f"occlusion: coverage {got.get('coverage')!r} is not between 0 and 1")
            duration = _num(got.get("duration_s"))
            if duration is None or duration < 0 or (window > 0 and duration > window + 0.05):
                if got.get("duration_s") is not None:
                    problems.append(f"occlusion: duration_s {got.get('duration_s')!r} dropped")
                duration = None
            if duration is None:                   # fall back on the stretches the model did name
                if out[item]["level"] == "none":
                    duration = 0.0
                elif out[item].get("whole") or not out[item]["times"]:
                    duration = round(window, 3) if window > 0 else None
                else:
                    duration = round(sum(b - a for a, b in out[item]["times"]), 3)
            out[item]["duration_s"] = duration
            # the share of the clip it hides, for the console's one-line reading
            out[item]["share"] = (round(min(1.0, duration / window), 3)
                                  if duration is not None and window > 0 else None)
    out["view"] = parse_view(raw.get("view"), problems)
    out["problems"] = problems
    return out


def make_video_assessor(endpoint: str, model: str, *, timeout_s: float = 60, api_key_env: str | None = None,
                        max_in_flight: int = 16, thinking: bool | None = None,
                        json_mode: bool = True, fps: float = 5.0, cameras_only: bool = False):
    """The ``probe`` request is the judgement: it also answers for every camera (D71), so there is
    no review request per camera and no arbitration. With ``cameras_only`` the same request asks
    for the picture defects alone (an episode without a task text, D73); ``instruction`` is then
    ignored."""
    import requests
    from .vlm_client import SharedGate, _with_thinking, auth_headers

    gate = SharedGate(max_in_flight)
    headers = auth_headers(api_key_env)
    url = endpoint.rstrip("/") + "/chat/completions"
    constrained = {"json": bool(json_mode)}      # flipped off if the backend refuses the field

    def assess(clips: list[VideoClip], instruction: str = "", *, hints: str = "") -> dict:
        from . import vlm_client

        if cameras_only:
            prompt = CAMERAS_ONLY_PROMPT
        else:                                   # one request answers the episode and every camera
            prompt = TASK_PROMPT.format(instruction=instruction, hints=hints or "none") + CAMERAS_PROMPT
        content = [{"type": "text", "text": prompt}] + video_content(clips, fps=fps)
        messages = [{"role": "user", "content": content}]
        budget_s = request_timeout(clips, fps, timeout_s)   # grows with the video sent

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
                tag="probe", timeout_s=budget_s, gate=gate)
            if (constrained["json"] and getattr(response, "status_code", 200) == 400
                    and "response_format" in (getattr(response, "text", "") or "")):
                constrained["json"] = False
                return ask()
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

        for attempt in range(2):
            text = ask()
            try:
                if cameras_only:
                    return parse_cameras_only(text, clips)
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
