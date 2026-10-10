"""Design doc 25 §6-§7 (D81, D82): channels give a verdict and a confidence per sub-item and camera; the merge takes
the largest, marks a conflict, caps a single source and voids the CPU where the cross tracks something else."""
from __future__ import annotations

import pytest

from curation.extensions.eef_consistency import channels as CH
from curation.extensions.eef_consistency import combine as CB
from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import profile as PR

CAM = "ext"
PROF = PR.load("demo")
CFG = CB.settings(PROF)


def _pos(status="ok", median_px=1.0, coverage=0.9):
    return {"status": status, "reasons": ["position_sustained_residual"] if status == "suspect" else [],
            "points": {"tcp": {"status": status, "metrics": {"median_px": median_px, "p95_px": median_px * 2},
                               "coverage": {"coverage": coverage}}}}


def _cpu(pos=None, ori="ok", tem="ok", mot="ok", state="ok", segments=(), lag_frames=0.0):
    return {"cameras": {CAM: {"subitems": {
        C.POSITION: pos or _pos(),
        C.ORIENTATION: {"status": ori, "axes": {"z": {"status": ori, "metrics": {"median_deg": 1.0},
                                                       "coverage": {"requested": 100, "valid": 90}}}},
        C.TEMPORAL: {"status": tem, "reasons": ["time_offset"] if tem == "suspect" else [],
                     "metrics": {"lag_frames": lag_frames, "lag_s": lag_frames / 15}},
        C.CAMERA_MOTION: {"status": mot, "metrics": {"hf_rms_max_px": 0.5}}}}},
        "state_motion": {"status": state, "metrics": {"hf_pos_rms_max_mm": 0.5, "spike_count": 0}},
        "segments": list(segments)}


def _seg(peak, sub=C.POSITION):
    return {"subitem": sub, "camera_id": CAM, "peak": peak, "start_s": 2.0, "end_s": 4.0, "evidence_frames": [40]}


def _w(kind="uniform", sub=None, pos=None, ori=None, track="support", status="answered"):
    ans = {"position_support": pos or "uncertain", "orientation_support": ori or "not_observable",
           "tracking_target_correct": track}
    return {"kind": kind, "subitem": sub, "status": status, "answer": ans if status == "answered" else None}


def _merge(cpu, *windows, **kw):
    return CB.merge(cpu=CH.cpu_channel(cpu, PROF), review=CH.review_channel({"cameras": {CAM: {"windows": list(windows)}}}, cpu),
                    cfg=CFG, **kw)


def _cell(merged, sub, cam=CAM):
    return next(c for c in merged["cells"] if c["subitem"] == sub and c["camera"] == cam)


def test_p_is_aligned_with_the_bands_as_far_as_the_evidence_goes():
    """An issue is at least "possibly inconsistent", an ok never gets there - two weak oks merged stay consistent."""
    assert CH.p_of(CH.ISSUE, 0.0) == 0.4 and CH.p_of(CH.ISSUE, 0.5) == 0.7 and CH.p_of(CH.ISSUE, 1.0) == 1.0
    assert CH.p_of(CH.OK, 1.0) == 0.0 and CH.p_of(CH.OK, 0.4) == 0.24 and CH.p_of(CH.OK, 0.01) < 0.4
    assert CH.p_of(CH.OK, 0.0) is None and CH.p_of(CH.CANNOT_TELL, None) is None     # an ok that says nothing
    assert CH.p_of(CH.ISSUE, 0.0, low=0.3) == 0.3                                    # the task's own band
    assert CH.strength(10.0, 10.0) == 0.0 and CH.strength(30.0, 10.0) == 1.0 and CH.strength(20.0, 10.0) == 0.5
    assert CB.label_of(0.7, CFG) == CB.INCONSISTENT and CB.label_of(0.4, CFG) == CB.POSSIBLY
    assert CB.label_of(0.39, CFG) == CB.CONSISTENT and CB.label_of(None, CFG) == CB.CANNOT_TELL


def test_everything_fine_and_the_model_agrees_is_consistent():
    m = _merge(_cpu(), _w(pos="support"), _w(pos="support"))
    pos = _cell(m, C.POSITION)
    assert pos["label"] == CB.CONSISTENT and pos["flags"] == [] and pos["p"] == pytest.approx(0.04)
    assert m["episode"]["label"] == CB.CONSISTENT and m["episode"]["conflicts"] == 0


def test_a_strong_cpu_suspect_the_model_confirms_is_inconsistent_without_a_card():
    cpu = _cpu(pos=_pos("suspect", median_px=12.0), segments=[_seg(30.0)])
    m = _merge(cpu, _w("candidate", C.POSITION, pos="refute"), _w("candidate", C.POSITION, pos="refute"))
    pos = _cell(m, C.POSITION)
    assert pos["sources"]["cpu"]["verdict"] == CH.ISSUE and pos["sources"]["vlm_review"]["verdict"] == CH.ISSUE
    assert pos["label"] == CB.INCONSISTENT and CB.CONFLICT not in pos["flags"] and pos["time_s"] == [2.0, 4.0]
    assert m["episode"]["subitem"] == C.POSITION and m["episode"]["reason"].startswith("不一致 · ")


