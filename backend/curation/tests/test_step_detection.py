"""step_detection (registry 8.12): step boundary candidates from pauses and gripper events, TASK-1's detected track."""
from __future__ import annotations

import json

import numpy as np

from curation.pipeline import step_detection as SD
from curation.pipeline.findings import derive

FPS = 10.0


def _episode(T=300, pause=(100, 125), grasp=180, release=240, head=5, tail=8):
    """A 30 s state at 10 fps: six joints on slow sines, a pause of 2.5 s, a gripper (column 6) that closes at
    18 s and opens at 24 s, an idle head and tail (TASK-11's, not steps)."""
    t = np.arange(T) / FPS
    s = np.stack([np.sin(t * (0.6 + 0.2 * j)) for j in range(6)], axis=1)
    s[pause[0]:pause[1]] = s[pause[0]]
    s[:head] = s[head]
    s[-tail:] = s[-tail - 1]
    g = np.zeros((T, 1))
    g[grasp:release] = 1.0
    return np.concatenate([s, g], axis=1)


def test_candidates_are_the_pause_start_and_the_gripper_events():
    s = _episode()
    c = SD.candidates(s, FPS, (6,))
    assert [(x["cue"], x["frame"]) for x in c] == [("pause", 100), ("resume", 124), ("grasp", 180), ("release", 240)]
    # 25 still frames are 24 zero steps: 2.4 s; the resume (the body moving again) scores a quarter of its pause
    assert c[0]["score"] == 0.8 and c[0]["dur_s"] == 2.4 and c[1]["score"] == 0.2 and c[1]["dur_s"] == 2.4
    assert c[2]["score"] == 0.8 and c[2]["column"] == 6
    # the head and tail idles are not candidates; a longer reference lowers the pause's score
    assert SD.candidates(s, FPS, (6,), pause_ref_s=10.0)[0]["score"] == 0.24


def test_a_gripper_bounce_and_near_candidates_merge():
    s = _episode()
    s[182:184, 6] = 0.0                                   # a 0.2 s bounce right after the grasp
    s[110:112] = s[110] + 0.5                             # a hiccup inside the pause shorter than 0.3 s is closed
    c = SD.candidates(s, FPS, (6,))
    assert [x["cue"] for x in c] == ["pause", "resume", "grasp", "release"]
    assert abs(c[2]["frame"] - 180) <= 3 and c[3]["frame"] == 240   # the median filter settles the grasp within 0.3 s
    assert SD.gripper_events(np.zeros(50), FPS) == []    # a gripper that never moves has no events


def test_a_bouncing_gripper_reading_is_one_grasp_and_one_release():
    """HABIT ep 144: the gripper reads 0.59, 0.6, 1.0, 0.27, 0.37, 0.87, 0.96, 1.0 ... while closing and
    0.78, 0.39, 0.2, 0.72, 0.62, 0.14, 0.05, 0.0 ... while opening - one event each, not one per crossing."""
    g = np.zeros(400)
    g[80:91] = [0.59, 0.6, 1.0, 0.27, 0.37, 0.87, 0.96, 1.0, 0.78, 0.87, 0.94]
    g[91:200] = 0.94
    g[200:212] = [0.78, 0.39, 0.2, 0.72, 0.62, 0.14, 0.05, 0.0, 0.23, 0.13, 0.04, 0.0]
    ev = SD.gripper_events(g, FPS)
    assert [k for _, k in ev] == ["grasp", "release"] and abs(ev[0][0] - 82) <= 3 and abs(ev[1][0] - 203) <= 3


def test_a_column_that_never_moved_does_not_vote_on_the_pauses():
    """Galaxea push_in_chairs: a 4.5 mm torso reading that jumps once and a chassis odometry column, normalized
    by their tiny ranges, read as perpetual motion - 0 of 829 frames at rest, no pause ever. They get no vote."""
    s = _episode()
    torso = np.zeros((len(s), 1))
    torso[150] = 0.0045                                   # one blip over the whole episode
    jitter = np.random.default_rng(0).normal(0, 1e-4, size=(len(s), 1))   # a sensor's noise, range ~5e-4
    with_cols = np.concatenate([s, torso, jitter], axis=1)
    assert SD.voting_columns(with_cols) == list(range(6))                  # the six joints vote, the gripper (still until 18 s) and the two extra columns do not
    c = SD.candidates(with_cols, FPS, (6,))
    assert [(x["cue"], x["frame"]) for x in c][:2] == [("pause", 100), ("resume", 124)]
    assert SD.voting_columns(np.zeros((10, 2))) == []


def test_detect_reads_the_state_and_falls_back_on_the_command_for_a_still_gripper():
    s = _episode()
    a = s.copy()
    s[:, 6] = 0.0                                         # HABIT: the state's gripper column is all zeros
    r = SD.detect(a, s, FPS, (6,))
    d = r.detail
    assert d["stream"] == "state" and d["gripper_stream"] == "action" and d["n_candidates"] == 4
    assert d["cues"] == {"pause": 1, "resume": 1, "grasp": 1, "release": 1} and d["n_reported"] == 3   # the resume stays a reading
    # no state: the command carries the body too; nothing at all: not assessed
    assert SD.detect(a, None, FPS, (6,)).detail["stream"] == "action"
    assert "reason" in SD.detect(None, None, FPS).detail


def test_findings_report_the_candidates_at_or_above_min_score():
    s = _episode()
    d = SD.detect(s, s, FPS, (6,), min_score=0.9).detail
    out = derive("step_detection", True, 1.0, d, {})
    assert out.findings == [] and out.readings["n_candidates"] == 4 and out.readings["n_reported"] == 0
    d = SD.detect(s, s, FPS, (6,), min_score=0.3).detail
    out = derive("step_detection", True, 1.0, d, {})
    assert [f["readings"]["cue"] for f in out.findings] == ["pause", "grasp", "release"]
    f = out.findings[0]
    assert f["item"] == "TASK-1" and f["readings"]["source"] == "detected" and f["time_s"] == [10.0, 10.0]
    assert f["message_zh"] == "候选边界 1：10.0 秒（停了 2.4 秒）" and out.findings[1]["message_zh"].endswith("（夹爪合上）")
    assert out.readings["n_candidates"] == 4 and out.readings["n_reported"] == 3 and out.readings["stream"] == "state"
    # a task that lowers min_score reports the resume, worded as the body moving again
    low = derive("step_detection", True, 1.0, SD.detect(s, s, FPS, (6,), min_score=0.1).detail, {})
    assert low.findings[1]["message_zh"] == "候选边界 2：12.4 秒（停了 2.4 秒后动起来）" and low.findings[1]["readings"]["dur_s"] == 2.4
    assert not out.unassessable
    out2 = derive("step_detection", True, 1.0, {"reason": "没有可用的读数列或指令列"}, {})
    assert not out2.findings and out2.unassessable[0]["item"] == "TASK-1"


def test_gripper_dims_come_from_the_layout_else_the_profile():
    class _Reg:
        def get(self, _id):
            class P:
                gripper_dims = (5,)
            if _id == "so101":
                return P()
            raise KeyError(_id)
    assert SD.gripper_dims_of(json.dumps({"layout": {"gripper_dims": [0, 1]}}), "r1lite", _Reg()) == (0, 1)
    assert SD.gripper_dims_of("{}", "so101", _Reg()) == (5,)
    assert SD.gripper_dims_of("not json", "unknown", _Reg()) == ()
