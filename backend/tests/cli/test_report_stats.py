"""The chart-ready aggregates of report.json's module summaries (06 §6.2, F6.2).

Unit tests of ``pipeline.report_stats`` on records shaped like the ones the checks write
(details copied from a real run of the synthetic fixture), plus the rules the report
page relies on: stable series, statistics only - never a list of episodes - and
tolerance of older details that lack keys.
"""
from __future__ import annotations

import json

from curation.pipeline import report_stats as S


def rec(ep: int, verdict: str, details: dict | None = None, *, score=None, error=None) -> dict:
    return {"episode_index": ep, "verdict": verdict, "score": score, "details": details or {},
            "error": error}


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
               rec(4, "scored", score=0.8)]
    out = S.generic(records, [0.8])
    assert names(out["abstain_reason_counts"]) == {"末态物证 … 在灰区": 2}
    assert names(out["error_steps"]) == {"arbitration": 1, "decode": 1}
    assert sum(b["count"] for b in out["score_hist"]) == 1
    assert S.generic([rec(0, "pass")], []) == {}


def test_timestamp_failures_by_kind_and_durations():
    records = [
        rec(0, "pass", {"n": 75, "duration_s": 4.93, "dt_nominal": 0.0667, "max_dt": 0.0667,
                        "jitter_ratio": 0.0}),
        rec(1, "fail", {"n": 75, "duration_s": 5.53, "dt_nominal": 0.0667, "max_dt": 0.67,
                        "gap_frames": [{"frame": 36, "dt": 0.67}], "reason": "第 37 帧后间隔突增"}),
        rec(2, "fail", {"n": 6, "duration_s": 0.33, "reason": "全长只有 0.33 秒"}),
        rec(3, "fail", {"n": 40, "reason": "时间戳出现倒退/重复(第 3 帧)", "frame": 2, "ts": [0.1, 0.1]}),
        rec(4, "fail", {"n": 80, "duration_s": 5.3, "dt_nominal": 0.0667, "max_dt": 0.1,
                        "jitter_ratio": 0.2}),
        rec(5, "fail", {"reason": "只有 1 个时间戳,连时长都算不出"}),
    ]
    out = S.timestamp_stats(records)
    assert out["fail_reasons"] == [{"name": "out_of_order", "count": 1}, {"name": "gap", "count": 1},
                                   {"name": "fragment", "count": 1}, {"name": "jitter", "count": 1},
                                   {"name": "other", "count": 1}]
    assert out["duration_total_s"] == 16.1 and out["duration_min_s"] == 0.33
    assert out["duration_max_s"] == 5.53 and sum(b["count"] for b in out["duration_hist"]) == 4
    assert S.timestamp_stats([rec(0, "pass")])["fail_reasons"][0] == {"name": "out_of_order", "count": 0}