def test_two_sides_saying_opposite_things_is_a_conflict_and_p_is_the_larger():
    cpu = _cpu(pos=_pos("suspect", median_px=12.0), segments=[_seg(30.0)])
    m = _merge(cpu, _w("candidate", C.POSITION, pos="support"), _w("candidate", C.POSITION, pos="support"))
    pos = _cell(m, C.POSITION)
    assert pos["flags"] == [CB.CONFLICT] and pos["p"] == pos["sources"]["cpu"]["p"] >= CFG["high"]
    assert pos["sources"]["vlm_review"]["p"] < CFG["low"] and "冲突" in m["episode"]["reason"]
    # the CPU fine everywhere and the model refuting on every window: a conflict too, p is the model's
    m = _merge(_cpu(), _w(pos="refute"), _w(pos="refute"))
    pos = _cell(m, C.POSITION)
    assert pos["flags"] == [CB.CONFLICT] and pos["p"] == 1.0
    # a mild disagreement is no conflict: the larger p, "possibly"
    m = _merge(_cpu(), _w(pos="refute"), _w(pos="refute"), _w(pos="support"))
    pos = _cell(m, C.POSITION)
    assert pos["flags"] == [] and pos["label"] == CB.POSSIBLY


def test_what_the_model_cannot_see_is_a_single_source_capped():
    m = _merge(_cpu(tem="suspect", lag_frames=8.0), _w(pos="support"))
    tem = _cell(m, C.TEMPORAL)
    assert tem["flags"] == [CB.SINGLE_SOURCE] and tem["missing"] == CB.MODEL_CANNOT_SEE
    assert tem["p"] == CFG["single_source_cap"] and tem["label"] == CB.INCONSISTENT
    state = _cell(m, C.STATE_MOTION, None)
    assert state["flags"] == [CB.SINGLE_SOURCE] and state["label"] == CB.CONSISTENT
    assert "只有一个渠道：这一项模型看不了" in m["episode"]["reason"]


def test_a_cross_on_the_wrong_target_voids_the_cpu():
    cpu = _cpu(pos=_pos("suspect", median_px=12.0), segments=[_seg(30.0)])
    m = _merge(cpu, _w("candidate", C.POSITION, pos="support", track="refute"), _w(track="refute"), _w(track="support"))
    pos = _cell(m, C.POSITION)
    assert CB.TRACKING_INVALID in pos["flags"] and pos["missing"] == CB.TRACKING_INVALID
    assert pos["p"] < CFG["low"]                                   # only the model's word stands


def test_no_answer_and_ties_cannot_tell_and_nothing_at_all_cannot_judge():
    cpu = _cpu(pos=_pos("unknown"), ori="unknown", tem="unknown", mot="unknown", state="unknown")
    m = _merge(cpu, _w(status="failed"))
    assert m["episode"]["label"] == CB.CANNOT_TELL and m["episode"]["p"] is None
    assert m["episode"]["reason"].startswith("判断不了")
    tie = CH.review_channel({"cameras": {CAM: {"windows": [_w(pos="refute"), _w(pos="support")]}}}, _cpu())[0]
    assert tie[(C.POSITION, CAM)]["verdict"] == CH.CANNOT_TELL and tie[(C.POSITION, CAM)]["why"] == "tie"
    # the model switched off: the CPU alone, capped, says why
    m = CB.merge(cpu=CH.cpu_channel(_cpu(), PROF), cfg=CFG, vlm_missing=CB.VLM_OFF)
    assert _cell(m, C.POSITION)["missing"] == CB.VLM_OFF


def test_a_single_source_says_whether_the_model_was_asked_answered_or_took_no_side():
    """"The model did not answer" only when it did not: an answer without a side is "unsure", a suspect stretch no
    window looked at is "not asked"; a CPU that measured and could not tell is not "does not measure"."""
    unsure = _merge(_cpu(), _w(), _w())                                    # every window: uncertain
    assert _cell(unsure, C.POSITION)["missing"] == CB.MODEL_UNSURE
    assert "只有一个渠道：模型拿不准" in CB.grounds(_cell(unsure, C.POSITION))
    failed = _merge(_cpu(), _w(status="failed"))
    assert _cell(failed, C.POSITION)["missing"] == CB.MODEL_NO_ANSWER
    suspect = _cpu(pos=_pos("suspect", median_px=12.0), segments=[_seg(30.0)])
    not_asked = _merge(suspect, _w(pos="support"))                         # uniform windows only, no candidate
    assert _cell(not_asked, C.POSITION)["missing"] == CB.NOT_ASKED
    cpu = _cpu(pos=_pos("unknown"))
    cpu["cameras"][CAM]["subitems"][C.POSITION]["reasons"] = ["coverage_insufficient"]
    blind = _merge(cpu, _w(pos="refute"), _w(pos="refute"))
    assert _cell(blind, C.POSITION)["missing"] == CB.CPU_UNSURE and _cell(blind, C.POSITION)["label"] == CB.INCONSISTENT


