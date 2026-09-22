"""The fake model answers the same on every platform.

The JPEG bytes a frame encodes to differ between the macOS and Linux wheels
(PyAV/FFmpeg decode, OpenCV's libjpeg-turbo encode). The fake picks its answer
from the request's texts and each image's pixel size, never the bytes
(``FakeVlm.answer_key``), so a test gets the same verdicts on either.
"""
from __future__ import annotations

import shutil

import numpy as np
import requests

from curation.adapters import vlm_client
from parity.fakevlm import FakeVlm

from .fakevlm_server import FakeVlmServer
from .pipeline import comparable, read_jsonl, results, run

SCORE_PROMPT = "Rate this robot episode from 0 to 100. Score:"
ANNOTATION = "pick up the red block and place it in the bin"


def _frame(width: int = 128, height: int = 96, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def _payload(*urls: str, text: str = SCORE_PROMPT) -> dict:
    content = [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": u}} for u in urls]
    return {"model": "fake-vlm", "temperature": 0.0, "max_tokens": 16,
            "messages": [{"role": "user", "content": content}]}


def test_the_server_answers_a_re_encoded_request_alike():
    frame = _frame(seed=7)
    urls = [vlm_client._frame_to_data_uri(frame, quality=q) for q in (95, 85, 30)]
    assert len(set(urls)) == 3                            # other bytes ...
    with FakeVlmServer() as vlm:
        got = [requests.post(vlm.url + "/chat/completions", json=_payload(u), timeout=10)
               .json()["choices"][0]["message"]["content"] for u in urls]
    assert len(set(got)) == 1 and got[0].isdigit()        # ... the same answer
    assert len({FakeVlm.answer_key(_payload(u)) for u in urls}) == 1


def test_a_run_with_other_jpeg_bytes_reaches_the_same_verdicts(vlm_stage, tmp_path,
                                                              monkeypatch):
    """The VLM stage with every frame encoded at another JPEG quality: the requests
    carry other bytes, the records are the reference's."""
    rd = str(tmp_path / "run")
    shutil.copytree(vlm_stage["base"], rd)
    encode = vlm_client._frame_to_data_uri
    encoded = []

    def other_quality(frame, quality: int = 85):
        encoded.append(quality)
        return encode(frame, quality=60)

    monkeypatch.setattr(vlm_client, "_frame_to_data_uri", other_quality)
    with FakeVlmServer() as vlm:
        res = run("check", "--modules", "task_success", "--input", vlm_stage["dataset"],
                  "--run-dir", rd, "--episodes", vlm_stage["episodes"],
                  "--vlm-endpoint", vlm.url, "--vlm-model", "fake-vlm")
    assert res.rc == 0, res.doc
    assert encoded                                        # the frames were re-encoded
    got = results(rd, "task_success")
    assert {e: comparable(r) for e, r in got.items()} == \
        {e: comparable(r) for e, r in vlm_stage["reference"].items()}


def test_the_mini_dataset_walks_every_task_success_path(vlm_stage):
    """What the CLI tests build on (``parity.fakevlm.SEED``): abstentions, a pass that
    arbitration rescues, a plain pass, and captions other than the annotation."""
    got = {e: (r["verdict"], r["details"].get("verdict"))
           for e, r in vlm_stage["reference"].items()}
    assert got == {0: ("abstain", "uncertain"), 1: ("pass", "arbitration_success"),
                   3: ("abstain", "uncertain"), 4: ("pass", "arbitration_success"),
                   6: ("pass", "arbitration_success"), 7: ("abstain", "uncertain")}
    captions = {ln["episode_index"]: ln["caption"] for ln in
                read_jsonl(f"{vlm_stage['reference_dir']}/autolabel/captions.jsonl")}
    assert set(captions) == {4, 6} and ANNOTATION not in captions.values()
