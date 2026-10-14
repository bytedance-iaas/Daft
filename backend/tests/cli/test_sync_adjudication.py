"""The sync check's person's question (registry 7.3, ``sync_check``, 2026-10-13).

A camera out of step with the action while the others are not (``camera_misaligned``), or cameras whose lags
differ (``lag_inconsistent``, MV-3), used to pass with a note. Now the episode is asked on sync_check:
"usable" settles it, "misaligned" makes it a person's blocking finding (not appealable), "unsure" keeps it
asked; since 7.5 the whole-episode offset (``misaligned_all``) is asked too, with the answer ``shifted`` keeping it.
"""
from __future__ import annotations

import json

from .pipeline import run
from .test_aggregate import RunDir, apply, decisions, final

SYNC = "video_action_sync"


def _disagree(front_lag: float, wrist_lag: float = 0.0, *, front_code: str = "misaligned") -> dict:
    return {"verdict": "annotated", "consensus_lag_s": None, "n_cameras": 2, "n_trusted": 2,
            "flagged_cameras": ["front"] if front_code == "misaligned" else [], "abstained_cameras": [],
            "per_camera": {"front": {"trusted": True, "lag_s": front_lag, "corr_peak": 0.8, "code": front_code},
                           "wrist": {"trusted": True, "lag_s": wrist_lag, "corr_peak": 0.8, "code": "aligned"}}}


def _asked(lists) -> dict[int, list[tuple[str, str, str, list[str]]]]:
    return {e: [(i["line"], i["kind"], i["source_module"], sorted(i["codes"])) for i in v["review"]]
            for e, v in lists["review"].items()}


def test_cameras_out_of_step_are_asked_and_a_person_settles_or_rejects(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(*range(6))
    # ep 1: one camera 1 s off against the action, the other not (Qian's MV-3 sample shape); ep 2: both trusted,
    # 0.4 s apart but neither flagged on its own; ep 3: the whole episode off (every trusted camera agrees)
    rd.replace(SYNC, 1, "pass", details=_disagree(1.0))
    rd.replace(SYNC, 2, "pass", details=_disagree(0.4, 0.0, front_code="aligned"))
    rd.replace(SYNC, 3, "fail", details={"verdict": "misaligned_all", "consensus_lag_s": -0.6,
                                         "per_camera": {"front": {"trusted": True, "lag_s": -0.6, "code": "misaligned"},
                                                        "wrist": {"trusted": True, "lag_s": -0.62, "code": "misaligned"}},
                                         "flagged_cameras": ["front", "wrist"], "abstained_cameras": []})
    run_dir = rd.write()
    lists = final(run_dir, "0-5")
    assert sorted(lists["passed"]) == [0, 1, 2, 3, 4, 5] and lists["reject"] == {}
    assert _asked(lists) == {1: [("sync_check", "sync_suspect", SYNC, ["camera_misaligned", "lag_inconsistent"])],
                             2: [("sync_check", "sync_suspect", SYNC, ["lag_inconsistent"])],
                             3: [("sync_check", "sync_suspect", SYNC, ["misaligned_all"])]}     # 7.5: asked, not rejected
    assert "front 比动作晚 1.00 秒" in lists["review"][1]["review"][0]["reason"]
    apply(run_dir, decisions(str(tmp_path / "d.json"), (1, "sync_check", "misaligned", None),
                             (2, "sync_check", "usable", None), (3, "sync_check", "shifted", None)))
    after = final(run_dir, "0-5", revision=2)
    assert sorted(after["passed"]) == [0, 2, 3, 4, 5] and sorted(after["reject"]) == [1]       # shifted keeps ep 3
    assert after["reject"][1]["reasons"] == [{"module": SYNC, "kind": "human", "code": "camera_misaligned", "item": "AV-1",
                                             "appealable": False, "text": "人工裁决判为相机与动作确有错位"}]
    assert _asked(after) == {}
    # a person's conclusion is final: no appeal on it
    path = tmp_path / "d2.json"
    path.write_text(json.dumps({"schema_version": "2.0", "decisions": [
        {"id": 10, "episode_index": 1, "line": "reject_appeal", "decision": "restore", "new_label": None,
         "note": None, "decided_by": "alice", "decided_at": 1790000000010}]}))
    refused = run("adjudicate-apply", "--run-dir", run_dir, "--decisions", str(path))
    assert refused.rc == 2 and "has no reject a person may appeal" in refused.doc["error"]["message"]


def test_unsure_keeps_the_question(tmp_path):
    rd = RunDir(str(tmp_path / "run")).good(0, 1)
    rd.replace(SYNC, 1, "pass", details=_disagree(1.0))
    run_dir = rd.write()
    apply(run_dir, decisions(str(tmp_path / "d.json"), (1, "sync_check", "unsure", None)))
    after = final(run_dir, "0-1", revision=2)
    assert sorted(after["passed"]) == [0, 1]
    assert _asked(after) == {1: [("sync_check", "sync_suspect", SYNC, ["camera_misaligned", "lag_inconsistent"])]}
