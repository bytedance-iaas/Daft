"""A deterministic stand-in for an OpenAI-compatible VLM endpoint.

It answers each v1 prompt family in the format that prompt asks for, and the
answer depends only on the request content (hash of the canonical request),
so the same request always gets the same answer. That is all the parity tests
need: v1 run twice against it must agree bit for bit, and a recorded run must
replay exactly.

Plug it in through ``TapeHooks(transport=FakeVlm(...).transport())``.
"""
from __future__ import annotations

import json
import re

from .vlm_tape import canonical_request

CAPTIONS = (
    "pick up the red block and place it in the bin",
    "push the red block to the left side of the table",
    "move the blue cup next to the plate",
)


def _texts(payload: dict) -> str:
    parts = []
    for msg in payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [p.get("text", "") for p in content if isinstance(p, dict)
                      and p.get("type") == "text"]
    return "\n".join(parts)


def _bullets_after(text: str, marker: str) -> list[str]:
    tail = text.split(marker, 1)[1] if marker in text else ""
    return [line[2:].strip() for line in tail.splitlines() if line.startswith("- ")]


class FakeVlm:
    def __init__(self, model: str = "fake-vlm"):
        self.model = model
        self.calls = 0

    # -- answers ---------------------------------------------------------------
    def answer(self, payload: dict) -> str:
        text = _texts(payload)
        _, digest = canonical_request(payload)
        n = int(digest.split(":")[1][:8], 16)
        if "Build a TWO-LEVEL skill taxonomy" in text:
            caps = _bullets_after(text, "CAPTIONS:")
            return json.dumps({"families": [{
                "name": "grasp-and-transport", "criterion": "grasp an object and carry it",
                "subskills": [{"name": "place-object", "criterion": "carry and release",
                               "members": caps}]}]})
        if "Sub-skill A:" in text and "Answer ONLY yes or no" in text:
            return "no"
        if "Assign the caption below to exactly one EXISTING sub-skill" in text:
            return json.dumps({"family": "grasp-and-transport", "subskill": "place-object"})
        if "Translate the following robot-manipulation skill names" in text:
            return json.dumps({"families": []})
        if "Skill families:" in text and "ANNOTATIONS:" in text:
            labels = [line.strip("- ").strip() for line in
                      text.split("ANNOTATIONS:", 1)[1].splitlines() if line.strip()]
            return json.dumps({"map": [{"label": lb, "family": "grasp-and-transport"}
                                       for lb in labels]})
        if "Return STRICT JSON: {\"pairs\"" in text or "\"pairs\": [" in text:
            idx = sorted({int(x) for x in re.findall(r"^\s*\[?(\d+)\]?[.):]", text.split(
                "PAIRS:", 1)[-1], flags=re.M)}) or [0]
            return json.dumps({"pairs": [{"i": i, "verdict": "same", "why": "fake"}
                                         for i in idx]})
        if "You are preparing a verification checklist" in text:
            return json.dumps({"task_type": "persistent", "target_location": "the bin",
                               "target_visual": "a gray plastic bin", "object": "the block",
                               "verify_question": "Is the block inside the bin?"})
        if "locate two things" in text:
            return "NOT VISIBLE\nNOT VISIBLE"
        if "You are verifying whether a robot manipulation task succeeded" in text:
            return "Step 1: a block near a bin.\nVERDICT: " + ("YES" if n % 2 else "UNCLEAR")
        if "Did the robot COMPLETE the task" in text:
            return "yes" if n % 4 else "no"
        if "Did the robot FAIL to complete" in text:
            return "no" if n % 4 else "yes"
        if "All cameras show the SAME robot episode" in text:
            return CAPTIONS[n % len(CAPTIONS)]
        if "robot episode" in text and ("0 to 100" in text or "Score" in text):
            return str(n % 101)
        if text.strip() == "ping":
            return "pong"
        return "unclear"

    # -- transport -------------------------------------------------------------
    def _response(self, url: str, status: int, body: dict):
        import requests
        from requests.structures import CaseInsensitiveDict

        r = requests.models.Response()
        r.status_code = status
        r.reason = "OK" if status == 200 else "Error"
        r._content = json.dumps(body).encode("utf-8")
        r.encoding = "utf-8"
        r.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
        r.url = url
        return r

    def post(self, url, *args, json=None, **kw):  # noqa: A002 - mirrors requests.post
        self.calls += 1
        content = self.answer(json or {})
        body = {"id": f"fake-{self.calls}", "object": "chat.completion",
                "model": (json or {}).get("model", self.model),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": len(content) // 4 + 1,
                          "total_tokens": 100 + len(content) // 4 + 1,
                          "completion_tokens_details": {"reasoning_tokens": 0},
                          "prompt_tokens_details": {"cached_tokens": 0}}}
        return self._response(url, 200, body)

    def get(self, url, *args, **kw):
        return self._response(url, 200, {"object": "list",
                                         "data": [{"id": self.model, "object": "model"}]})

    def transport(self) -> dict:
        return {"post": self.post, "get": self.get}
