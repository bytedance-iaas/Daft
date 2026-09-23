"""Durable episode handoff and bounded SQL lookups."""
from __future__ import annotations

from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.records import latest_results
from curation.pipeline.timing import processing_times


def test_episode_processing_time_counts_shared_layer_once():
    times = processing_times({
        "timestamp_check": {"elapsed_s": 0.3},
        "motion_quality": {"elapsed_s": 0.3},
        "visual_quality": {"elapsed_s": 4.5},
        "video_action_sync": {"elapsed_s": 4.5},
        "task_success": {"elapsed_s": 12.0},
        "dedup": {"elapsed_s": 99.0},
    })
    assert times == {"numeric": 0.3, "frame": 4.5, "vlm": 12.0}
    assert round(sum(times.values()), 3) == 16.8
    assert processing_times({"task_success": {"elapsed_s": float("nan")}}) == {}


def test_episode_resume_state_is_durable_with_nonfinite_details(tmp_path):
    path = state_path(tmp_path)
    store = EpisodeState(path)
    store.bootstrap(str(tmp_path), ["timestamp_check", "task_success"])
    scored = {"module": "timestamp_check", "episode_index": 7,
              "verdict": "scored", "details": {"value": float("nan")}}
    store.finish("numeric", 7, {"timestamp_check": scored}, "vlm")
    store.close()

    reopened = EpisodeState(path)
    try:
        assert reopened.progress_for([7]) == {7: "vlm"}
        assert reopened.counts("timestamp_check") == (1, 0)
        assert reopened.survivors(["timestamp_check"]) == [7]
        assert latest_results(str(tmp_path), "timestamp_check", [7])[7]["verdict"] == "scored"
        failed = {"module": "task_success", "episode_index": 7,
                  "verdict": "fail", "details": {}}
        reopened.finish("vlm", 7, {"task_success": failed}, "done")
        assert reopened.progress_for([7]) == {7: "done"}
        assert reopened.survivors(["task_success"]) == []
    finally:
        reopened.close()


def test_recent_episodes_follow_completion_order_and_cursor(tmp_path):
    store = EpisodeState(state_path(tmp_path))
    try:
        store.forward("numeric", [1, 9], "frame")
        store.forward("frame", [1], "vlm")
        recent = store.recent(limit=2)
        assert [row["episode_index"] for row in recent] == [1, 9]
        older = store.recent(before=recent[0]["updated_seq"], limit=2)
        assert [row["episode_index"] for row in older] == [9]
    finally:
        store.close()


def test_failed_gate_recovery_preserves_missing_and_downstream_results(tmp_path):
    path = state_path(tmp_path)
    store = EpisodeState(path)
    try:
        for ep in (0, 1):
            store.finish("frame", ep, {"visual_quality": {"verdict": "fail"}}, "vlm")
        store.missing("frame", 2)
        store.finish("vlm", 1, {"task_success": {"verdict": "fail"}}, "done")
        store.fail_stage("frame", "injected")
    finally:
        store.close()
    store = EpisodeState(path)
    try:
        assert store.failed_stages() == {"frame": "injected"}
        assert store.reopen_gate("frame", "vlm") == [0]
        assert store.progress_for([0, 1, 2]) == {0: "vlm", 1: "done", 2: "done"}
        assert store.reopen_gate("frame", "vlm") == []
    finally:
        store.close()
