"""F5.6: the EEF VLM review on the CLI under a fixed tape (design doc 12 §10, §13.4).

On the mini dataset with a seed on every frame the EEF module finds episode 0 ok and episodes 1-2
suspect (episode 1 with a candidate segment). The review then runs one episode per call against the
parity fake model, scripted per episode to cover every branch: a refuted ok (conflict), a supported
candidate (conflict), a malformed answer fixed by the repair turn, a timeout, a frame that is not in
the request (twice: failed), a measured value in the explanation (repaired). The calls are recorded
on a tape and replayed offline in a fresh run directory: the records come out the same.
"""
from __future__ import annotations

import json
import os
import re
import shutil

import pytest
import requests

from .pipeline import results
from .test_eef_check import CAM, EEF, _files

REVIEW = "eef_video_review"
URL = "http://fake-vlm.test/v1"
GOOD = {"review_status": "uncertain", "target_visible": True, "tracking_target_correct": "support",
        "position_support": "uncertain", "orientation_support": "not_observable",
        "background_motion_support": "support", "offset_direction": "none", "offset_magnitude_class": "none",
        "evidence_frame_ids": [], "reason_codes": [], "explanation": "看不太清"}


def _frames(payload: dict) -> list[int]:
    text = payload["messages"][0]["content"][0]["text"]
    return [int(x) for x in re.findall(r"\d+", text.split("Frames ", 1)[1].split("(", 1)[0])]


def _answer(payload: dict, **kw) -> str:
    return json.dumps({**GOOD, "evidence_frame_ids": _frames(payload)[:1], **kw}, ensure_ascii=False)


