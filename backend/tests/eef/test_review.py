"""F5.6: the VLM review's windows, answer checks, repair turn, cache and CPU conflicts (design 12 §10)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import review as R

GOOD = {"review_status": "refute", "target_visible": True, "tracking_target_correct": "support",
        "position_support": "refute", "orientation_support": "uncertain", "background_motion_support": "support",
        "offset_direction": "left", "offset_magnitude_class": "one_to_two_finger_widths",
        "evidence_frame_ids": [10, 12], "reason_codes": [], "explanation": "红圈在夹爪左侧"}


def _detail(*segments):
    return {"segments": list(segments)}


def _seg(cam, sub, a, b, **kw):
    return {"camera_id": cam, "subitem": sub, "start_frame": a, "end_frame": b, **kw}


def test_overlapping_segments_of_one_subitem_are_one_candidate():
    segs = [_seg("ext", C.POSITION, 10, 30, point_id="tcp", evidence_frames=[20], reasons=["a"]),
            _seg("ext", C.POSITION, 25, 40, point_id="tip", evidence_frames=[33], reasons=["b"]),
            _seg("ext", C.POSITION, 60, 70, point_id="tcp"), _seg("ext", C.ORIENTATION, 12, 28, axis_id="z")]
    merged = R.merge_segments(segs)
    assert [(m["subitem"], m["start_frame"], m["end_frame"]) for m in merged] == \
        [(C.POSITION, 10, 40), (C.POSITION, 60, 70), (C.ORIENTATION, 12, 28)]
    assert merged[0]["targets"] == ["tcp", "tip"] and merged[0]["evidence_frames"] == [20, 33]
    assert merged[0]["reasons"] == ["a", "b"] and segs[0]["end_frame"] == 30          # inputs untouched


def test_windows_take_the_longest_candidates_and_spread_uniform_ones():
    mask = np.zeros(300, bool)
    mask[20:280] = True
    detail = _detail(_seg("ext", C.POSITION, 50, 60, evidence_frames=[55]), _seg("ext", C.ORIENTATION, 100, 180),
                     _seg("ext", C.CAMERA_MOTION, 200, 205), _seg("ext", C.POSITION, 210, 290),
                     _seg("wrist", C.POSITION, 0, 299), _seg("ext", C.STATE_MOTION, 0, 10))
    wins, truncated = R.select_windows(detail, "ext", mask, 15.0, per_camera=2, frames_per_window=4)
    cands = [w for w in wins if w.kind == "candidate"]
    assert truncated                                             # three ext candidates, room for two
    assert [(w.subitem, w.segment["start_frame"]) for w in cands] == [(C.ORIENTATION, 100), (C.POSITION, 210)]
    assert all(len(w.frames) == 4 and w.frames == sorted(w.frames) for w in cands)
    uni = [w for w in wins if w.kind == "uniform"]
    assert len(uni) == 2 and all(1 <= len(w.frames) <= 4 and all(mask[f] for f in w.frames) for w in uni)
    wins, truncated = R.select_windows(_detail(_seg("ext", C.POSITION, 50, 60, evidence_frames=[57, 51])), "ext",
                                       mask, 15.0, per_camera=3, frames_per_window=3)
    assert not truncated and wins[0].frames[:2] == [51, 57] or 57 in wins[0].frames and 51 in wins[0].frames
    assert R.select_windows(_detail(), "ext", np.zeros(10, bool), 15.0, per_camera=3, frames_per_window=3) == ([], False)


@pytest.mark.parametrize("text, code", [
    ("not json at all", "malformed_json"),
    ("[1, 2]", "malformed_json"),
    (json.dumps({**GOOD, "offset_px": 12}), "schema_violation"),
    (json.dumps({**GOOD, "review_status": "maybe"}), "schema_violation"),
    (json.dumps({**GOOD, "evidence_frame_ids": [10, 99]}), "unknown_frame"),
    (json.dumps({**GOOD, "explanation": "向左偏了约 3 cm"}), "measured_value"),
    (json.dumps({**GOOD, "explanation": "offset is 12px"}), "measured_value"),
    (json.dumps({**GOOD, "explanation": "转了 15 度"}), "measured_value"),
])
def test_answers_are_refused_for_each_reason(text, code):
    answer, problem = R.check_answer(text, [10, 12, 14])
    assert answer is None and problem["code"] == code


def test_a_fenced_valid_answer_passes_and_mentions_of_frames_are_fine():
    ok, problem = R.check_answer("```json\n" + json.dumps({**GOOD, "explanation": "第 12 帧最明显，偏两指宽"}) + "\n```",
                                 [10, 12])
    assert problem is None and ok["position_support"] == "refute"


class _Ask:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req, history):
        self.calls.append(list(history))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _req(frames=(10, 12)):
    return R.Request(R.Window("ext", "uniform", list(frames)), "prompt", [], list(frames), key="k" * 64)


def test_one_repair_turn_then_failed(tmp_path):
    cache = R.Cache(str(tmp_path / "cache"))
    ask = _Ask("oops", json.dumps(GOOD))
    got = R.ask_window(_req(), ask, cache)
    assert got["status"] == R.ANSWERED and got["attempts"] == 2
    assert ask.calls[1][0]["content"] == "oops" and "malformed_json" in ask.calls[1][1]["content"]
    again = R.ask_window(_req(), _Ask(), cache)                          # cached: no request at all
    assert again == {"status": R.ANSWERED, "answer": GOOD, "attempts": 0, "cache_hit": True}
    bad = R.ask_window(R.Request(_req().window, "p", [], [10, 12], "x" * 64),
                       _Ask(json.dumps({**GOOD, "evidence_frame_ids": [7]}), "{}"), cache)
    assert bad["status"] == R.FAILED and bad["attempts"] == 2 and bad["failure"]["code"] == "schema_violation"
    timeout = R.ask_window(R.Request(_req().window, "p", [], [10, 12], "y" * 64),
                           _Ask(R.ReviewCallError("timeout", "no answer in 60 s")), cache)
    assert timeout["status"] == R.FAILED and timeout["failure"]["code"] == "timeout"
    assert R.Cache(str(tmp_path / "cache")).get("y" * 64) is None     # failures are not cached


def test_conflicts_with_the_cpu():
    cand = R.Window("ext", "candidate", [1, 2], subitem=C.POSITION)
    assert R.conflict(cand, {**GOOD, "position_support": "support"}, {}) == \
        {"subitem": C.POSITION, "cpu": C.SUSPECT, "vlm": "support"}
    assert R.conflict(cand, {**GOOD, "position_support": "uncertain"}, {}) is None
    assert R.conflict(cand, GOOD, {}) is None                            # both say off: agreement
    uni = R.Window("ext", "uniform", [1, 2])
    cells = {C.POSITION: {"status": C.OK}, C.ORIENTATION: {"status": C.UNKNOWN}}
    assert R.conflict(uni, GOOD, cells) == {"subitem": C.POSITION, "cpu": C.OK, "vlm": "refute"}
    assert R.conflict(uni, {**GOOD, "position_support": "support", "orientation_support": "refute"}, cells) is None


def test_summary_status():
    ans = {"status": R.ANSWERED, "answer": GOOD, "attempts": 1, "conflict": {"subitem": C.POSITION}}
    wrong = {"status": R.ANSWERED, "answer": {**GOOD, "review_status": "uncertain",
                                              "tracking_target_correct": "refute"}, "attempts": 0, "cache_hit": True}
    failed = {"status": R.FAILED, "failure": {"code": "timeout"}, "attempts": 1}
    status, s = R.summarize([ans, wrong], False)
    assert status == R.COMPLETED and s["conflicts"] == 1 and s["tracking_suspect"] == 1 and s["refute"] == 1
    assert s["uncertain"] == 1 and s["requests"] == 1 and s["cache_hits"] == 1
    status, s = R.summarize([ans, failed], True)
    assert status == R.INCOMPLETE and s["failed"] == 1 and s["truncated"]
    assert R.summarize([], False)[0] == R.NOT_REVIEWED
