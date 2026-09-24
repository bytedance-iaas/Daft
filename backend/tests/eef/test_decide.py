"""D-E12 / design 12 C.9: every episode ends as pass, reject or a person's card."""
from __future__ import annotations

from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import decide as D

CAM = "ext"


def _cpu(pos="ok", ori="ok", tem="ok", mot="ok", state="ok", reasons=()):
    return {"cameras": {CAM: {"subitems": {C.POSITION: {"status": pos, "reasons": list(reasons)},
                                            C.ORIENTATION: {"status": ori}, C.TEMPORAL: {"status": tem},
                                            C.CAMERA_MOTION: {"status": mot}}}},
            "state_motion": {"status": state}}


def _w(kind="uniform", sub=None, pos=None, ori=None, track="support", status="answered"):
    ans = {"position_support": pos or "uncertain", "orientation_support": ori or "not_observable",
           "tracking_target_correct": track}
    return {"kind": kind, "subitem": sub, "status": status, "answer": ans if status == "answered" else None}


def _rev(*windows):
    return {"cameras": {CAM: {"windows": list(windows)}}}


def test_all_ok_and_the_model_agrees_or_abstains_passes():
    d = D.decide(_cpu(), _rev(_w(pos="support"), _w(pos="uncertain"), _w(status="failed")))
    assert (d["outcome"], d["passed"], d["reason"]) == (D.PASS, True, "")
    assert D.decide(_cpu(), _rev())["outcome"] == D.PASS                     # nothing to ask, nothing wrong


def test_a_suspect_the_model_does_not_contradict_is_a_confirmed_reject():
    d = D.decide(_cpu(pos="suspect"), _rev(_w("candidate", C.POSITION, pos="refute"),
                                           _w("candidate", C.POSITION, pos="support"), _w(pos="support")))
    assert d["outcome"] == D.REJECT and d["passed"] is False               # a tie follows the CPU
    assert d["confirmed"][0]["votes"] == {"support": 1, "refute": 1} and "位置" in d["reason"]


def test_conflicts_go_to_a_person():
    d = D.decide(_cpu(pos="suspect"), _rev(_w("candidate", C.POSITION, pos="support"),
                                           _w("candidate", C.POSITION, pos="support"),
                                           _w("candidate", C.POSITION, pos="refute")))
    assert d["outcome"] == D.HUMAN and d["passed"] is None and d["human"][0]["code"] == "conflict"
    d = D.decide(_cpu(), _rev(_w(pos="refute"), _w(pos="refute"), _w(pos="support")))
    assert d["outcome"] == D.HUMAN and d["human"][0]["cpu"] == C.OK
    one_off = D.decide(_cpu(), _rev(_w(pos="refute"), _w(pos="support")))   # no majority: follow the CPU
    assert one_off["outcome"] == D.PASS


def test_no_model_opinion_on_a_suspect_goes_to_a_person():
    d = D.decide(_cpu(ori="suspect"), _rev(_w("candidate", C.ORIENTATION, ori="uncertain"),
                                           _w("candidate", C.ORIENTATION, status="failed")))
    assert d["outcome"] == D.HUMAN and d["human"][0]["code"] == "no_model_opinion"


def test_what_the_model_cannot_see_goes_to_a_person_when_suspect():
    for kw, sub in (({"tem": "suspect"}, C.TEMPORAL), ({"mot": "suspect"}, C.CAMERA_MOTION),
                    ({"state": "suspect"}, C.STATE_MOTION)):
        d = D.decide(_cpu(**kw), _rev(_w(pos="support")))
        assert d["outcome"] == D.HUMAN and d["human"][0] == {**d["human"][0], "code": "model_cannot_see",
                                                             "subitem": sub}


def test_a_confirmed_defect_wins_over_open_questions():
    d = D.decide(_cpu(pos="suspect", tem="suspect"), _rev(_w("candidate", C.POSITION, pos="refute")))
    assert d["outcome"] == D.REJECT and d["human"][0]["code"] == "model_cannot_see"


def test_cannot_judge():
    assert D.decide(None, None)["human"][0]["code"] == "not_in_file"
    d = D.decide(_cpu(pos="unknown", reasons=["coverage_insufficient"]), _rev())
    assert d["outcome"] == D.HUMAN and d["human"][0]["reasons"] == ["coverage_insufficient"]
    part = D.decide(_cpu(ori="unknown", tem="unsupported"), _rev(_w(pos="support")))
    assert part["outcome"] == D.PASS and {u["subitem"] for u in part["unchecked"]} == {C.ORIENTATION, C.TEMPORAL}


def test_a_green_cross_on_the_wrong_target_voids_the_cpu_reading():
    d = D.decide(_cpu(pos="suspect"), _rev(_w("candidate", C.POSITION, pos="refute", track="refute"),
                                           _w(pos="refute", track="refute"), _w(track="support")))
    assert d["outcome"] == D.HUMAN and [h["code"] for h in d["human"]][0] == "tracking_suspect"
    assert not d["confirmed"]
