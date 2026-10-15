"""The chart-ready aggregates of report.json's module summaries (06 §6.2, F6.2).

Unit tests of ``pipeline.report_stats`` on records shaped like the ones the checks write
(details copied from a real run of the synthetic fixture), plus the rules the report
page relies on: stable series, statistics only - never a list of episodes - and
tolerance of older details that lack keys.
"""
from __future__ import annotations

import json
from functools import partial

from curation.pipeline import report_stats as S
from curation.pipeline.records import record_v2


def rec(ep: int, verdict: str, details: dict | None = None, *, score=None, error=None,
        m: str = "task_success") -> dict:
    """A record 2.0 of module ``m`` whose verdict word is ``verdict``, derived from ``details`` the way
    ``check`` derives it."""
    passed = {"pass": True, "fail": False}.get(verdict)
    return record_v2(m, ep, passed, score, details or {}, error=error, evidence=[], elapsed_s=0.1)


rec_ts = partial(rec, m="timestamp_check")
rec_kin = partial(rec, m="kinematic_limits")
rec_mq = partial(rec, m="motion_quality")
rec_vq = partial(rec, m="visual_quality")
rec_sync = partial(rec, m="video_action_sync")


def names(series: list[dict]) -> dict[str, int]:
    return {x["name"]: x["count"] for x in series}


def no_episode_lists(summary: dict) -> None:
    """Statistics only: no value names episodes (no ``epNNNNNN`` ids, no index lists)."""
    text = json.dumps(summary, ensure_ascii=False)
    assert "ep0000" not in text and "episodes\": [" not in text


def test_series_keeps_the_given_order_and_zeros():
    assert S.series({"b": 2, "a": 1, "z": 0}, ("a", "c"), keep_zero=True) == [
        {"name": "a", "count": 1}, {"name": "c", "count": 0}, {"name": "b", "count": 2}]
    assert S.series({"x": 1, "y": 3}) == [{"name": "y", "count": 3}, {"name": "x", "count": 1}]
    assert S.top_series({str(i): i + 1 for i in range(8)}, 2) == [
        {"name": "7", "count": 8}, {"name": "6", "count": 7}, {"name": "其它", "count": 21}]


def test_score_hist_has_ten_stable_bins():
    hist = S.score_hist([0.0, 0.3, 0.6, 0.7, 0.95, 1.0, None, float("nan"), "x"])
    assert [b["name"] for b in hist][:2] == ["0.0–0.1", "0.1–0.2"] and hist[-1]["name"] == "0.9–1.0"
    assert [b["count"] for b in hist] == [1, 0, 0, 1, 0, 0, 1, 1, 0, 2]


def test_duration_hist_picks_round_bins():
    hist = S.duration_hist([11.0, 12.5, 30.0, 31.9])
    assert len(hist) <= 12 and hist[0]["name"] == "10–12" and sum(b["count"] for b in hist) == 4
    assert S.duration_hist([4.0, 4.0]) == [{"name": "4–4.5", "count": 2}]
    assert S.duration_hist([]) == []


def test_reason_heads_group_reasons_that_differ_in_numbers():
    assert S.reason_head("末态物证 0.38 在灰区(0.25~0.45),证据不足以硬判") == "末态物证 … 在灰区"
    assert S.reason_head("末态物证 0.41 在灰区(0.25~0.45),证据不足以硬判") == "末态物证 … 在灰区"
    assert S.reason_head("单位疑似错配:数据典型幅值(p95) 12.3 vs 极限幅值 2.9") == "单位疑似错配"
    assert S.reason_head(None) == "未注明"


def test_generic_counts_abstentions_and_failed_steps():
    records = [rec(0, "abstain", {"reason": "末态物证 0.38 在灰区(…)"}),
               rec(1, "abstain", {"reason": "末态物证 0.40 在灰区(…)"}),
               rec(2, "error", error={"kind": "execution", "incidents": [
                   {"step": "arbitration", "cause": "timeout"}, {"step": "arbitration"}]}),
               rec(3, "error", error={"kind": "execution", "incidents": [{"step": "decode"}]}),
               rec(4, "scored", score=0.8, m="motion_quality")]
    out = S.generic(records, [0.8])
    assert names(out["abstain_reason_counts"]) == {"末态物证 … 在灰区": 2}
    assert names(out["error_steps"]) == {"arbitration": 1, "decode": 1}
    assert "score_hist" not in out          # report 2.0: one per 0-1 reading (reporting.finding_stats)
    assert S.generic([rec(0, "pass")], []) == {}


