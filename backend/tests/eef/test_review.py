"""F5.6: the VLM review's windows, answer checks, repair turn, cache and CPU conflicts (design 12 §10)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import review as R

GOOD = {"review_status": "refute", "target_visible": True, "tracking_target_correct": "support",
        "position_support": "refute", "orientation_support": "uncertain",
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


# ----------------------------------------------------------------------------------- one point, one axis (F5.10)


def _sample():
    from curation.extensions.eef_consistency import load

    from . import synth

    r = load.load_bundle(json.dumps(synth.make_bundle([synth.make_entry(0, 45)])).encode(), check_media=False)
    assert r.ok
    return r.samples[0]


def _cam_detail(cov):
    return {"compared_points": list(cov), "observed_axes": ["finger_line"],
            "subitems": {C.POSITION: {"points": {p: {"coverage": {"coverage": c}} for p, c in cov.items()}}}}


def test_every_window_asks_about_one_point_and_at_most_one_axis():
    s = _sample()
    cam = _cam_detail({"tcp": 0.5, "finger_plus_y": 0.9, "finger_minus_y": 0.7})
    assert R.focus(cam, s.axes) == ("finger_plus_y", "finger_line")          # best-covered point, compared axis
    segs = [_seg("cam0", C.POSITION, 5, 20, point_id="tcp", peak=4.0),
            _seg("cam0", C.POSITION, 8, 22, point_id="finger_minus_y", peak=9.0),
            _seg("cam0", C.ORIENTATION, 25, 40, axis_id="finger_line", peak=12.0)]
    detail = {"segments": segs, "cameras": {"cam0": cam}}
    wins, _ = R.select_windows(detail, "cam0", np.ones(45, bool), 15.0, per_camera=3, frames_per_window=4,
                               axes=s.axes)
    pos = next(w for w in wins if w.subitem == C.POSITION)
    ori = next(w for w in wins if w.subitem == C.ORIENTATION)
    uni = [w for w in wins if w.kind == "uniform"]
    assert (pos.point_id, pos.axis_id) == ("finger_minus_y", "finger_line")   # the worst of the merged points
    assert (ori.point_id, ori.axis_id) == ("finger_minus_y", "finger_line")   # the axis and its start point
    assert uni and all((w.point_id, w.axis_id) == ("finger_plus_y", "finger_line") for w in uni)
    assert R.select_windows({"segments": [_seg("cam0", C.CAMERA_MOTION, 0, 30)], "cameras": {"cam0": cam}}, "cam0",
                            np.ones(45, bool), 15.0, per_camera=2, frames_per_window=3, axes=s.axes)[0][0].kind \
        == "uniform"                                                          # no camera-motion candidates


def test_the_prompt_defines_only_what_is_drawn():
    s = _sample()
    frames = {f: np.full((480, 640, 3), 90, np.uint8) for f in (3, 6, 9)}
    w = R.Window("cam0", "uniform", [3, 6, 9], point_id="tcp", axis_id="z")
    obs = {"tcp": np.full((45, 2), np.nan)}
    obs["tcp"][:] = [320.0, 240.0]
    lengths = {a: R._axis_length(s, "cam0", a, [3, 6, 9]) for a in s.axes}
    req = R.build_request(s, w, frames, obs, model="m")
    longest = max(lengths, key=lengths.get)
    assert w.axis_id == ("z" if lengths["z"] >= R.MIN_AXIS_PX else longest)    # a short arrow is replaced
    assert "- P = tcp: finger centre" in req.text and f"- A = {w.axis_id}: from " in req.text
    assert "eef_origin" not in req.text                                      # nothing that is not drawn
    assert "GREEN cross labelled P" in req.text and "RED arrow labelled A" in req.text
    assert [i["role"] for i in req.images] == ["context"] + ["raw", "marked"] * 3
    ori = R.Window("cam0", "candidate", [3, 6], subitem=C.ORIENTATION, point_id="tcp", axis_id="z")
    old_min = R.MIN_AXIS_PX
    try:
        R.MIN_AXIS_PX = 1e6                                                   # no arrow is long enough
        R.build_request(s, ori, frames, obs, model="m")
        assert ori.axis_id is None
        bare = R.build_request(s, R.Window("cam0", "uniform", [3], point_id="tcp"), frames, {}, model="m")
    finally:
        R.MIN_AXIS_PX = old_min
    assert "RED arrow" not in bare.text and "GREEN cross labelled" not in bare.text
    assert "there is no arrow: answer not_observable" in bare.text
    assert bare.key != R.build_request(s, R.Window("cam0", "uniform", [3], point_id="finger_plus_y"), frames, {},
                                       model="m").key                          # another point is another question
    assert "finger_minus_y" not in bare.text                                  # nothing that is not drawn


def test_votes_ignore_uncertain_and_not_observable():
    assert R.votes({**GOOD, "position_support": "refute", "orientation_support": "not_observable"}) == \
        {C.POSITION: "refute"}
    assert R.votes({**GOOD, "position_support": "uncertain", "orientation_support": "support"}) == \
        {C.ORIENTATION: "support"}
