"""F5.5 / F5.9 / F5.11: the EEF module through the Daemon (C4 1.8.0, registry 1.5-1.9, design doc 12 §11).

Upload validation with located errors, the task rules for the EEF module (an upload handle, never a
server path; the module is preflighted against the dataset with the uploaded file; it needs a model)
and - in the slow tests - a task that runs it end to end (the files are copied into the run
directory, it judges the frame stage's survivors next to task_success, the report has its section and
the delivery its artifacts) and one where a person settles what it could not, through to the re-export.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from ..cli.test_eef_check import CAM, _entry, _truth
from .conftest import ALL_MODULES, API, JSON, assert_schema

EEF = "eef_video_consistency"


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


def test_the_module_needs_a_model(daemon):
    """D49: the module reviews with a model; with the file and a model chosen it is available."""
    d = daemon()
    traj = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    mods = [*ALL_MODULES, {"id": EEF, "params": {"trajectory_json": traj["handle"], "review_windows_per_camera": 2}}]
    created = d.create(modules=mods, start_now=False)
    rows = {m["id"]: m for m in d.get(created["id"])["modules"]}
    assert rows[EEF]["selected"] and rows[EEF]["availability"] == "available"
    no_vlm = d.task_body(modules=[m for m in mods if m not in ("task_success", "skill_profile")], start_now=False)
    no_vlm.pop("vlm", None)
    r = d.api("POST", "/tasks", json=no_vlm)
    assert r.status_code == 400 and "VLM" in r.text, r.text


def test_skill_profile_without_dedup_is_accepted(daemon):
    """Regression (F5.6 read every module id in depends_on as "re-examines"): skill_profile lists
    dedup only to order the stages, it never needs dedup selected."""
    d = daemon()
    created = d.create(modules=[m for m in ALL_MODULES if m != "dedup"], start_now=False)
    rows = {m["id"]: m for m in d.get(created["id"])["modules"]}
    assert rows["skill_profile"]["selected"] and not rows["dedup"]["selected"]


@pytest.mark.slow
def test_an_eef_task_runs_end_to_end(daemon):
    d = daemon()
    traj = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    seeds = _upload(d, "eef_observation_seeds", "seeds.jsonl", _seed_rows())
    params = {"trajectory_json": traj["handle"], "observation_seeds": seeds["handle"],
              "review_windows_per_camera": 1, "review_frames_per_window": 2}
    created = d.create(modules=[*ALL_MODULES, {"id": EEF, "params": params}], export=False)
    task = d.wait(created["id"])
    assert task["state"] in ("succeeded", "completed_with_errors"), json.dumps(task)[:2000]
    rd = d.run_dir(created["id"])
    uploads = json.load(open(os.path.join(rd, "inputs", "uploads.json")))
    assert set(uploads) == {traj["handle"], seeds["handle"]}
    assert all(os.path.isfile(os.path.join(rd, e["path"])) for e in uploads.values())
    # D49: a vlm-tier gate next to task_success, on the frame stage's survivors
    plan = json.load(open(os.path.join(rd, "plan.json")))
    (vlm,) = [s for s in plan["stages"] if s["id"] == "vlm"]
    assert EEF in vlm["modules"] and vlm["episodes"] == "survivors:frame" and EEF in vlm["hard_gates"]
    assert not [s for s in plan["stages"] if s["id"].startswith("advisory_")]
    recs = {r["episode_index"]: r for r in (json.loads(x) for x in open(os.path.join(rd, "checks", EEF,
                                                                                      "results.jsonl")))}
    ts = {r["episode_index"] for r in (json.loads(x) for x in open(os.path.join(rd, "checks", "task_success",
                                                                                "results.jsonl")))}
    assert set(recs) == ts                                   # the same survivors as task_success
    (row,) = [m for m in task["modules"] if m["id"] == EEF]
    assert row["state"] == "succeeded" and row["episodes_total"] == len(recs) and row["episodes_error"] == 0
    assert {r["details"]["input_file_sha256"] for r in recs.values() if r["details"].get("input_file_sha256")} \
        == {traj["sha256"]}
    for r in recs.values():
        assert r["passed"] is {"pass": True, "reject": False, "human": None}[r["details"]["decision"]["outcome"]]
    usage = [json.loads(x) for x in open(os.path.join(rd, "usage.jsonl"))]
    assert {u["call_kind"] for u in usage if u.get("module") == EEF} == {"eef_review"}
    rev = task["result_rev"]
    verdicts = {x["episode_index"]: x for x in (json.loads(y) for y in open(
        os.path.join(rd, "revisions", f"r{rev:04d}", "verdicts.jsonl")))}
    for ep, r in recs.items():                               # only its rejects move a verdict
        if r["passed"] is False:
            assert verdicts[ep]["verdict"] == "drop" and EEF in verdicts[ep]["hard_fails"]
        else:
            assert EEF not in (verdicts[ep].get("hard_fails") or [])
    delivered = d.delivery(task["run_id"])
    assert os.path.isfile(os.path.join(delivered, "checks", EEF, "results.jsonl"))
    assert os.path.isdir(os.path.join(delivered, "inputs"))
    rep = json.load(open(os.path.join(rd, "revisions", f"r{rev:04d}", "report.json")))
    (sec,) = [s for s in rep["modules"] if s["id"] == EEF]
    s = sec["summary"]
    assert s["judged_pass"] + s["judged_reject"] + s["to_human"] == len(recs)
    r = d.api("GET", f"/tasks/{created['id']}/report/tables/eef_review_windows")
    assert r.status_code == 200, r.text
    r = d.api("GET", f"/tasks/{created['id']}/report")
    assert r.status_code == 200 and EEF in [s["id"] for s in r.json()["report"]["modules"]]


def test_a_person_settles_what_the_module_could_not_and_the_delivery_follows(daemon):
    """F5.11: an episode the module sent to a person is a pending card; "inconsistent" rejects it and
    "consistent" keeps it once the decisions are executed, and the re-export delivers accordingly."""
    d = daemon()
    traj = _upload(d, "eef_trajectory", "trajectory.json", _bundle())
    seeds = _upload(d, "eef_observation_seeds", "seeds.jsonl", _seed_rows())
    params = {"trajectory_json": traj["handle"], "observation_seeds": seeds["handle"],
              "review_windows_per_camera": 1, "review_frames_per_window": 2}
    created = d.create(modules=[*ALL_MODULES, {"id": EEF, "params": params}])
    task_id = created["id"]
    first = d.wait(task_id)
    assert first["state"] in ("succeeded", "completed_with_errors"), json.dumps(first)[:2000]
    rd = d.run_dir(task_id)
    queue = d.api("GET", f"/tasks/{task_id}/adjudication", params={"status": "all"}).json()
    asked = sorted(c["episode_index"] for c in queue["items"]
                   if any(q["line"] == "eef_check" for q in c["questions"]))
    assert asked, queue
    assert first["pending_adjudication"] == queue["counts"]["pending"] >= len(asked)
    delivered_dir = d.delivery(first["run_id"])
    for ep in asked:
        view = d.api("GET", f"/tasks/{task_id}/episodes/{ep}").json()
        rec = view["modules"][EEF]
        assert view["list"] == "passed" and rec["passed"] is None and rec["details"]["decision"]["human"]
        # F5.12: the crops of every window, signed with scope=delivery, are in the delivery
        crops = [p for cam in rec["details"]["review"].get("cameras", {}).values()
                 for w in cam["windows"] for p in w.get("evidence") or []]
        assert all(os.path.isfile(os.path.join(delivered_dir, p)) for p in crops), crops
        assert set(crops) <= {e["path"] for e in view["evidence"] if e["module"] == EEF}
    keep, drop = asked[0], asked[-1]
    answers = [{"episode_index": drop, "line": "eef_check", "decision": "inconsistent"}]
    if keep != drop:
        answers.append({"episode_index": keep, "line": "eef_check", "decision": "consistent"})
    r = d.api("POST", f"/tasks/{task_id}/adjudication", json={"decisions": answers})
    assert r.status_code == 200, r.text
    r = d.api("POST", f"/tasks/{task_id}/adjudication/apply", json={})
    assert r.status_code == 202, r.text
    done = d.wait(task_id)
    assert done["state"] == "succeeded" and done["result_rev"] == first["result_rev"] + 1, json.dumps(done)[:2000]
    assert done["delivery_stale"] is True
    assert d.api("GET", f"/tasks/{task_id}/episodes/{drop}").json()["reasons"] == [
        {"module": EEF, "kind": "human", "text": "人工裁决判为 EEF 与视频不一致"}]
    if keep != drop:
        assert d.api("GET", f"/tasks/{task_id}/episodes/{keep}").json()["list"] == "passed"
    cards = {c["episode_index"]: c for c in d.api("GET", f"/tasks/{task_id}/adjudication",
                                                  params={"status": "all"}).json()["items"]}
    assert cards[drop]["status"] == "applied"
    with open(os.path.join(rd, "human-decisions", "eef_checks.csv"), encoding="utf-8") as fh:
        assert f"ep{drop:06d}" in fh.read()
    r = d.api("POST", f"/tasks/{task_id}/reexport")
    assert r.status_code == 202, r.text
    exported = d.wait(task_id)
    assert exported["delivery_stale"] is False
    with open(os.path.join(rd, "export", "manifest.json"), encoding="utf-8") as fh:
        delivered = {e["episode_index"] for e in json.load(fh)["episodes"]}
    assert drop not in delivered and (keep == drop or keep in delivered)