class Script:
    """Answers by call number within one review call; ``None`` falls back to a cautious answer."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.n = 0

    def __call__(self, payload: dict) -> str:
        if "You review whether a robot trajectory" not in json.dumps(payload):
            return "pong"
        step = self.steps[self.n] if self.n < len(self.steps) else None
        self.n += 1
        if step is None:
            return _answer(payload)
        if isinstance(step, BaseException):
            raise step
        return step(payload) if callable(step) else step


SCRIPTS = {
    # uniform windows on an ok camera, refuted: a conflict each, evidence kept
    0: lambda: Script(*[lambda p: _answer(p, review_status="refute", position_support="refute",
                                         offset_direction="left", offset_magnitude_class="within_finger_width")] * 6),
    # the candidate first (the model finds it fine: conflict), after a malformed answer and its repair
    1: lambda: Script("Sure! The red circle looks fine.", lambda p: _answer(p, review_status="support",
                                                                               position_support="support")),
    # a timeout; a frame not in the request, twice; a measured value, then fixed
    2: lambda: Script(requests.exceptions.ReadTimeout("fake: no answer in time"),
                      lambda p: _answer(p, evidence_frame_ids=[99999]),
                      lambda p: _answer(p, evidence_frame_ids=[99998]),
                      lambda p: _answer(p, explanation="红圈偏左约 2 cm"),
                      lambda p: _answer(p, explanation="红圈偏左一指宽")),
}


def _review(cli, dataset, rd, traj, episodes, *extra):
    res = cli("check", "--modules", REVIEW, "--input", dataset, "--run-dir", rd, "--episodes", episodes,
              "--param", f"{EEF}.trajectory_json={traj}", "--param", f"{REVIEW}.review_windows_per_camera=3",
              "--param", f"{REVIEW}.review_frames_per_window=3", "--vlm-endpoint", URL, "--vlm-model", "fake-vlm",
              "--retry", "0", *extra)
    assert res.rc == 0, res.doc
    return res.doc["modules"][REVIEW]


def _hooks(mode, **kw):
    from parity import vlm_tape as T

    from curation.adapters import vlm_client

    hooks = T.TapeHooks(mode, **kw)
    hooks.install(vlm_client)
    return hooks


def _details(rd):
    out = {}
    for e, r in results(rd, REVIEW).items():
        d = dict(r["details"])
        for cam in d.get("cameras", {}).values():
            for w in cam.get("windows", []):
                w.pop("cache_hit", None)
        out[e] = (r["verdict"], d)
    return out


@pytest.fixture(scope="module")
def taped(tmp_path_factory, mini_dataset):
    """The EEF module on episodes 0-2, then the scripted reviews, recorded."""
    from parity.fakevlm import FakeVlm

    from curation.cli import main

    tmp = tmp_path_factory.mktemp("eef-review")
    rd, traj = str(tmp / "run"), _files(tmp, seed_every=1)
    assert main(["check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-2",
                 "--param", f"{EEF}.trajectory_json={traj}", "--json"]) == 0
    base = str(tmp / "base")
    shutil.copytree(rd, base)
    return {"rd": rd, "base": base, "traj": traj, "tape": str(tmp / "tape.jsonl.gz"), "fake": FakeVlm("fake-vlm")}


def test_every_branch_under_a_recorded_tape_and_its_offline_replay(cli, taped, mini_dataset):
    from parity import vlm_tape as T

    fake, rd, traj, ds = taped["fake"], taped["rd"], taped["traj"], mini_dataset
    hooks = _hooks("record", tape_out=taped["tape"], transport=fake.transport())
    try:
        docs = {}
        for ep in (0, 1, 2):
            fake.answer = SCRIPTS[ep]()
            docs[ep] = _review(cli, ds, rd, traj, str(ep), "--set",
                               "checks.task_success.vlm.timeouts_s.eef_review=5")
        fake.answer = Script()
        docs["rest"] = _review(cli, ds, rd, traj, "3,7")
    finally:
        hooks.uninstall()
    recs = results(rd, REVIEW)
    assert sorted(recs) == [0, 1, 2, 3, 7]
    assert all(r["passed"] is None and r["score"] is None and r["gate"] == "none" and r["verdict"] == "abstain"
               for r in recs.values())
    d0, d1, d2 = (recs[e]["details"] for e in (0, 1, 2))
    # episode 0: the CPU says ok, the model refutes -> conflicts, a person looks, evidence kept
    assert d0["status"] == "completed" and d0["needs_human"] and d0["summary"]["conflicts"] == 3
    assert {c["subitem"] for c in d0["conflicts"]} == {"position_2d"} and d0["conflicts"][0]["cpu"] == "ok"
    assert d0["evidence"] and all(os.path.isfile(os.path.join(rd, p)) for p in d0["evidence"])
    # episode 1: the candidate the model finds fine is a conflict; the malformed answer was repaired
    cand = [w for w in d1["cameras"][CAM]["windows"] if w["kind"] == "candidate"]
    assert cand and cand[0]["conflict"] == {"subitem": "position_2d", "cpu": "suspect", "vlm": "support"}
    assert cand[0]["attempts"] == 2 and d1["status"] == "completed" and d1["needs_human"]
    # episode 2: a timeout and an unknown frame fail their windows; the measured value was repaired
    w2 = d2["cameras"][CAM]["windows"]
    assert [w["status"] for w in w2] == ["failed", "failed", "answered"]
    assert [w.get("failure", {}).get("code") for w in w2[:2]] == ["timeout", "unknown_frame"]
    assert w2[2]["attempts"] == 2 and w2[2]["answer"]["explanation"] == "红圈偏左一指宽"
    assert d2["status"] == "incomplete" and d2["summary"]["failed"] == 2 and not d2["needs_human"]
    # no answer carries a measurement: classes, booleans and frame ids only
    for d in (d0, d1, d2):
        for w in d["cameras"][CAM]["windows"]:
            for k, v in (w.get("answer") or {}).items():
                assert isinstance(v, (str, bool)) or (isinstance(v, list) and all(isinstance(x, (int, str)) for x in v))
    assert recs[3]["details"]["reasons"] == ["base_missing"] and recs[7]["details"]["reasons"] == ["projection_missing"]
    assert docs[2]["episodes"]["error"] == 0                       # a failed window is not an execution error
    usage = [json.loads(x) for x in open(os.path.join(rd, "usage.jsonl"))]
    assert {u["call_kind"] for u in usage if u.get("module") == REVIEW} == {"eef_review"}

    # the tape replays offline into a fresh run directory: the same records
    _, entries = T.read_tape(taped["tape"])
    fresh = str(os.path.dirname(taped["rd"])) + "/replay"
    shutil.copytree(taped["base"], fresh)
    hooks = _hooks("replay", replay_entries=entries)
    try:
        for ep in ("0", "1", "2", "3,7"):
            _review(cli, ds, fresh, traj, ep, *(["--set", "checks.task_success.vlm.timeouts_s.eef_review=5"]
                                               if ep != "3,7" else []))
    finally:
        hooks.uninstall()
    got, want = _details(fresh), _details(rd)
    for e in want:
        for d in (got[e][1], want[e][1]):
            d.pop("evidence", None)
    assert got == want


def test_answers_are_cached_and_resume_skips_current_lines(cli, taped, mini_dataset):
    from parity.fakevlm import FakeVlm

    rd, traj, ds = taped["rd"], taped["traj"], mini_dataset
    fake = FakeVlm("fake-vlm")

    def no_review(payload):
        assert "You review whether" not in json.dumps(payload), "answered from the cache, not asked"
        return "pong"

    fake.answer = no_review
    hooks = _hooks("record", tape_out=os.path.join(os.path.dirname(rd), "tape2.jsonl.gz"), transport=fake.transport())
    try:
        again = _review(cli, ds, rd, traj, "0")                     # a new part, every answer from the cache
        resumed = _review(cli, ds, rd, traj, "0-2", "--resume")
    finally:
        hooks.uninstall()
    assert again["episodes"]["abstain"] == 1
    wins = results(rd, REVIEW)[0]["details"]["cameras"][CAM]["windows"]
    assert all(w["cache_hit"] and w["attempts"] == 0 for w in wins)
    assert resumed["skipped_existing"] == 3


def test_usage_errors(cli, mini_dataset, tmp_path):
    rd = str(tmp_path / "run")
    res = cli("check", "--modules", REVIEW, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0",
              "--vlm-endpoint", URL, "--vlm-model", "fake-vlm")
    assert res.rc != 0 and f"{EEF}.trajectory_json" in res.doc["error"]["message"]
    mixed = cli("check", "--modules", f"task_success,{REVIEW}", "--input", mini_dataset, "--run-dir", rd,
                "--episodes", "0")
    assert mixed.rc != 0 and "call of their own" in mixed.doc["error"]["message"]