def test_timestamp_failures_by_kind_and_durations():
    records = [
        rec_ts(0, "pass", {"n": 75, "duration_s": 4.93, "dt_nominal": 0.0667, "max_dt": 0.0667,
                        "jitter_ratio": 0.0}),
        rec_ts(1, "fail", {"n": 75, "duration_s": 5.53, "dt_nominal": 0.0667, "max_dt": 0.67,
                        "gap_frames": [{"frame": 36, "dt": 0.67}], "reason": "第 37 帧后间隔突增"}),
        rec_ts(2, "fail", {"n": 6, "duration_s": 0.33, "reason": "全长只有 0.33 秒"}),
        rec_ts(3, "fail", {"n": 40, "reason": "时间戳出现倒退/重复(第 3 帧)", "frame": 2, "ts": [0.1, 0.1]}),
        rec_ts(4, "fail", {"n": 80, "duration_s": 5.3, "dt_nominal": 0.0667, "max_dt": 0.1,
                        "jitter_ratio": 0.2}),
        rec_ts(5, "fail", {"reason": "只有 1 个时间戳,连时长都算不出"}),
    ]
    out = S.timestamp_stats(records)
    assert out["fail_reasons"] == [{"name": "out_of_order", "count": 1}, {"name": "gap", "count": 1},
                                   {"name": "fragment", "count": 1}, {"name": "jitter", "count": 1},
                                   {"name": "other", "count": 1}]
    assert out["duration_total_s"] == 16.1 and out["duration_min_s"] == 0.33
    assert out["duration_max_s"] == 5.53 and sum(b["count"] for b in out["duration_hist"]) == 4
    assert S.timestamp_stats([rec_ts(0, "pass")])["fail_reasons"][0] == {"name": "out_of_order", "count": 0}


def test_kinematic_violations_by_type_and_joint():
    records = [
        rec_kin(0, "fail", {"n_violations": 3, "profile": "franka", "violations": [
            {"type": "joint_limit", "joint": 3, "frame": 12, "value": 2.5, "limit": [-2, 2]},
            {"type": "joint_limit", "joint": 3, "frame": 13, "value": 2.6, "limit": [-2, 2]},
            {"type": "velocity_limit", "joint": 10, "frame": 5, "value": 9.0, "limit": 2.0}]}),
        rec_kin(1, "fail", {"mode": "ee", "profile": "franka", "violations": [
            {"type": "ee_reach", "joint": "xyz", "frame": 1, "value": 2.0, "limit": 1.1}]}),
        rec_kin(2, "pass", {"n_violations": 0, "violations": [], "profile": "franka"}),
        rec_kin(3, "abstain", {"reason": "单位疑似错配:…", "unit_mismatch": True}),
    ]
    out = S.kinematic_stats(records)
    assert out["violation_episodes"] == 2 and out["limits_profile"] == "franka"
    assert names(out["violations_by_type"]) == {"joint_limit": 1, "velocity_limit": 1, "ee_reach": 1}
    assert [x["name"] for x in out["violations_by_joint"]] == ["3", "10", "xyz"]


def test_motion_subscores_stuck_and_idle():
    base = {"smoothness": 0.95, "spike": 1.0, "gripper_jitter": 1.0, "actuator_saturation": None,
            "saturation_reason": "指令与读数同源,无法评估执行响应", "path_efficiency": 0.1,
            "joint_stability": 0.15, "fluency": 1.0, "stuck": None,
            "stuck_reason": "指令与读数同源,无法评估执行响应", "idle_head_s": 0.0,
            "idle_tail_s": 1.2, "idle_mid_count": 0, "active_ratio": 0.9}
    records = [rec_mq(0, "scored", base, score=0.98),
               rec_mq(1, "scored", {**base, "smoothness": 0.85, "stuck": 0.0,
                                 "stuck_joints": [{"joint": 1}], "idle_mid_count": 2}, score=0.9),
               rec_mq(2, "scored", {"fluency": 0.8, "active_ratio": 0.9, "stuck_joints": []}, score=0.7),
               rec_mq(3, "error", error={"kind": "execution", "incidents": []})]
    out = S.motion_stats(records)
    subs = {s["name"]: s for s in out["subscores"]}
    assert subs["smoothness"] == {"name": "smoothness", "mean": 0.9, "n": 2, "na": 0, "in_total": True}
    assert subs["actuator_saturation"]["mean"] is None and subs["actuator_saturation"]["na"] == 2
    assert subs["actuator_saturation"]["na_reason"].startswith("指令与读数同源")
    assert subs["fluency"]["n"] == 3 and subs["fluency"]["in_total"] is False
    assert out["stuck_episodes"] == 1 and out["stuck_unassessable"] == 1
    assert names(out["idle_episodes"]) == {"head": 0, "mid": 1, "tail": 2}
    assert out["active_ratio_mean"] == 0.9


