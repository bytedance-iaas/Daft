"""GET /tasks/{id}/episodes/{index}/sync-curves: one episode's sync curves (F6.2)."""
from __future__ import annotations

import json
import math

from .conftest import SHORT, assert_error, assert_schema


def _curves(world, ep: int, *, n: int = 1500, lag_frames: int = 1) -> None:
    """A curves file as the frame stage writes it: per camera t / flow / speed and the
    reading, for ``n`` samples at 30 fps (more than the 600 points served)."""
    cams = {}
    for k, cam in enumerate(SHORT):
        t = [round(i / 30.0, 3) for i in range(n)]
        speed = [abs(math.sin(i / 17.0)) + 0.2 * abs(math.sin(i / 5.0 + k)) for i in range(n)]
        flow = [speed[max(0, i - lag_frames)] * 3.0 for i in range(n)]      # the picture is later
        cams[cam] = {"t": t, "flow": [round(v, 5) for v in flow], "speed": [round(v, 6) for v in speed],
                     "lag_s": 0.01 * ep, "corr_peak": 0.8, "code": "aligned", "n_trimmed_static": 0}
    doc = {"cameras": cams, "verdict": "aligned", "consensus_lag_s": None, "n_cameras": 2,
           "n_trusted": 2, "flagged_cameras": [], "per_camera": {}, "lag_tol_s": 0.25}
    path = world.run_dir / "checks" / "video_action_sync" / "curves" / f"ep{ep:06d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_curves_per_camera_downsampled_with_the_reading_on_them(world):
    _curves(world, 3)
    r = world.get("/episodes/3/sync-curves")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("SyncCurves", body)
    assert (body["episode_index"], body["revision"], body["lag_tol_s"], body["window_s"]) == (3, 1, 0.25, 2.0)
    assert body["verdict"] == "aligned"
    assert [c["camera"] for c in body["cameras"]] == sorted(SHORT)
    for cam in body["cameras"]:
        assert 0 < len(cam["t"]) <= 600 and len(cam["t"]) == len(cam["flow"]) == len(cam["speed"])
        assert cam["t"][0] == 0 and cam["t"][-1] > 40                        # the whole episode
        assert 0 < len(cam["lags"]) <= 600 and len(cam["lags"]) == len(cam["xcorr"])
        assert min(cam["lags"]) >= -2.0 and max(cam["lags"]) <= 2.0
        # the revision's reading (0.03 s), placed on the drawn curve
        assert cam["lag_s"] == 0.03 and cam["code"] == "aligned" and cam["corr_peak"] == 0.8
        assert cam["peak"]["lag_s"] == 0.03 and -1.0 <= cam["peak"]["corr"] <= 1.5
    # an older revision reads the same file with its own readings
    assert world.get("/episodes/3/sync-curves", rev=1).status_code == 200


def test_why_there_are_no_curves(world):
    body = assert_error(world.get("/episodes/0/sync-curves"), "not_found", 404)
    assert body["error"]["details"] == {"episode_index": 0, "revision": 1, "reason": "no_curves"}
    body = assert_error(world.get("/episodes/1/sync-curves"), "not_found", 404)   # killed at numeric
    assert body["error"]["details"]["reason"] == "no_record"
    assert "前面已被判废" in body["error"]["message"]
    body = assert_error(world.get("/episodes/99/sync-curves"), "not_found", 404)
    assert body["error"]["details"]["reason"] == "episode_missing"
    assert_error(world.get("/episodes/-1/sync-curves"), "validation_failed", 400)
    assert_error(world.get("/episodes/3/sync-curves", rev=2), "not_found", 404)


def test_a_broken_or_empty_curves_file_is_a_404(world):
    path = world.run_dir / "checks" / "video_action_sync" / "curves" / "ep000004.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert assert_error(world.get("/episodes/4/sync-curves"), "not_found", 404)["error"]["details"][
        "reason"] == "no_curves"
    path.write_text(json.dumps({"cameras": {"wrist": {"t": [0.0], "flow": [1.0], "speed": [1.0]}}}),
                    encoding="utf-8")
    assert assert_error(world.get("/episodes/4/sync-curves"), "not_found", 404)["error"]["details"][
        "reason"] == "no_curves"


def test_a_task_that_did_not_select_the_sync_check(world):
    _curves(world, 3)
    report = world.run_dir / "revisions" / "r0001" / "report.json"
    doc = json.loads(report.read_text(encoding="utf-8"))
    doc["modules"] = [m for m in doc["modules"] if m["id"] != "video_action_sync"]
    report.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    body = assert_error(world.get("/episodes/3/sync-curves"), "not_found", 404)
    assert body["error"]["details"]["reason"] == "module_not_run"
