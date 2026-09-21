"""Example VLM modules that exist only in tests (design doc 04, section 4.3).

Two "sample 8 frames, ask once" modules with the same frame policy, the shape
new VLM modules will have. They exercise the four paths the framework must get
right: merging, splitting an over-limit group, falling back a single unit whose
part of a merged answer does not parse, and usage attribution. ``FakeVlm`` is a
deterministic stand-in for the model: its answer depends only on the frames it
is shown and the question it is asked, so merged and single requests agree.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import partial
from typing import Any, Callable, Iterable

from curation.contracts.modules import MODULES, ModuleSpec
from curation.core.contract import CheckResult
from curation.planner import DeclaredMergeUnits, FramePolicy, MergeUnit, VlmTransportError
from curation.planner.merge import strip_reasoning

FRAME_POLICY = FramePolicy("interval", 0.5, 448, 4, "linspace:8")
CAMERAS = 3

GRASP_PROMPT = (
    "The images are frames of one robot episode in temporal order, grouped by camera.\n"
    "At the END of the episode, is an object held between the gripper fingers?\n"
    "If no view shows the gripper clearly enough to tell, answer \"unclear\".\n"
    "Answer ONLY one word: yes, no, or unclear.")
TABLE_PROMPT = (
    "The images are frames of one robot episode in temporal order, grouped by camera.\n"
    "At the END of the episode, is the table surface in front of the robot free of objects?\n"
    "If no view shows the table clearly enough to tell, answer \"unclear\".\n"
    "Answer ONLY one word: yes, no, or unclear.")
QUESTIONS = {GRASP_PROMPT: "grasp", TABLE_PROMPT: "table"}
VOTES = {"yes": True, "no": False, "unclear": None}


def parse_vote(module_id: str, text: str) -> CheckResult:
    """The module's own parser: exactly one of yes / no / unclear."""
    word = strip_reasoning(str(text)).strip().strip('."').lower()
    if word not in VOTES:
        raise ValueError(f"expected yes, no or unclear, got {text!r}")
    return CheckResult(name=module_id, passed=VOTES[word], detail={"answer": word})


def _declare(module_id: str, prompt: str) -> DeclaredMergeUnits:
    def build(episode_index: int, context: dict) -> list[MergeUnit]:
        return [MergeUnit(episode_index, module_id, FRAME_POLICY, prompt,
                          parser=partial(parse_vote, module_id), call_kind="caption",
                          max_tokens=16)]

    return DeclaredMergeUnits(FRAME_POLICY, "caption", build)


def _spec(module_id: str, name_zh: str, prompt: str) -> ModuleSpec:
    return ModuleSpec(
        id=module_id, name_zh=name_zh, summary_zh="测试用示例模块：抽 8 帧、问一次",
        level="episode", gate="hard", needs=frozenset({"video", "vlm"}), stage="vlm",
        depends_on=("frame_gates",), produces_adjudication=False,
        param_schema={"type": "object", "properties": {}, "additionalProperties": False},
        merge_units=_declare(module_id, prompt))


EXAMPLE_GRASP = _spec("example_grasp", "示例：末态抓持", GRASP_PROMPT)
EXAMPLE_TABLE = _spec("example_table", "示例：桌面清空", TABLE_PROMPT)
EXAMPLE_MODULES = (EXAMPLE_GRASP, EXAMPLE_TABLE)
#: C1 plus the two examples; the real registry never contains them.
REGISTRY = MODULES + EXAMPLE_MODULES


def units_for(episodes: Iterable[int]) -> list[MergeUnit]:
    """Both modules' units for these episodes, in a stable order."""
    return [u for ep in episodes for spec in EXAMPLE_MODULES for u in spec.merge_units(ep)]


def frames(episode_index: int, policy: FramePolicy) -> list[str]:
    """Stand-in decoded frames: labels that tell the fake model which episode it sees."""
    return [f"ep{episode_index:04d}/cam{c}/f{i}"
            for c in range(min(CAMERAS, policy.max_cams))
            for i in range(policy.frames_per_camera())]