def test_visual_per_camera_rows():
    def vis(scores, padded=()):
        return {"per_camera_detail": {f"observation.images.{c}": {"score": s} for c, s in scores.items()},
                "padded_channels": [f"observation.images.{c}" for c in padded],
                "camera_weights": {f"observation.images.{c}": 1.0 for c in scores},
                "params": {"blur_ref_var": 100.0, "frame_max_side": 448}}
    records = [rec_vq(0, "scored", vis({"wrist": 0.95, "front": 0.5}), score=0.72),
               rec_vq(1, "scored", vis({"wrist": 0.85}, padded=("front",)), score=0.85),
               rec_vq(2, "scored", {"per_camera": {"wrist": 0.4}}, score=0.4)]      # older details
    out = S.visual_stats(records)
    rows = {r["camera"]: r for r in out["cameras"]}
    assert rows["wrist"]["n"] == 3 and rows["wrist"]["low"] == 1 and rows["wrist"]["weight"] == 1.0
    assert rows["wrist"]["hist"] == [0, 0, 0, 0, 1, 0, 0, 0, 1, 1]
    assert rows["front"] == {"camera": "front", "n": 1, "mean": 0.5, "low": 1, "placeholder": 1,
                             "hist": [0, 0, 0, 0, 0, 1, 0, 0, 0, 0], "weight": 1.0}
    assert out["low_camera_readings"] == 2 and out["placeholder_readings"] == 1
    assert out["blur_ref_var"] == 100.0 and out["frame_max_side"] == 448


def _reading(lag, peak, code, trusted, cause="aligned"):
    return {"lag_s": lag, "corr_peak": peak, "code": code, "trusted": trusted,
            "diagnosis": {"cause": cause, "label": "", "text": "", "advice": ""}}


def test_sync_verdicts_and_the_dataset_health():
    aligned = {"verdict": "aligned", "flagged_cameras": [], "per_camera": {
        "wrist": _reading(0.02, 0.9, "aligned", True), "front": _reading(-0.01, 0.8, "aligned", True)}}
    flagged = {"verdict": "annotated", "flagged_cameras": ["front"], "per_camera": {
        "wrist": _reading(0.0, 0.9, "aligned", True), "front": _reading(0.6, 0.8, "misaligned", True)}}
    suspect = {"verdict": "annotated", "flagged_cameras": [], "suspect_cameras": ["front"],
               "abstained_cameras": ["front"], "per_camera": {
                   "wrist": _reading(0.01, 0.9, "aligned", True),
                   "front": _reading(0.5, 0.4, "flat_peak", False, "blurry_motion")}}
    records = [rec_sync(0, "pass", aligned), rec_sync(1, "pass", flagged), rec_sync(2, "pass", suspect),
               rec_sync(3, "pass", {"verdict": "undecidable", "per_camera": {}, "flagged_cameras": []}),
               rec_sync(4, "fail", {"verdict": "misaligned_all", "flagged_cameras": ["wrist", "front"],
                               "per_camera": {"wrist": _reading(0.7, 0.9, "misaligned", True),
                                              "front": _reading(0.72, 0.8, "misaligned", True)}}),
               rec_sync(5, "pass", {"per_camera": {"wrist": 0.3}})]                  # no verdict: skipped
    out = S.sync_stats(records, 0.25)
    assert out["verdicts"] == [{"name": "aligned", "count": 1}, {"name": "annotated", "count": 1},
                               {"name": "suspect", "count": 1}, {"name": "undecidable", "count": 1},
                               {"name": "misaligned", "count": 1}]
    assert out["flagged_camera_readings"] == 3 and out["lag_tol_s"] == 0.25
    cams = {c["camera"]: c for c in out["cameras"]}
    assert cams["wrist"]["readings"] == 4 and cams["wrist"]["n"] == 4
    assert cams["front"]["n_suspect"] == 1 and cams["front"]["n_flagged"] == 2
    assert "**" not in out["sync_advice"] and out["sync_advice"]
    assert out["negative_lag_episodes"] == 0
    # registry 7.4: the windowed comparison's coverage - none of these readings carries windows
    assert out["window_comparable_episodes"] == 0 and out["episodes_with_readings"] == 5
    assert all(c["n_window_stable"] == 0 for c in out["cameras"])
    assert "分窗规则可比对 0/5 条" in out["sync_advice"]
    no_episode_lists(out)


