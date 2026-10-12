"""Durable episode handoff and bounded SQL lookups."""
from __future__ import annotations

import math

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
        "duplicate_check": {"elapsed_s": 0.02},
    })
    assert times == {"numeric": 0.3, "frame": 4.5, "duplicate": 0.02, "vlm": 12.0}
    assert round(sum(times.values()), 3) == 16.82
    assert processing_times({"task_success": {"elapsed_s": float("nan")}}) == {}
    # a record of a module the registry no longer has is left out, not an error
    assert processing_times({"dedup": {"elapsed_s": 3.0}, "timestamp_check": {"elapsed_s": 0.3}}) == {"numeric": 0.3}


#: the blocks of a run (design doc 17 §3.3): each block's streaming stages in order, with their modules
CHAINS = {"cpu": [("numeric", ["timestamp_check"]), ("frame", ["visual_quality"])],
          "vlm": [("vlm", ["task_success"])]}


def _store(tmp_path) -> EpisodeState:
    store = EpisodeState(state_path(tmp_path))
    store.set_blocks(CHAINS)
    return store


def test_episode_resume_state_is_durable_with_nonfinite_details(tmp_path):
    """Records and positions survive a reopen; a failure stops nothing (D57): every judged episode goes on."""
    path = state_path(tmp_path)
    store = _store(tmp_path)
    store.bootstrap(str(tmp_path), ["timestamp_check", "task_success"])
    scored = {"module": "timestamp_check", "episode_index": 7, "status": "ok", "findings": [],
              "details": {"value": float("nan")}, "error": None}
    store.finish("numeric", 7, {"timestamp_check": scored}, "frame")
    store.close()

    reopened = EpisodeState(path)
    try:
        assert reopened.blocks == {b: [(sid, mods) for sid, mods in chain] for b, chain in CHAINS.items()}
        assert reopened.positions([7], "cpu") == {7: "frame"} and reopened.positions([7], "vlm") == {}
        assert reopened.counts("timestamp_check") == (1, 0)
        assert reopened.completed(["timestamp_check"]) == [7]
        assert math.isnan(latest_results(str(tmp_path), "timestamp_check", [7])[7]["details"]["value"])
        failed = {"module": "task_success", "episode_index": 7, "status": "error", "findings": [],
                  "details": {}, "error": {"kind": "execution", "incidents": []}}
        reopened.finish("vlm", 7, {"task_success": failed}, "done")
        assert reopened.positions([7], "vlm") == {7: "done"}
        assert reopened.counts("task_success") == (1, 1)
        assert reopened.completed(["task_success"]) == [7]                # an error stops nothing either
        assert reopened.stage_states(7) == {"numeric": "done", "frame": "waiting", "vlm": "error"}
    finally:
        reopened.close()


def test_recent_episodes_follow_completion_order_and_cursor(tmp_path):
    store = _store(tmp_path)
    try:
        store.forward("numeric", [1, 9], "frame")
        store.forward("frame", [1], "done")
        recent = store.recent(limit=2)
        assert [row["episode_index"] for row in recent] == [1, 9]
        assert recent[0]["blocks"]["cpu"]["next_stage"] == "done" and not recent[0]["done"]   # the vlm block is not
        older = store.recent(before=recent[0]["updated_seq"], limit=2)
        assert [row["episode_index"] for row in older] == [9]
    finally:
        store.close()


def test_module_counts_so_far(tmp_path):
    """The live module cards (design doc 17 §5.3): per module the episodes judged, failed on, skipped without
    judging (D94: data integrity rejected them) and, of the judged, those with a finding."""
    store = _store(tmp_path)
    try:
        ok = {"module": "visual_quality", "status": "ok", "findings": [], "error": None}
        found = {**ok, "findings": [{"code": "frozen", "item": "IMG-1", "severity": "low", "message_zh": "冻结"}]}
        failed = {**ok, "status": "error", "error": {"kind": "execution", "incidents": []}}
        skipped = {**ok, "details": {"skipped": "integrity_rejected", "reason": "数据完整性已判废：文件为空"}}
        for ep, rec in ((0, ok), (1, found), (2, failed), (3, found), (4, skipped)):
            store.finish("frame", ep, {"visual_quality": {**rec, "episode_index": ep}}, "done")
        assert store.module_counts(["visual_quality", "task_success"]) == {
            "visual_quality": {"judged": 3, "error": 1, "skipped": 1, "flagged": 2},
            "task_success": {"judged": 0, "error": 0, "skipped": 0, "flagged": 0}}
        assert store.module_counts([]) == {}
    finally:
        store.close()


def test_a_record_is_read_from_another_thread(tmp_path):
    """D92: the check stage's episode workers read data integrity's record while the stage's own store connection
    belongs to its main thread."""
    import threading

    from curation.pipeline.episode_state import read_record

    store = _store(tmp_path)
    try:
        rec = {"module": "data_integrity", "episode_index": 3, "status": "ok", "findings": [], "error": None}
        store.finish("numeric", 3, {"data_integrity": rec}, "frame")
        got = {}
        t = threading.Thread(target=lambda: got.update(
            hit=read_record(store.path, "data_integrity", 3), miss=read_record(store.path, "data_integrity", 4)))
        t.start()
        t.join()
        assert got == {"hit": rec, "miss": None}
    finally:
        store.close()