def preflight(episodes: int = 64, *, without_task: int = 0, supported: bool = True,
              availability: dict[str, dict] | None = None,
              cameras: tuple[str, ...] = ("exterior_1", "exterior_2", "wrist")) -> dict:
    """A ``curation preflight --json`` result (valid against its contract)."""
    modules = [{"id": m.id, "availability": "available"} for m in REGISTRY]
    for entry in modules:
        entry.update((availability or {}).get(entry["id"], {}))
    return {
        "schema_version": "1.0",
        "format": {"kind": "lerobot" if supported else "mcap",
                   "version": "v2" if supported else None, "supported": supported,
                   "detail": f"LeRobot v2, {episodes} episodes" if supported else "mcap"},
        "validation": [],
        "dataset": {"episode_count": episodes, "cameras": list(cameras), "fps": 15.0,
                    "robot_type": "franka", "total_frames": episodes * 300,
                    "labels": {"with_task": episodes - without_task, "without_task": without_task},
                    "profile": None},
        "modules": modules,
        "meta_fingerprint": "sha256:" + "0" * 64,
        "warnings": [],
    }


class FakeVlm:
    """A deterministic model behind ``send(request)``.

    It reads the episode from the frame labels and the question from the prompt
    text (so a changed prompt is a test failure), and answers from a fixed truth.
    Knobs, all keyed by (episode, question) or episode:

    * ``garble`` - in merged answers this task's value does not parse
    * ``drop`` - in merged answers this task's key is missing
    * ``not_json`` - merged answers for these episodes are prose
    * ``flip`` - merged answers flip yes/no (for testing the agreement bar)
    * ``no_usage(request)`` - leave ``usage`` out of the response
    * ``transient`` - how many times a request for an episode fails with 429 first
    """

    def __init__(self, *, garble: Iterable = (), drop: Iterable = (), not_json: Iterable = (),
                 flip: Iterable = (), no_usage: Callable[[Any], bool] | None = None,
                 transient: dict[int, int] | None = None) -> None:
        self.garble, self.drop, self.flip = set(garble), set(drop), set(flip)
        self.not_json = set(not_json)
        self.no_usage = no_usage or (lambda request: False)
        self.transient = dict(transient or {})
        self.requests: list[Any] = []

    @staticmethod
    def truth(episode: int, question: str) -> str:
        digest = hashlib.sha256(f"{episode}:{question}".encode()).digest()
        return ("yes", "no", "unclear")[digest[0] % 3]

    @staticmethod
    def episode_of(request) -> int:
        seen = {int(m.group(1)) for img in request.images
                for m in [re.match(r"ep(\d+)/", str(img))] if m}
        assert len(seen) == 1, f"a request must show one episode, saw {seen}"
        return seen.pop()

    @staticmethod
    def tasks_of(text: str) -> list[str]:
        """The questions of a merged prompt, in task order."""
        heads = list(re.finditer(r"^Task (\d+) \(([a-z0-9_]+)\): ", text, flags=re.M))
        out = []
        for i, head in enumerate(heads):
            assert int(head.group(1)) == i + 1
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            out.append(QUESTIONS[text[head.end():end].rstrip("\n")])
        return out

    def __call__(self, request):
        self.requests.append(request)
        episode = self.episode_of(request)
        if self.transient.get(episode, 0) > 0:
            self.transient[episode] -= 1
            raise VlmTransportError("429 Too Many Requests", cause="rate_limited", status=429,
                                    retry_after_s=0.0)
        if request.text in QUESTIONS:                               # asked alone
            content = self.truth(episode, QUESTIONS[request.text])
        elif "Return STRICT JSON" in request.text:                  # asked merged
            if episode in self.not_json:
                content = "Task 1 looks done to me; the rest I am not sure about."
            else:
                answer = {}
                for i, question in enumerate(self.tasks_of(request.text), 1):
                    key, word = f"task_{i}", self.truth(episode, question)
                    if (episode, question) in self.drop:
                        continue
                    if (episode, question) in self.garble:
                        word = "definitely maybe"
                    elif (episode, question) in self.flip:
                        word = {"yes": "no", "no": "yes"}.get(word, word)
                    answer[key] = word
                content = "```json\n" + json.dumps(answer) + "\n```"
        else:
            raise AssertionError(f"the fake model does not understand: {request.text[:80]!r}")
        body = {"choices": [{"message": {"content": content}}]}
        if not self.no_usage(request):
            prompt = len(request.text) // 4 + 256 * len(request.images)
            completion = max(1, len(content) // 4)
            body["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion,
                             "completion_tokens_details": {"reasoning_tokens": min(3, completion)},
                             "prompt_tokens_details": {"cached_tokens": 128 * len(request.images)}}
        return body