def test_sync_window_coverage_counts_the_episodes_both_cameras_are_steady_on():
    """Registry 7.4: a report says on how many episodes the windowed comparison could speak (two or more cameras
    with steady windows) and, per camera, how often it was steady - by the task's own lines."""
    def lw(lag, n_trusted=8, iqr=0.03):
        return {"win_s": 2.0, "hop_s": 1.0, "dt_s": 0.0333, "n_windows": 12, "n_trusted": n_trusted,
                "lag_s": lag, "iqr_s": iqr, "windows": []}
    both = {"verdict": "aligned", "flagged_cameras": [], "per_camera": {
        "wrist": {**_reading(0.02, 0.9, "aligned", True), "lag_windows": lw(0.03)},
        "front": {**_reading(-0.01, 0.8, "aligned", True), "lag_windows": lw(0.0)}}}
    one = {"verdict": "aligned", "flagged_cameras": [], "per_camera": {
        "wrist": {**_reading(0.02, 0.9, "aligned", True), "lag_windows": lw(0.03)},
        "front": {**_reading(-0.01, 0.8, "aligned", True), "lag_windows": lw(0.0, iqr=0.4)}}}
    none = {"verdict": "aligned", "flagged_cameras": [], "per_camera": {
        "wrist": {**_reading(0.02, 0.9, "aligned", True), "lag_windows": lw(0.03, n_trusted=2)},
        "front": _reading(-0.01, 0.8, "aligned", True)}}
    records = [rec_sync(0, "pass", both), rec_sync(1, "pass", one), rec_sync(2, "pass", none)]
    out = S.sync_stats(records, 0.25)
    cams = {c["camera"]: c for c in out["cameras"]}
    assert out["window_comparable_episodes"] == 1 and out["episodes_with_readings"] == 3
    assert cams["wrist"]["n_window_stable"] == 2 and cams["front"]["n_window_stable"] == 1
    assert "分窗规则可比对 1/3 条" in out["sync_advice"] and "front 只有 1 条稳" in out["sync_advice"]
    # the task's lines: tolerate the wobble and two trusted windows, and every episode is comparable
    loose = S.sync_stats(records, 0.25, {"window_max_iqr_s": 0.5, "window_min_trusted": 2})
    assert loose["window_comparable_episodes"] == 2                       # ep2's front has no windows at all
    assert "只有" not in loose["sync_advice"].split("分窗规则可比对")[1]
    no_episode_lists(out)


def test_task_success_judgements_layers_and_sources():
    """The one request (D71): the episode's verdict (probe) and the cameras' own answers (endstate)."""
    records = [
        rec(0, "pass", {"verdict": "success", "init_verdict": "success", "review": "yes",
                        "cam_votes": {"a": "yes"}, "task_desc_source": "原始标注"}),
        rec(1, "pass", {"verdict": "success", "init_verdict": "success", "task_desc_source": "人工改标"}),
        rec(2, "abstain", {"verdict": "uncertain", "init_verdict": "success", "review": "no",
                           "cam_votes": {"a": "no"}, "reason": "相机 a 看到杯子没放进去",
                           "task_desc_source": "原始标注"}),
        rec(3, "fail", {"verdict": "failure", "init_verdict": "failure", "review": "no",
                        "cam_votes": {"a": "no"}, "task_desc_source": "原始标注"}),
        rec(4, "error", error={"kind": "execution", "incidents": [{"step": "probe"}]}),
    ]
    out = S.task_stats(records)
    assert names(out["judgements"]) == {"success": 2, "uncertain": 1, "failure": 1}
    assert out["abstain_by_judgement"] == [{"name": "uncertain", "count": 1}]
    assert names(out["text_sources"]) == {"原始标注": 3, "人工改标": 1}
    assert out["layers"] == [{"name": "probe", "count": 4}, {"name": "endstate", "count": 3}]


