"""F5.5: input files of module parameters through the Daemon (C4 1.8.0, registry 1.5, design doc 12 §11).

Upload validation with located errors, the task rules for the advisory EEF module (an upload handle,
never a server path; the module is preflighted against the dataset with the uploaded file) and - in
the slow test - a task that runs it end to end: the files are copied into the run directory, the
advisory stage runs on every selected episode, the report has its section and the delivery its
artifacts.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from ..cli.test_eef_check import CAM, _entry, _truth
from .conftest import ALL_MODULES, API, JSON, assert_schema

EEF = "eef_video_consistency"
REVIEW = "eef_video_review"


def _bundle(*, corrupt: bool = False) -> dict:
    entries = [_entry(ep, shift=4 if ep == 1 else 0, offset=9.0 if ep == 2 else 0.0) for ep in range(7)]
    if corrupt:
        entries[3]["frames"][10]["cameras"][CAM]["projection"]["points"]["block_center"]["truth"] = [1, 2]
    return {"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
            "dataset": {"id": "mini", "lerobot_codebase_version": "v2.1", "fps": 15.0, "episode_count": 8},
            "media_uri_base": "lerobot_root", "samples": entries}


def _seed_rows() -> list[dict]:
    rows = []
    for ep in range(7):
        uv = _truth(ep)
        rows += [{"schema_version": "eef-video/1.0.0", "sample_id": f"mini_{ep:06d}", "frame_index": i,
                  "camera_id": CAM, "video_frame_index": i, "pixel_space": "media", "method": "synthetic_fixture",
                  "model_version": "test", "input_image_sha256": "0" * 64, "projection_visible_to_localizer": False,
                  "points": {"block_center": {"uv_px": [float(uv[i, 0]), float(uv[i, 1])], "visibility": "visible",
                                              "confidence": 1.0, "uncertainty_px": None}}}
                 for i in range(0, len(uv), 15)]
    return rows


def _upload(d, kind: str, name: str, payload, *, status: int = 201) -> dict:
    r = d.client.post(f"{API}/uploads", params={"kind": kind, "name": name}, headers=JSON,
                      content=json.dumps(payload).encode())
    assert r.status_code == status, r.text
    body = r.json()
    if status == 201:
        assert_schema("Upload", body)
    return body


def test_upload_is_validated_on_arrival_with_located_errors(daemon):
    d = daemon()
    up = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    assert up["handle"] == f"upload:{up['upload_id']}" and len(up["sha256"]) == 64
    assert re.fullmatch(r"upl-[a-z]{9}", up["upload_id"])                 # D45
    s = up["validation"]["summary"]
    assert s["samples"] == 7 and s["episodes"] == list(range(7)) and s["cameras"] == [CAM]
    got = d.client.get(f"{API}/uploads/{up['upload_id']}")
    assert got.status_code == 200 and got.json() == up
    bad = _upload(d, "eef_trajectory", "trajectory.json", _bundle(corrupt=True), status=400)
    assert bad["error"]["code"] == "validation_failed"
    (err,) = bad["error"]["details"]["errors"]
    assert "truth" in err["field"] and err["code"] == "forbidden_key"
    assert (err["sample_id"], err["episode_index"], err["frame_index"], err["camera_id"], err["point_id"]) == \
        ("mini_000003", 3, 10, CAM, "block_center")
    assert "样本 mini_000003、第 10 帧、相机 exterior、点 block_center" in bad["error"]["message"]
    seeds = _upload(d, "eef_observation_seeds", "seeds.jsonl", _seed_rows())
    assert seeds["validation"]["summary"]["samples"] == 7 and seeds["kind"] == "eef_observation_seeds"
    rows = _seed_rows()
    rows[3]["projection_visible_to_localizer"] = True
    bad = _upload(d, "eef_observation_seeds", "seeds.jsonl", rows, status=400)
    first = bad["error"]["details"]["errors"][0]
    assert first["field"].startswith("3") and first["sample_id"] == "mini_000000"
    plain = d.client.post(f"{API}/uploads", params={"kind": "eef_trajectory", "name": "t.json"},
                          headers={"Content-Type": "text/plain"}, content=b"{}")
    assert plain.status_code == 400
    for unknown in ("upl_0000000000", "upl-aaaaaaaaa", "upl-AAAAAAAAA", "../etc"):
        assert d.client.get(f"{API}/uploads/{unknown}").status_code == 404
    assert _upload(d, "no_such_kind", "x.json", {}, status=400)["error"]["code"] == "validation_failed"


def test_uploads_made_before_d45_keep_working_and_a_taken_id_is_drawn_again(daemon, monkeypatch):
    from daemon import uploads as U

    d = daemon()
    legacy, fresh = "upl_0123456789abcdef0123", U.new_id
    with monkeypatch.context() as m:                           # an upload from before D45
        m.setattr(U, "new_id", lambda prefix: legacy)
        old = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    assert (old["upload_id"], old["handle"]) == (legacy, f"upload:{legacy}")
    draws = [legacy]                                           # the next draw hits it
    with monkeypatch.context() as m:
        m.setattr(U, "new_id", lambda prefix: draws.pop() if draws else fresh(prefix))
        new = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    assert re.fullmatch(r"upl-[a-z]{9}", new["upload_id"]) and not draws
    assert d.client.get(f"{API}/uploads/{legacy}").json() == old       # untouched, still readable
    created = d.create(modules=[*ALL_MODULES, {"id": EEF, "params": {"trajectory_json": old["handle"]}}],
                       start_now=False)
    (row,) = [m for m in d.get(created["id"])["modules"] if m["id"] == EEF]
    assert row["selected"] and row["availability"] == "available"


def test_the_dataset_preflight_asks_for_the_file(daemon):
    d = daemon()
    pf = d.preflight()["result"]
    (entry,) = [m for m in pf["modules"] if m["id"] == EEF]
    assert entry["availability"] == "needs_input" and entry["input_hint"] == {"field": "trajectory_json"}


def test_a_task_needs_an_upload_handle_not_a_path(daemon):
    d = daemon()
    for params, words in (({}, "trajectory_json"),
                          ({"trajectory_json": "/etc/hosts"}, "upload"),
                          ({"trajectory_json": "upload:upl_0000000000"}, "不存在")):
        body = d.task_body(modules=[*ALL_MODULES, {"id": EEF, "params": params}], start_now=False)
        r = d.api("POST", "/tasks", json=body)
        assert r.status_code == 400, r.text
        assert words in r.text, r.text
    seeds = _upload(d, "eef_observation_seeds", "seeds.jsonl", _seed_rows())
    body = d.task_body(modules=[*ALL_MODULES, {"id": EEF, "params": {"trajectory_json": seeds["handle"]}}],
                       start_now=False)
    r = d.api("POST", "/tasks", json=body)
    assert r.status_code == 400 and "eef_trajectory" in r.text


def test_the_module_is_preflighted_with_the_uploaded_file(daemon):
    d = daemon()
    traj = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    seeds = _upload(d, "eef_observation_seeds", "seeds.jsonl", _seed_rows())
    params = {"trajectory_json": traj["handle"], "observation_seeds": seeds["handle"]}
    created = d.create(modules=[*ALL_MODULES, {"id": EEF, "params": params}], start_now=False)
    task = d.get(created["id"])
    (row,) = [m for m in task["modules"] if m["id"] == EEF]
    assert row["selected"] and row["availability"] == "available"
    snap = d.rt.repo.get_task(created["id"]).preflight           # the task's frozen preflight
    (entry,) = [m for m in snap["modules"] if m["id"] == EEF]
    assert entry["availability"] == "available" and entry["episode_counts"] == {"available": 7, "unsupported": 1}
    assert entry["subitems"]["position_2d"]["availability"] == "available"      # the seeds were used


def test_the_review_comes_with_the_module_it_reviews(daemon):
    d = daemon()
    alone = d.task_body(modules=[*ALL_MODULES, REVIEW], start_now=False)
    r = d.api("POST", "/tasks", json=alone)
    assert r.status_code == 400 and "一起勾选" in r.text, r.text
    nofile = d.task_body(modules=[*ALL_MODULES, EEF, REVIEW], start_now=False)
    r = d.api("POST", "/tasks", json=nofile)
    assert r.status_code == 400 and "trajectory_json" in r.text, r.text
    traj = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    both = [*ALL_MODULES, {"id": EEF, "params": {"trajectory_json": traj["handle"]}},
            {"id": REVIEW, "params": {"review_windows_per_camera": 2}}]
    created = d.create(modules=both, start_now=False)
    rows = {m["id"]: m for m in d.get(created["id"])["modules"]}
    assert rows[REVIEW]["selected"] and rows[REVIEW]["availability"] == "available"
    no_vlm = d.task_body(modules=[m for m in both if m not in ("task_success", "skill_profile")], start_now=False)
    no_vlm.pop("vlm", None)
    r = d.api("POST", "/tasks", json=no_vlm)
    assert r.status_code == 400 and "VLM" in r.text, r.text


@pytest.mark.slow
def test_an_eef_task_runs_end_to_end(daemon):
    d = daemon()
    traj = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    seeds = _upload(d, "eef_observation_seeds", "seeds.jsonl", _seed_rows())
    params = {"trajectory_json": traj["handle"], "observation_seeds": seeds["handle"]}
    created = d.create(modules=[*ALL_MODULES, {"id": EEF, "params": params},
                                {"id": REVIEW, "params": {"review_windows_per_camera": 1,
                                                          "review_frames_per_window": 2}}], export=False)
    task = d.wait(created["id"])
    assert task["state"] in ("succeeded", "completed_with_errors"), json.dumps(task)[:2000]
    (row,) = [m for m in task["modules"] if m["id"] == EEF]
    assert row["state"] == "succeeded" and row["episodes_total"] == 8 and row["episodes_error"] == 0
    rd = d.run_dir(created["id"])
    uploads = json.load(open(os.path.join(rd, "inputs", "uploads.json")))
    assert set(uploads) == {traj["handle"], seeds["handle"]}
    assert all(os.path.isfile(os.path.join(rd, e["path"])) for e in uploads.values())
    plan = json.load(open(os.path.join(rd, "plan.json")))
    (adv,) = [s for s in plan["stages"] if s["id"] == "advisory_frame"]
    assert adv["modules"] == [EEF] and adv["episodes"] == "selected"
    recs = [json.loads(x) for x in open(os.path.join(rd, "checks", EEF, "results.jsonl"))]
    assert sorted(r["episode_index"] for r in recs) == list(range(8))
    assert {r["details"]["input_file_sha256"] for r in recs if r["details"].get("input_file_sha256")} \
        == {traj["sha256"]}
    run_id = task["run_id"]
    delivered = d.delivery(run_id)
    assert os.path.isfile(os.path.join(delivered, "checks", EEF, "results.jsonl"))
    assert os.path.isdir(os.path.join(delivered, "inputs"))
    # the review (F5.6): after the EEF module, on every selected episode, its own call kind
    (row,) = [m for m in task["modules"] if m["id"] == REVIEW]
    assert row["state"] == "succeeded" and row["episodes_total"] == 8
    (adv,) = [s for s in plan["stages"] if s["id"] == "advisory_vlm"]
    assert adv["modules"] == [REVIEW] and adv["episodes"] == "selected"
    assert [s["id"] for s in plan["stages"]].index("advisory_vlm") > [s["id"] for s in plan["stages"]].index(
        "advisory_frame")
    revs = {r["episode_index"]: r for r in (json.loads(x) for x in open(os.path.join(rd, "checks", REVIEW,
                                                                                   "results.jsonl")))}
    assert sorted(revs) == list(range(8)) and revs[7]["details"]["reasons"] == ["projection_missing"]
    assert all(revs[e]["details"]["status"] == "completed" and revs[e]["details"]["summary"]["answered"] == 1
               for e in range(7))
    usage = [json.loads(x) for x in open(os.path.join(rd, "usage.jsonl"))]
    assert {u["call_kind"] for u in usage if u.get("module") == REVIEW} == {"eef_review"}
    rev = task["result_rev"]
    rep = json.load(open(os.path.join(rd, "revisions", f"r{rev:04d}", "report.json")))
    (sec,) = [s for s in rep["modules"] if s["id"] == EEF]
    assert sec["summary"]["assessment_mode"] == "advisory"
    (sec,) = [s for s in rep["modules"] if s["id"] == REVIEW]
    assert sec["summary"]["reviewed"] == 7 and sec["summary"]["not_reviewed"] == 1
    r = d.api("GET", f"/tasks/{created['id']}/report/tables/eef_review_windows")
    assert r.status_code == 200 and len(r.json()["items"]) >= 7, r.text
    r = d.api("GET", f"/tasks/{created['id']}/report")
    assert r.status_code == 200 and EEF in [s["id"] for s in r.json()["report"]["modules"]]
