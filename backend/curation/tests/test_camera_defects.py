"""camera_defects: the advisory record built from task_success's per-camera reviews."""
from __future__ import annotations

import json

from curation.adapters.video_vlm import CAMERA_CHECK_PROTOCOL
from curation.extensions import camera_defects as CD


def _review(check=None, error=None):
    if error:
        return {"error": error}
    out = {"verdict": "success", "task_type": "transient", "completion": 0.9, "reason": "r", "evidence": []}
    if check is not None:
        out["camera_check"] = check
    return out


def _check(glitch="none", shake="none", contamination="none", kind="none", times=None):
    return {"glitch": {"level": glitch, "times": times or [], "note": ""},
            "shake": {"level": shake, "times": [], "note": ""},
            "contamination": {"level": contamination, "kind": kind, "times": [], "note": ""}, "problems": []}


def _task(reviews, cams=None):
    return {"passed": None, "score": None,
            "detail": json.dumps({"cams": cams or sorted(reviews), "video_reviews": reviews})}


def test_all_cameras_clean():
    s = CD.struct_from_task(_task({"a": _review(_check()), "b": _review(_check())}))
    assert s["passed"] is None and s["score"] is None
    d = json.loads(s["detail"])
    assert d["protocol"] == CAMERA_CHECK_PROTOCOL and d["reason"] == "" and d["clean_ratio"] == 1.0
    assert d["items"] == {"glitch": "none", "shake": "none", "contamination": "none"}
    assert d["per_camera"]["a"]["answered"] is True


def test_worst_camera_wins_and_kinds_are_kept():
    s = CD.struct_from_task(_task({
        "a": _review(_check(glitch="minor", times=[[1.0, 1.5]])),
        "b": _review(_check(glitch="severe", contamination="minor", kind="dirt"))}))
    d = json.loads(s["detail"])
    assert d["items"]["glitch"] == "severe" and d["per_camera"]["a"]["glitch"]["times"] == [[1.0, 1.5]]
    assert d["items"]["contamination"] == "minor"
    assert {c: e["contamination"]["kind"] for c, e in d["per_camera"].items()} == {"a": "none", "b": "dirt"}


def test_unknown_when_the_review_failed_or_the_model_did_not_answer():
    d = json.loads(CD.struct_from_task(_task({"a": _review(error="boom"), "b": _review()}))["detail"])
    assert d["items"] == {k: "unknown" for k in CD.CAMERA_CHECK_ITEMS}
    assert d["per_camera"]["a"]["error"] == "boom" and d["per_camera"]["b"]["answered"] is False
    assert d["reason"] == "模型未回答 camera_check" and d["clean_ratio"] is None
    d = json.loads(CD.struct_from_task({"passed": None, "score": None, "detail": json.dumps({"cams": ["a"]})})["detail"])
    assert d["reason"].startswith("未运行逐机位复核")
    d = json.loads(CD.struct_from_task(None)["detail"])
    assert d["reason"].startswith("任务成败判定没有产生结果") and set(d["items"]) == set(CD.CAMERA_CHECK_ITEMS)


def test_summary_and_table_rows():
    recs = {0: {"verdict": "abstain", "details": json.loads(CD.struct_from_task(_task(
                {"a": _review(_check(glitch="severe")), "b": _review()}))["detail"])},
            1: {"verdict": "abstain", "details": json.loads(CD.struct_from_task(None)["detail"])}}
    s = CD.summary(recs)
    assert s["glitch"] == {"none": 0, "minor": 0, "severe": 1, "unknown": 1}
    assert s["episodes_with_severe"] == 1 and s["cameras"] == 2 and s["cameras_unanswered"] == 1
    rows = CD.table_rows(recs)
    assert [(r["episode_index"], r["camera"], r["glitch"]) for r in rows] == [(0, "a", "severe"), (0, "b", "unknown"), (1, "", "unknown")]
