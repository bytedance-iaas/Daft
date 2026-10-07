"""The single-pass protocol (video-task/2): one request judges the episode and every camera.

What these check is the part that is not the model: the answer's shape is read correctly, a camera
the model mishandled never costs the verdict, and the decision keeps rejection harder than passing
now that the per-camera answers are no longer an independent second signature.
"""
from __future__ import annotations

import json

import pytest

from curation.adapters.video_input import VideoClip
from curation.adapters.video_vlm import PROTOCOL_SINGLE, parse_assessment, parse_cameras
from curation.pipeline.funnel import single_pass
from curation.pipeline.video_task import judge_video_episode

FRONT = "observation.images.front"
WRIST = "observation.images.wrist"


def clip(name: str, end: float = 10.0) -> VideoClip:
    return VideoClip(camera=name, url="data:,", sha256="x", start_s=0.0, end_s=end,
                     frames=50, byte_size=1)


def answer(verdict="success", cameras=None, evidence=None) -> str:
    body = {"verdict": verdict, "task_type": "transient", "completion": 0.9,
            "reason": "the pen is in the mug",
            "evidence": evidence if evidence is not None else
            [{"camera": FRONT, "start_s": 1.0, "end_s": 2.0, "observation": "pen enters the mug"}]}
    if cameras is not None:
        body["cameras"] = cameras
    return json.dumps(body)


def cams(front="success", wrist="success") -> dict:
    def one(v):
        return {"verdict": v, "reason": "what this view shows",
                "camera_check": {"glitch": {"level": "none", "times": [], "note": ""},
                                 "shake": {"level": "none", "times": [], "note": ""},
                                 "contamination": {"level": "none", "kind": "none",
                                                   "times": [], "note": ""}}}
    return {FRONT: one(front), WRIST: one(wrist)}


# ---------------------------------------------------------------- the answer's shape


def test_every_supplied_camera_gets_an_entry_even_when_the_model_skips_one():
    clips = [clip(FRONT), clip(WRIST)]
    out = parse_assessment(answer(cameras={FRONT: cams()[FRONT]}), clips, per_camera=True)
    assert sorted(out["cameras"]) == sorted([FRONT, WRIST])
    assert out["cameras"][FRONT]["verdict"] == "success"
    assert out["cameras"][WRIST]["verdict"] == "unavail", "a camera left out votes for nothing"
    assert any("no entry" in p for p in out["cameras"][WRIST]["camera_check"]["problems"])


def test_a_shortened_camera_name_still_lands_on_its_camera():
    clips = [clip(FRONT), clip(WRIST)]
    out = parse_assessment(answer(cameras={"front": cams()[FRONT], "wrist": cams()[WRIST]}),
                           clips, per_camera=True)
    assert [e["verdict"] for e in out["cameras"].values()] == ["success", "success"]


def test_a_bad_per_camera_verdict_is_unavail_and_named_not_raised():
    clips = [clip(FRONT)]
    broken = {FRONT: {"verdict": "yes please", "reason": "", "camera_check": {}}}
    out = parse_assessment(answer(cameras=broken), clips, per_camera=True)
    assert out["cameras"][FRONT]["verdict"] == "unavail"
    assert any("is not success/failure/uncertain" in p
               for p in out["cameras"][FRONT]["camera_check"]["problems"])


def test_a_missing_cameras_block_is_an_error_so_it_costs_the_one_repair_round():
    with pytest.raises(ValueError, match="per-camera block"):
        parse_assessment(answer(), [clip(FRONT)], per_camera=True)


def test_each_camera_check_time_is_bounded_by_that_camera_window():
    clips = [clip(FRONT, end=4.0), clip(WRIST, end=30.0)]
    far = {"verdict": "success", "reason": "",
           "camera_check": {"glitch": {"level": "minor", "times": [[20.0, 25.0]], "note": ""},
                            "shake": {"level": "none", "times": [], "note": ""},
                            "contamination": {"level": "none", "kind": "none", "times": [],
                                              "note": ""}}}
    out = parse_cameras({FRONT: far, WRIST: far}, clips)
    assert out[FRONT]["camera_check"]["glitch"]["times"] == [], "outside the 4 s front window"
    assert out[WRIST]["camera_check"]["glitch"]["times"] == [[20.0, 25.0]]


