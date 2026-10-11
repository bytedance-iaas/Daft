"""The fake model picks its answers from a platform-stable key.

The same frame encodes to other JPEG bytes on another platform (other PyAV/FFmpeg
and libjpeg-turbo builds). The fake's answer must not move with them; the tape's
request hash still must, since replay matches requests exactly.
"""
from __future__ import annotations

import base64
import json

import cv2
import numpy as np

from parity.fakevlm import FakeVlm, answer_view
from parity.vlm_tape import canonical_request

SCORE_PROMPT = "Rate this robot episode from 0 to 100. Score:"


def _jpeg(frame: np.ndarray, quality: int) -> str:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _frame(width: int = 128, height: int = 96, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (height, width, 3), dtype=np.uint8)


def _payload(*urls: str, text: str = SCORE_PROMPT, **params) -> dict:
    content = [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in urls]
    return {"model": "fake-vlm", "temperature": 0.0, "max_tokens": 16, **params,
            "messages": [{"role": "user", "content": content}]}


def test_re_encoding_an_image_leaves_the_answer_unchanged():
    frame = _frame()
    a, b = _payload(_jpeg(frame, 85)), _payload(_jpeg(frame, 40))
    assert canonical_request(a)[1] != canonical_request(b)[1]   # the tape tells them apart
    assert FakeVlm.answer_key(a) == FakeVlm.answer_key(b)
    fake = FakeVlm()
    assert fake.answer(a) == fake.answer(b)
    # other pixels of the same size, other model parameters: still the same key
    assert FakeVlm.answer_key(_payload(_jpeg(_frame(seed=1), 85))) == FakeVlm.answer_key(a)
    assert FakeVlm.answer_key(_payload(_jpeg(frame, 85), max_tokens=2048,
                                       reasoning_effort="low")) == FakeVlm.answer_key(a)


def test_texts_image_count_and_pixel_size_make_the_key():
    frame = _jpeg(_frame(), 85)
    keys = {FakeVlm.answer_key(_payload(frame)),
            FakeVlm.answer_key(_payload(frame, text=SCORE_PROMPT + " ")),
            FakeVlm.answer_key(_payload(frame, frame)),
            FakeVlm.answer_key(_payload(_jpeg(_frame(64, 48), 85)))}
    assert len(keys) == 4
    view = answer_view(_payload(frame))
    assert view["messages"][0]["content"][1] == {"type": "image_url",
                                                 "image_url": {"url": "pixels:128x96"}}
    # a text-only message keeps its text
    assert answer_view({"messages": [{"role": "user", "content": "hello"}]})["messages"][0]["content"] == "hello"


def test_the_judgement_reports_each_cameras_defects_as_a_pure_function_of_camera_and_window():
    """The per-camera block of the one request (D71, camera_defects): each camera's verdict and defect report
    are decided by its name and window, so every level, a missing item and a missing report all occur over a
    fixture."""
    def payload(camera, end):
        text = ("Assess the robot manipulation task from the supplied continuous videos. "
                f"Camera: {camera}. Same episode. Clip starts at episode time 0.0s; episode "
                f"window ends at {end}s. Add ONE more field \"cameras\" to the same JSON object.")
        return {"messages": [{"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA", "fps": 5}}]}]}

    fake = FakeVlm()
    out = json.loads(fake.answer(payload("cam_a", 6.5)))
    assert out["verdict"] == "failure" and set(out["cameras"]) == {"cam_a"}
    assert fake.answer(payload("cam_a", 6.5)) == fake.answer(payload("cam_a", 6.5))
    seen = set()
    for cam in ("cam_a", "cam_b", "wrist", "front", "top"):
        for tenths in range(60, 90):
            c = json.loads(fake.answer(payload(cam, tenths / 10)))["cameras"][cam]["camera_check"]
            for item in ("glitch", "contamination"):
                if item in c:
                    assert c[item]["times"] == ([] if c[item]["level"] == "none" else [[0.0, 0.5]])
            seen.add("missing" if not c else ("no-shake" if "shake" not in c else c["glitch"]["level"]))
    assert seen == {"missing", "no-shake", "none", "minor", "severe"}
