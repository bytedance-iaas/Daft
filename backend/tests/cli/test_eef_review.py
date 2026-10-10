"""The EEF module's channels and merge under a fixed tape (design doc 25 §6-§7, D81, D82; review F5.10).

On the mini dataset with a seed on every frame the CPU finds episode 0 ok, episode 1 suspect with a
candidate segment and episode 2 suspect without one. The module then runs one episode per call against
the parity fake model, scripted per episode: a CPU ok the model refutes (a conflict: the only card), a CPU
candidate the model confirms after a malformed answer and its repair (inconsistent, both channels), a
suspect with no model opinion while its windows time out, cite frames not in the request or give a measured
value (the CPU alone, capped), an ok the model does not contradict (consistent) and an episode the file does
not declare (cannot tell). The calls are recorded on a tape and replayed offline in a fresh run directory:
the records come out the same; aggregate rejects nothing and asks the conflict.
"""
from __future__ import annotations

import json
import os
import re

import pytest
import requests

from .pipeline import codes_of, passed_of, read_jsonl, results, verdict_of
from .test_eef_check import CAM, EEF, URL, _files

GOOD = {"review_status": "uncertain", "target_visible": True, "tracking_target_correct": "support",
        "position_support": "uncertain", "orientation_support": "not_observable",
        "offset_direction": "none", "offset_magnitude_class": "none",
        "evidence_frame_ids": [], "reason_codes": [], "explanation": "看不太清"}
TIMEOUT = ("--set", "checks.task_success.vlm.timeouts_s.eef_review=5")


def _frames(payload: dict) -> list[int]:
    text = payload["messages"][0]["content"][0]["text"]
    return [int(x) for x in re.findall(r"\d+", text.split("Frames ", 1)[1].split("(", 1)[0])]


def _answer(payload: dict, **kw) -> str:
    return json.dumps({**GOOD, "evidence_frame_ids": _frames(payload)[:1], **kw}, ensure_ascii=False)


