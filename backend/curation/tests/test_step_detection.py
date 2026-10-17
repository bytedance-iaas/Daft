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
    assert [(x["cue"], x["frame"]) for x in c] == [("pause", 100), ("grasp", 180), ("release", 240)]
    # 25 still frames are 24 zero steps: 2.4 s
    assert c[0]["score"] == 0.8 and c[0]["dur_s"] == 2.4
    assert c[1]["score"] == 0.8 and c[1]["column"] == 6
    # the head and tail idles are not candidates; a longer reference lowers the pause's score
    assert SD.candidates(s, FPS, (6,), pause_ref_s=10.0)[0]["score"] == 0.24


def test_a_gripper_bounce_and_near_candidates_merge():
    s = _episode()
    s[182:184, 6] = 0.0                                   # a 0.2 s bounce right after the grasp
    s[110:112] = s[110] + 0.5                             # a hiccup inside the pause shorter than 0.3 s is closed
    c = SD.candidates(s, FPS, (6,))
    assert [(x["cue"], x["frame"]) for x in c] == [("pause", 100), ("grasp", 180), ("release", 240)]
    assert SD.gripper_events(np.zeros(50), FPS) == []    # a gripper that never moves has no events


def test_detect_reads_the_state_and_falls_back_on_the_command_for_a_still_gripper():
    s = _episode()
    a = s.copy()
    s[:, 6] = 0.0                                         # HABIT: the state's gripper column is all zeros
    r = SD.detect(a, s, FPS, (6,))
    d = r.detail
    assert d["stream"] == "state" and d["gripper_stream"] == "action" and d["n_candidates"] == 3
    assert d["cues"] == {"pause": 1, "grasp": 1, "release": 1} and d["n_reported"] == 3
    # no state: the command carries the body too; nothing at all: not assessed
    assert SD.detect(a, None, FPS, (6,)).detail["stream"] == "action"
    assert "reason" in SD.detect(None, None, FPS).detail


def test_findings_report_the_candidates_at_or_above_min_score():
    s = _episode()
    d = SD.detect(s, s, FPS, (6,), min_score=0.9).detail
    out = derive("step_detection", True, 1.0, d, {})
    assert out.findings == [] and out.readings["n_candidates"] == 3 and out.readings["n_reported"] == 0
    d = SD.detect(s, s, FPS, (6,), min_score=0.3).detail
    out = derive("step_detection", True, 1.0, d, {})
    assert [f["readings"]["cue"] for f in out.findings] == ["pause", "grasp", "release"]
    f = out.findings[0]
    assert f["item"] == "TASK-1" and f["readings"]["source"] == "detected" and f["time_s"] == [10.0, 10.0]
    assert f["message_zh"] == "候选边界 1：10.0 秒（停了 2.4 秒）" and out.findings[1]["message_zh"].endswith("（夹爪合上）")
    assert out.readings["n_candidates"] == 3 and out.readings["n_reported"] == 3 and out.readings["stream"] == "state"
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
