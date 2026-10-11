"""The fake model answers the same on every platform.

The JPEG bytes a frame encodes to differ between the macOS and Linux wheels
(PyAV/FFmpeg decode, OpenCV's libjpeg-turbo encode). The fake picks its answer
from the request's texts and each image's pixel size, never the bytes
(``FakeVlm.answer_key``), so a test gets the same verdicts on either.
"""
from __future__ import annotations

import os
import shutil

import numpy as np
import requests

from parity.fakevlm import FakeVlm

from .fakevlm_server import FakeVlmServer
from .pipeline import comparable, results, run, verdict_of

SCORE_PROMPT = "Rate this robot episode from 0 to 100. Score:"
ANNOTATION = "pick up the red block and place it in the bin"


def _frame(width: int = 128, height: int = 96, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def _data_uri(frame: np.ndarray, quality: int) -> str:
    import base64

    import cv2

    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _payload(*urls: str, text: str = SCORE_PROMPT) -> dict:
    content = [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in urls]
    return {"model": "fake-vlm", "temperature": 0.0, "max_tokens": 16,
            "messages": [{"role": "user", "content": content}]}


def test_the_server_answers_a_re_encoded_request_alike():
    frame = _frame(seed=7)
    urls = [_data_uri(frame, q) for q in (95, 85, 30)]
    assert len(set(urls)) == 3                            # other bytes ...
    with FakeVlmServer() as vlm:
        got = [requests.post(vlm.url + "/chat/completions", json=_payload(u), timeout=10)
               .json()["choices"][0]["message"]["content"] for u in urls]
    assert len(set(got)) == 1 and got[0].isdigit()        # ... the same answer
    assert len({FakeVlm.answer_key(_payload(u)) for u in urls}) == 1


def test_video_run_sends_video_inputs(vlm_stage, tmp_path):
    """The VLM stage sends the episode's videos, not sampled frames, and the records are the reference's."""
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["base"], rd)
    with FakeVlmServer() as vlm:
        res = run("check", "--modules", "task_success", "--input", vlm_stage["dataset"],
                  "--run-dir", rd, "--episodes", vlm_stage["episodes"],
                  "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm")
    assert res.rc == 0, res.doc
    assert any(c["type"] == "video_url" for call in vlm.calls
               for m in call["payload"].get("messages", []) if isinstance(m.get("content"), list)
               for c in m["content"])
    got = results(rd, "task_success")
    assert {e: comparable(r) for e, r in got.items()} == \
        {e: comparable(r) for e, r in vlm_stage["reference"].items()}


def test_the_mini_dataset_walks_every_task_success_path(vlm_stage):
    """What the CLI tests build on (``parity.fakevlm.SEED``) under the one judgement (D71):
    abstentions that go to a person, rejections a camera backs, and captions other than the
    annotation (4 and 6). Nothing is rescued by a second request any more."""
    got = {e: (verdict_of(r), r["details"].get("verdict"))
           for e, r in vlm_stage["reference"].items()}
    # 4 and 6 have no task text: not judged at all (D72), no caption is written for them
    assert got == {0: ("abstain", "uncertain"), 1: ("fail", "failure"),
                   2: ("abstain", "uncertain"), 3: ("abstain", "uncertain"),
                   4: ("abstain", None), 5: ("fail", "failure"),
                   6: ("abstain", None), 7: ("abstain", "uncertain")}
    assert {e for e, r in vlm_stage["reference"].items() if r["details"].get("skipped")} == {4, 6}
    assert not os.path.exists(f"{vlm_stage['reference_dir']}/autolabel")