class Script:
    """Answers by call number within one call; ``None`` falls back to a cautious answer."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.n = 0

    def __call__(self, payload: dict) -> str:
        if "You review ONE point P" not in json.dumps(payload):
            return "pong"
        step = self.steps[self.n] if self.n < len(self.steps) else None
        self.n += 1
        if step is None:
            return _answer(payload)
        if isinstance(step, BaseException):
            raise step
        return step(payload) if callable(step) else step


def _refute(p):
    return _answer(p, review_status="refute", position_support="refute", offset_direction="left",
                   offset_magnitude_class="within_finger_width")


SCRIPTS = {
    0: lambda: Script(*[_refute] * 6),                       # CPU ok, the model refutes: a conflict
    1: lambda: Script("Sure! The red circle looks off.", _refute),   # candidate confirmed after a repair
    2: lambda: Script(requests.exceptions.ReadTimeout("fake: no answer in time"),   # no candidate: the CPU alone
                      lambda p: _answer(p, evidence_frame_ids=[99999]),
                      lambda p: _answer(p, evidence_frame_ids=[99998]),
                      lambda p: _answer(p, explanation="红圈偏左约 2 cm"),
                      lambda p: _answer(p, explanation="红圈偏左一指宽")),
    3: lambda: Script(),                                      # CPU ok, the model does not object: consistent
    7: lambda: Script(),                                      # not in the file: cannot tell
}
#: the episode's label and its findings (design doc 25 §7): only episode 0 asks a person
OUTCOME = {0: ("inconsistent", ["conflict"]), 1: ("inconsistent", ["inconsistent", "inconsistent"]),
           2: ("inconsistent", ["inconsistent"]), 3: ("consistent", []), 7: ("cannot_tell", [])}


def _check(cli, dataset, rd, traj, episodes, *extra):
    res = cli("check", "--modules", EEF, "--input", dataset, "--run-dir", rd, "--episodes", episodes,
              "--param", f"{EEF}.trajectory_json={traj}", "--param", f"{EEF}.review_windows_per_camera=3",
              "--param", f"{EEF}.review_frames_per_window=3", "--vlm-endpoint", URL, "--vlm-model", "fake-vlm",
              "--retry", "0", *TIMEOUT, *extra)
    assert res.rc == 0, res.doc
    return res.doc["modules"][EEF]


def _hooks(mode, **kw):
    from parity import vlm_tape as T

    from curation.adapters import vlm_client

    hooks = T.TapeHooks(mode, **kw)
    hooks.install(vlm_client)
    return hooks


def _details(rd):
    out = {}
    for e, r in results(rd, EEF).items():
        d = json.loads(json.dumps(r["details"]))
        d.pop("evidence", None)
        d.pop("timing", None)
        rv = d.get("review") or {}
        rv.pop("elapsed_s", None)
        for cam in rv.get("cameras", {}).values():
            for w in cam.get("windows", []):
                w.pop("cache_hit", None)
        for cam in d.get("cameras", {}).values():
            cam.pop("timing", None)
        out[e] = (verdict_of(r), passed_of(r), d)
    return out


@pytest.fixture(scope="module")
def taped(tmp_path_factory):
    from parity.fakevlm import FakeVlm

    tmp = tmp_path_factory.mktemp("eef-verdict")
    return {"rd": str(tmp / "run"), "traj": _files(tmp, seed_every=1), "tape": str(tmp / "tape.jsonl.gz"),
            "fake": FakeVlm("fake-vlm"), "tmp": tmp}


def test_every_branch_under_a_recorded_tape_and_its_offline_replay(cli, taped, mini_dataset):
    from parity import vlm_tape as T

    fake, rd, traj, ds = taped["fake"], taped["rd"], taped["traj"], mini_dataset
    hooks = _hooks("record", tape_out=taped["tape"], transport=fake.transport())
    try:
        for ep in SCRIPTS:
            fake.answer = SCRIPTS[ep]()
            doc = _check(cli, ds, rd, traj, str(ep))
            assert doc["episodes"]["error"] == 0                # a failed window is not an execution error
    finally:
        hooks.uninstall()
    recs = results(rd, EEF)
    assert sorted(recs) == sorted(SCRIPTS)
    for ep, (label, codes) in OUTCOME.items():
        d = recs[ep]["details"]
        assert d["merged"]["episode"]["label"] == label and codes_of(recs[ep]) == codes, (ep, d["merged"]["episode"])
        assert passed_of(recs[ep]) is not False                           # nothing is rejected (D81)
    d0, d1, d2 = (recs[e]["details"] for e in (0, 1, 2))
    pos = {e: next(c for c in recs[e]["details"]["merged"]["cells"] if c["subitem"] == "position_2d") for e in (0, 1, 2, 3)}
    assert pos[0]["flags"] == ["conflict"] and pos[0]["sources"]["cpu"]["verdict"] == "ok"
    assert pos[0]["sources"]["vlm_review"]["verdict"] == "issue" and pos[0]["p"] == 1.0
    assert d0["review"]["conflicts"] and recs[0]["evidence"] and all(os.path.isfile(os.path.join(rd, p))
                                                                      for p in recs[0]["evidence"])
    cand = [w for w in d1["review"]["cameras"][CAM]["windows"] if w["kind"] == "candidate"]
    assert cand and cand[0]["attempts"] == 2 and cand[0]["point_id"] == "block_center"
    assert pos[1]["flags"] == [] and {s["verdict"] for s in pos[1]["sources"].values()} == {"issue"}
    assert "位置" in d1["reason"] and "诊断支持：time_offset" in d1["reason"]
    w2 = d2["review"]["cameras"][CAM]["windows"]
    assert [w["status"] for w in w2] == ["failed", "failed", "answered"]
    assert [w.get("failure", {}).get("code") for w in w2[:2]] == ["timeout", "unknown_frame"]
    assert w2[2]["attempts"] == 2 and w2[2]["answer"]["explanation"] == "红圈偏左一指宽"
    assert d2["review"]["status"] == "incomplete"
    assert pos[2]["flags"] == ["single_source"] and pos[2]["missing"] == "not_asked" and pos[2]["p"] == 0.8
    assert pos[3]["label"] == "consistent" and pos[3]["missing"] == "model_unsure"      # every window said uncertain
    # a card shows every window's marked crops (F5.11) - and a conflict is the only card now (design doc 25 §7.3)
    assert not any(w.get("evidence") for w in w2)
    assert all(w["evidence"] and all(os.path.isfile(os.path.join(rd, p)) for p in w["evidence"])
               for w in d0["review"]["cameras"][CAM]["windows"])
    assert not any(w.get("evidence") for w in recs[3]["details"]["review"]["cameras"][CAM]["windows"])
    assert recs[7]["details"]["merged"]["episode"]["reason"] == "判断不了：trajectory.json 里没有这一条"
    for d in (d0, d1, d2):                                    # classes, booleans and frame ids only
        for w in d["review"]["cameras"][CAM]["windows"]:
            for v in (w.get("answer") or {}).values():
                assert isinstance(v, (str, bool)) or (isinstance(v, list) and all(isinstance(x, (int, str)) for x in v))
    usage = [json.loads(x) for x in open(os.path.join(rd, "usage.jsonl"))]
    assert {u["call_kind"] for u in usage if u.get("module") == EEF} == {"eef_review"}

    # the tape replays offline into a fresh run directory: the same records
    _, entries = T.read_tape(taped["tape"])
    video_requests = [e["request"]["body"]["messages"][0]["content"] for e in entries
                      if e.get("request", {}).get("body")
                      and "You review ONE point P" in json.dumps(e["request"]["body"])]
    assert video_requests
    for content in video_requests:
        assert sum(c["type"] == "video_url" for c in content) == 2  # RAW + MARKED
        assert not any(c["type"] == "image_url" for c in content)
    fresh = str(taped["tmp"] / "replay")
    hooks = _hooks("replay", replay_entries=entries)
    try:
        for ep in SCRIPTS:
            _check(cli, ds, fresh, traj, str(ep))
    finally:
        hooks.uninstall()
    assert _details(fresh) == _details(rd)


def test_aggregate_rejects_nothing_and_asks_the_conflict(cli, taped, mini_dataset):
    """D81: the module's opinions reject nothing; its conflict is asked on eef_check."""
    rd = taped["rd"]
    res = cli("aggregate", "--run-dir", rd, "--phase", "funnel", "--revision", "1", "--episodes", "0-3,7",
              "--modules", EEF)
    assert res.rc == 0, res.doc
    lines = {x["episode_index"]: x for x in read_jsonl(os.path.join(rd, "revisions", "r0001", "verdicts.jsonl"))}
    assert all(lines[e]["verdict"] == "keep" for e in (0, 1, 2, 3, 7))
    assert [(r["code"], r["line"]) for r in lines[0]["review"]] == [("conflict", "eef_check")]
    assert all(not lines[e]["review"] for e in (1, 2, 3, 7))


def test_answers_are_cached_and_resume_skips_current_lines(cli, taped, mini_dataset):
    from parity.fakevlm import FakeVlm

    rd, traj, ds = taped["rd"], taped["traj"], mini_dataset
    fake = FakeVlm("fake-vlm")

    def no_review(payload):
        assert "You review ONE point P" not in json.dumps(payload), "answered from the cache, not asked"
        return "pong"

    fake.answer = no_review
    hooks = _hooks("record", tape_out=str(taped["tmp"] / "tape2.jsonl.gz"), transport=fake.transport())
    try:
        again = _check(cli, ds, rd, traj, "0")                  # a new part, every answer from the cache
        resumed = _check(cli, ds, rd, traj, "0-3", "--resume")
    finally:
        hooks.uninstall()
    assert again["episodes"] == {"total": 1, "ok": 1, "error": 0} and again["findings"] == {"conflict": 1}
    wins = results(rd, EEF)[0]["details"]["review"]["cameras"][CAM]["windows"]
    assert all(w["cache_hit"] and w["attempts"] == 0 for w in wins)
    assert resumed["skipped_existing"] == 4