def test_the_whole_clip_opinion_uses_the_models_own_confidence():
    op = {"cameras": {CAM: {"status": "answered", "clips": [{"status": "answered"}],
                            "segments": [{"aspect": "position", "confidence": 0.85, "start_s": 1.0, "end_s": 2.0,
                                          "evidence_frames": [20]}]},
                      "wrist": {"status": "skipped"}, "top": {"status": "failed", "clips": [{"status": "failed"}]}}}
    cells = CH.opinion_channel(op)
    assert cells[(C.POSITION, CAM)]["p"] == pytest.approx(0.82) and cells[(C.ORIENTATION, CAM)]["p"] == 0.0
    assert (C.POSITION, "wrist") not in cells and cells[(C.POSITION, "top")]["verdict"] == CH.CANNOT_TELL
    m = CB.merge(opinion=cells, cfg=CFG)
    pos = _cell(m, C.POSITION)
    assert pos["p"] == CFG["single_source_cap"] and pos["missing"] == CB.NO_REFERENCE and pos["time_s"] == [1.0, 2.0]
    # half the clips answered and nothing found: towards the middle, never "inconsistent"
    half = CH.opinion_channel({"cameras": {CAM: {"status": "partial", "clips": [{"status": "answered"}, {"status": "failed"}],
                                                 "segments": []}}})
    assert half[(C.POSITION, CAM)]["p"] == 0.2
    # a handheld gripper's prompt asks about the approach axis only
    assert set(CH.opinion_channel(op, umi=True)) == {(C.ORIENTATION, CAM), (C.ORIENTATION, "top")}


def test_a_wrist_cameras_own_motion():
    ego = {"cameras": {"r0": {"status": "ok", "metrics": {"coverage": 0.97}},
                       "r1": {"status": "suspect", "metrics": {"coverage": 0.97},
                              "segments": [{"reason": "time_offset", "magnitude": 0.467, "lag_s": 0.467, "band": "moderate",
                                            "band_level": 2, "start_s": 1.7, "end_s": 8.3, "evidence_frames": [120]}]},
                       "r2": {"status": "unknown", "reason": "pictures_unmatched"}}}
    cells = CH.ego_channel(ego)
    assert cells[(C.EGO_MOTION, "r0")]["p"] == pytest.approx(0.012)
    assert cells[(C.EGO_MOTION, "r1")]["verdict"] == CH.ISSUE and cells[(C.EGO_MOTION, "r1")]["p"] > 0.9
    assert cells[(C.EGO_MOTION, "r2")]["why"] == "pictures_unmatched"
    m = CB.merge(ego=cells, cfg=CFG)
    r1 = _cell(m, C.EGO_MOTION, "r1")
    assert r1["label"] == CB.INCONSISTENT and r1["p"] == CFG["single_source_cap"] and r1["sources"]["ego"]
    assert m["episode"]["camera"] == "r1"


def test_the_grounds_name_the_supported_diagnosis():
    cpu = _cpu(pos=_pos("suspect", median_px=12.0), segments=[_seg(30.0)])
    m = CB.merge(cpu=CH.cpu_channel(cpu, PROF), cfg=CFG, vlm_missing=CB.VLM_OFF,
                 hypotheses=[{"hypothesis": "extrinsics_error", "camera_id": CAM, "supported": True}])
    assert _cell(m, C.POSITION)["supported_hypotheses"] == ["extrinsics_error"]
    assert "诊断支持：extrinsics_error" in m["episode"]["reason"]


def test_two_weak_oks_stay_consistent():
    """dataset2's orientation: the CPU fine on a fifth of the frames, the model one support out of six windows - the
    larger of two weak oks is still consistent, never "possibly inconsistent"."""
    cpu = _cpu()
    cpu["cameras"][CAM]["subitems"][C.ORIENTATION]["axes"]["z"]["coverage"] = {"requested": 100, "valid": 18}
    m = _merge(cpu, _w(ori="support"), *[_w()] * 5)
    ori = _cell(m, C.ORIENTATION)
    assert ori["sources"]["cpu"]["p"] < CFG["low"] and ori["sources"]["vlm_review"]["p"] < CFG["low"]
    assert ori["label"] == CB.CONSISTENT and ori["flags"] == []