def test_kinematic_violations_by_type_and_joint():
    records = [
        rec(0, "fail", {"n_violations": 3, "profile": "franka", "violations": [
            {"type": "joint_limit", "joint": 3, "frame": 12, "value": 2.5, "limit": [-2, 2]},
            {"type": "joint_limit", "joint": 3, "frame": 13, "value": 2.6, "limit": [-2, 2]},
            {"type": "velocity_limit", "joint": 10, "frame": 5, "value": 9.0, "limit": 2.0}]}),
        rec(1, "fail", {"mode": "ee", "profile": "franka", "violations": [
            {"type": "ee_reach", "joint": "xyz", "frame": 1, "value": 2.0, "limit": 1.1}]}),
        rec(2, "pass", {"n_violations": 0, "violations": [], "profile": "franka"}),
        rec(3, "abstain", {"reason": "单位疑似错配:…", "unit_mismatch": True}),
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
    records = [rec(0, "scored", base, score=0.98),
               rec(1, "scored", {**base, "smoothness": 0.85, "stuck": 0.0,
                                 "stuck_joints": [{"joint": 1}], "idle_mid_count": 2}, score=0.9),
               rec(2, "scored", {"fluency": 0.8, "active_ratio": 0.9, "stuck_joints": []}, score=0.7),
               rec(3, "error", error={"kind": "execution", "incidents": []})]
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
    records = [rec(0, "scored", vis({"wrist": 0.95, "front": 0.5}), score=0.72),
               rec(1, "scored", vis({"wrist": 0.85}, padded=("front",)), score=0.85),
               rec(2, "scored", {"per_camera": {"wrist": 0.4}}, score=0.4)]      # older details
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
    records = [rec(0, "pass", aligned), rec(1, "pass", flagged), rec(2, "pass", suspect),
               rec(3, "pass", {"verdict": "undecidable", "per_camera": {}, "flagged_cameras": []}),
               rec(4, "fail", {"verdict": "misaligned_all", "flagged_cameras": ["wrist", "front"],
                               "per_camera": {"wrist": _reading(0.7, 0.9, "misaligned", True),
                                              "front": _reading(0.72, 0.8, "misaligned", True)}}),
               rec(5, "pass", {"per_camera": {"wrist": 0.3}})]                  # no verdict: skipped
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
    no_episode_lists(out)


def test_task_success_judgements_layers_and_sources():
    records = [
        rec(0, "pass", {"verdict": "success", "init_verdict": "success", "review": "yes",
                        "cam_votes": {"a": "yes"}, "task_desc_source": "原始标注"}),
        rec(1, "pass", {"verdict": "arbitration_success", "init_verdict": "uncertain",
                        "review": "abstain", "arbitration": {"final": "yes"},
                        "task_desc_source": "自产caption"}),
        rec(2, "abstain", {"verdict": "label_conflict_suspect", "init_verdict": "failure",
                           "review": "no", "label_check": {"outcome": "different"},
                           "reason": "复核判未完成,但标注与画面不是同一任务", "task_desc_source": "原始标注"}),
        rec(3, "fail", {"verdict": "failure", "init_verdict": "failure", "review": "no",
                        "task_desc_source": "原始标注"}),
        rec(4, "error", error={"kind": "execution", "incidents": [{"step": "probe"}]}),
    ]
    out = S.task_stats(records)
    assert names(out["judgements"]) == {"success": 1, "arbitration_success": 1,
                                        "label_conflict_suspect": 1, "failure": 1}
    assert out["abstain_by_judgement"] == [{"name": "label_conflict_suspect", "count": 1}]
    assert names(out["text_sources"]) == {"原始标注": 3, "自产caption": 1}
    assert out["layers"] == [{"name": "probe", "count": 4}, {"name": "endstate", "count": 4},
                             {"name": "label_guard", "count": 1}, {"name": "arbitration", "count": 1}]


def test_dedup_group_sizes_and_skill_families():
    assert S.dedup_stats({"action_collisions": [[3, 7], [1, 2, 9], [4, 5]]})["group_sizes"] == [
        {"name": "2", "count": 2}, {"name": "3", "count": 1}]
    assert S.dedup_stats({}) == {"group_sizes": []}
    profile = {"families": {
        "grasp": {"count": 5, "pct": 62.5, "name_zh": "抓取搬运", "subskills": {
            "place": {"count": 4, "name_zh": "放置"}, "stack": {"count": 1}}},
        "wipe": {"count": 3, "pct": 37.5, "subskills": {"wipe": {"count": 3}}}},
        "undersampled": ["wipe"]}
    audit = {"high": [{"id": "ep000001"}], "mid_for_review": [{"id": "ep000002"}, {"id": "ep000003"}],
             "low_caption_unstable": [{"id": "ep000004"}]}
    records = [rec(0, "pass", {"grouping_text_source": "原始标注"}),
               rec(1, "pass", {"grouping_text_source": "自产caption"})]
    out = S.skill_stats(records, profile, audit)
    assert out["family_distribution"] == [{"name": "抓取搬运", "count": 5}, {"name": "wipe", "count": 3}]
    assert out["family_tree"][0]["subskills"] == [{"name": "放置", "count": 4}, {"name": "stack", "count": 1}]
    assert out["family_tree"][1]["undersampled"] is True
    assert (out["label_disagreements"], out["disagreement_high"], out["disagreement_review"],
            out["unstable"]) == (3, 1, 2, 1)
    assert names(out["grouping_sources"]) == {"原始标注": 1, "自产caption": 1}
    no_episode_lists(out)
    # the records' own assignments win: counts per family and sub-skill, names from the profile
    records = [rec(i, "pass", {"family": "grasp", "subskill": "place"}) for i in range(3)] + [
        rec(3, "pass", {"family": "wipe", "subskill": "wipe"}),
        rec(4, "error", error={"kind": "execution", "incidents": []})]
    out = S.skill_stats(records, profile, None)
    assert out["family_distribution"] == [{"name": "抓取搬运", "count": 3}, {"name": "wipe", "count": 1}]
    assert out["family_tree"][0] == {"name": "抓取搬运", "count": 3, "pct": 75.0, "undersampled": False,
                                     "subskills": [{"name": "放置", "count": 3}]}
    assert out["label_disagreements"] == 0 and out["grouping_sources"] == []
