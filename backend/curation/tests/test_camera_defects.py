"""camera_defects: the advisory record built from task_success's per-camera reviews."""
from __future__ import annotations

import json

from curation.adapters.video_vlm import CAMERA_CHECK_PROTOCOL
from curation.extensions import camera_defects as CD

CLEAN = {"glitch": "none", "shake": "none", "contamination": "none", "occlusion": "none"}


def _review(check=None, error=None):
    if error:
        return {"error": error}
    out = {"verdict": "success", "task_type": "transient", "completion": 0.9, "reason": "r", "evidence": []}
    if check is not None:
        out["camera_check"] = check
    return out


def _view(kind="fixed", arm="single", matches="yes", same_as=""):
    return {"kind": kind, "arm": arm, "matches_name": matches, "same_as": same_as, "note": ""}


def _check(glitch="none", shake="none", contamination="none", kind="none", times=None,
           occlusion="none", source="none", share=0.0, view=None):
    return {"glitch": {"level": glitch, "times": times or [], "note": ""},
            "shake": {"level": shake, "times": [], "note": ""},
            "contamination": {"level": contamination, "kind": kind, "times": [], "note": ""},
            "occlusion": {"level": occlusion, "source": source, "times": [], "note": "", "share": share},
            "view": view or _view(), "problems": []}


def _task(reviews, cams=None):
    return {"passed": None, "score": None,
            "detail": json.dumps({"cams": cams or sorted(reviews), "cameras": reviews})}


def test_all_cameras_clean():
    s = CD.struct_from_task(_task({"a": _review(_check()), "b": _review(_check())}))
    assert s["passed"] is None and s["score"] is None
    d = json.loads(s["detail"])
    assert d["protocol"] == CAMERA_CHECK_PROTOCOL and d["reason"] == "" and d["clean_ratio"] == 1.0
    assert d["items"] == CLEAN and d["identity"] == "consistent"
    assert d["per_camera"]["a"]["answered"] is True
    assert d["per_camera"]["a"]["view"] == _view()


def test_worst_camera_wins_and_kinds_are_kept():
    s = CD.struct_from_task(_task({
        "a": _review(_check(glitch="minor", times=[[1.0, 1.5]], occlusion="minor", source="arm", share=0.2)),
        "b": _review(_check(glitch="severe", contamination="minor", kind="dirt"))}))
    d = json.loads(s["detail"])
    assert d["items"]["glitch"] == "severe" and d["per_camera"]["a"]["glitch"]["times"] == [[1.0, 1.5]]
    assert d["items"]["contamination"] == "minor" and d["items"]["occlusion"] == "minor"
    assert {c: e["contamination"]["kind"] for c, e in d["per_camera"].items()} == {"a": "none", "b": "dirt"}
    assert d["per_camera"]["a"]["occlusion"] == {"level": "minor", "times": [], "note": "", "source": "arm", "share": 0.2}


def test_identity_follows_the_cameras_views():
    """One camera that contradicts its name, or duplicates another, makes the episode inconsistent;
    same_as must name another supplied camera (exact or a unique dotted suffix) or it reads empty."""
    d = json.loads(CD.struct_from_task(_task({
        "observation.images.wrist": _review(_check(view=_view(kind="fixed", matches="no"))),
        "observation.images.front": _review(_check())}))["detail"])
    assert d["identity"] == "inconsistent"
    assert d["per_camera"]["observation.images.wrist"]["view"]["matches_name"] == "no"
    d = json.loads(CD.struct_from_task(_task({
        "observation.images.a": _review(_check(view=_view(same_as="b"))),
        "observation.images.b": _review(_check())}))["detail"])
    assert d["identity"] == "inconsistent"
    assert d["per_camera"]["observation.images.a"]["view"]["same_as"] == "observation.images.b"
    d = json.loads(CD.struct_from_task(_task({
        "a": _review(_check(view=_view(same_as="nowhere"))), "b": _review(_check(view=_view(matches="unknown")))}))["detail"])
    assert d["identity"] == "consistent" and d["per_camera"]["a"]["view"]["same_as"] == ""
    d = json.loads(CD.struct_from_task(_task({"a": _review(_check(view=_view(matches="unknown")))}))["detail"])
    assert d["identity"] == "unknown"
    # a record from before the view existed: unknown, never an error
    old = {k: v for k, v in _check().items() if k != "view"}
    d = json.loads(CD.struct_from_task(_task({"a": _review(old)}))["detail"])
    assert d["identity"] == "unknown" and d["per_camera"]["a"]["view"]["kind"] == "unknown"


def test_unknown_when_the_review_failed_or_the_model_did_not_answer():
    d = json.loads(CD.struct_from_task(_task({"a": _review(error="boom"), "b": _review()}))["detail"])
    assert d["items"] == {k: "unknown" for k in CD.CAMERA_CHECK_ITEMS} and d["identity"] == "unknown"
    assert d["per_camera"]["a"]["error"] == "boom" and d["per_camera"]["b"]["answered"] is False
    assert d["reason"] == "模型未回答 camera_check" and d["clean_ratio"] is None
    d = json.loads(CD.struct_from_task({"passed": None, "score": None, "detail": json.dumps({"cams": ["a"]})})["detail"])
    assert d["reason"].startswith("判定请求没有按相机作答")
    d = json.loads(CD.struct_from_task(None)["detail"])
    assert d["reason"].startswith("任务成败判定没有产生结果") and set(d["items"]) == set(CD.CAMERA_CHECK_ITEMS)


def test_summary_and_table_rows():
    recs = {0: {"verdict": "abstain", "details": json.loads(CD.struct_from_task(_task(
                {"a": _review(_check(glitch="severe", view=_view(matches="no"))), "b": _review()}))["detail"])},
            1: {"verdict": "abstain", "details": json.loads(CD.struct_from_task(None)["detail"])}}
    s = CD.summary(recs)
    assert s["glitch"] == {"none": 0, "minor": 0, "severe": 1, "unknown": 1}
    assert s["occlusion"] == {"none": 1, "minor": 0, "severe": 0, "unknown": 1}
    assert s["identity"] == {"consistent": 0, "inconsistent": 1, "unknown": 1}
    assert s["episodes_with_severe"] == 1 and s["cameras"] == 2 and s["cameras_unanswered"] == 1
    rows = CD.table_rows(recs)
    assert [(r["episode_index"], r["camera"], r["glitch"], r["occlusion"], r["identity"], r["view_matches_name"]) for r in rows] == \
        [(0, "a", "severe", "none", "inconsistent", "no"), (0, "b", "unknown", "unknown", "inconsistent", "unknown"),
         (1, "", "unknown", "unknown", "unknown", "")]