def test_the_two_pass_shape_is_untouched():
    out = parse_assessment(answer(), [clip(FRONT)])
    assert sorted(out) == ["completion", "evidence", "reason", "task_type", "verdict"]


# ---------------------------------------------------------------- the decision


class _Scorer:
    media_input = "video"

    def __init__(self, text: str, clips):
        self.text, self.clips, self.calls = text, clips, 0

    def __call__(self, clips, instruction, *, hints=""):
        self.calls += 1
        return parse_assessment(self.text, self.clips, per_camera=True)


def _judge(verdict, front, wrist, monkeypatch):
    clips = [clip(FRONT), clip(WRIST)]
    monkeypatch.setattr("curation.pipeline.video_task.prepare_videos", lambda *a, **k: clips)
    scorer = _Scorer(answer(verdict, cams(front, wrist)), clips)
    reviewer = _Fail()
    res = judge_video_episode({}, {}, "put the pen in the mug", scorer, reviewer,
                              arb_deps={"video_judge": _Fail()}, single_pass=True)
    return res, scorer, reviewer


class _Fail:
    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("single pass must not send a second request")


def test_single_pass_sends_one_request_and_no_review_or_arbitration(monkeypatch):
    res, scorer, reviewer = _judge("success", "success", "success", monkeypatch)
    assert scorer.calls == 1 and reviewer.calls == 0
    assert res.detail["protocol"] == PROTOCOL_SINGLE
    assert res.passed is True
    assert res.detail["rules"] == ["video_single_pass_success"]


def test_a_camera_saying_failure_blocks_the_pass(monkeypatch):
    res, _s, _r = _judge("success", "success", "failure", monkeypatch)
    assert res.passed is None, "a contradicting camera sends it to a person, it does not pass"
    assert "video_single_pass_undecided" in res.detail["rules"]


def test_rejection_needs_the_episode_and_a_camera_to_say_failure(monkeypatch):
    res, _s, _r = _judge("failure", "failure", "uncertain", monkeypatch)
    assert res.passed is False
    assert res.detail["rules"] == ["video_single_pass_failure"]


def test_a_failure_verdict_no_camera_backs_goes_to_a_person(monkeypatch):
    res, _s, _r = _judge("failure", "uncertain", "uncertain", monkeypatch)
    assert res.passed is None


def test_a_camera_saying_success_blocks_the_rejection(monkeypatch):
    res, _s, _r = _judge("failure", "success", "failure", monkeypatch)
    assert res.passed is None, "rejection stays the harder direction"


def test_an_uncertain_episode_goes_to_a_person(monkeypatch):
    res, _s, _r = _judge("uncertain", "success", "success", monkeypatch)
    assert res.passed is None


def test_the_votes_and_camera_table_are_recorded_for_the_report(monkeypatch):
    res, _s, _r = _judge("success", "success", "uncertain", monkeypatch)
    assert res.detail["cam_votes"] == {FRONT: "yes", WRIST: "unclear"}
    assert res.detail["review"] == "yes"
    assert sorted(res.detail["cameras"]) == sorted([FRONT, WRIST])


# ---------------------------------------------------------------- the switch


@pytest.mark.parametrize("cfg,want", [
    ({}, False),
    ({"checks": {"task_success": {"vlm": {}}}}, False),
    ({"checks": {"task_success": {"vlm": {"single_pass": True}}}}, True),
    ({"checks": {"task_success": {"vlm": {"single_pass": False}}}}, False),
])
def test_the_switch_is_off_unless_the_config_asks_for_it(cfg, want):
    assert single_pass(cfg) is want
