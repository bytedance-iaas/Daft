"""F5.4: the advisory EEF-video consistency module on the CLI chain (design doc 12 §11, §13.4).

On tools/parity's mini dataset the exterior camera shows a red block that moves with joint 0 over a
static checkerboard. A 2D-only trajectory.json declares the block centre for episodes 0-6 (episode 1
lags the video by 4 frames, episode 2 is offset by 9 px) with P-A seeds from the true centre. The
module runs on every selected episode - including those the old hard gates reject - and adding it to
the task leaves the funnel verdicts, keep set and final lists as they were. The 128x96 clips with a
flat-coloured block are too poor for the P-A tracker, which abstains between the seeds
(``coverage_insufficient``) instead of claiming anything: this test is about the wiring; what the
measurements say is tested on rendered scenes (tests/eef) and the DEMO data (tools/eef_eval).
"""
from __future__ import annotations

import filecmp
import json
import os
import shutil

import numpy as np
import pytest

from .pipeline import Chain, read_jsonl, results, run

MODS = ("timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,"
        "dedup,skill_profile")
EEF = "eef_video_consistency"
CAM = "exterior"


def _truth(ep: int) -> np.ndarray:
    from parity import fixtures as F

    src = F.DUPLICATE_OF.get(ep, ep)
    q = F._joint_track(F.EPISODES[src][0], src)
    lo, hi = float(q[:, 0].min()), float(q[:, 0].max())
    pos = (q[:, 0] - lo) / max(hi - lo, 1e-6)
    cx = np.array([int(12 + p * (F.WIDTH - 36)) for p in pos], float)
    return np.stack([cx + 11.5, np.full(len(cx), F.HEIGHT // 2 - 12 + 11.5)], 1)


def _entry(ep: int, *, shift: int = 0, offset: float = 0.0) -> dict:
    from parity import fixtures as F

    uv = _truth(ep)
    n = len(uv)
    sid = f"mini_{ep:06d}"
    prov = {"source": "test", "method": "fixture geometry", "assurance": "synthetic"}
    frames = []
    for i in range(n):
        u, v = uv[min(max(i - shift, 0), n - 1)]
        u += offset
        pt = {"uv_px": [float(u), float(v)], "depth_m": None, "status": "valid", "in_frame": True}
        frames.append({"schema_version": "eef-video/1.0.0", "sample_id": sid, "frame_index": i, "timestamp_s": i / F.FPS,
                       "source_state_index": None, "source_timing": [], "eef": None, "gripper": None,
                       "cameras": {CAM: {"video_frame_index": i, "video_timestamp_s": i / F.FPS,
                                         "image_size_wh": [F.WIDTH, F.HEIGHT], "calibration_id": None,
                                         "T_reference_camera": None, "H_media_from_calibration": np.eye(3).tolist(),
                                         "projection": {"source": "provided", "pixel_space": "media",
                                                        "points": {"block_center": pt}}}}})
    sample = {"schema_version": "eef-video/1.0.0", "sample_id": sid,
              "source": {"dataset": "mini", "episode_id": str(ep), "instruction": None}, "frame_count": n,
              "timebase": "video_pts", "annotations_path": "#frames", "calibration_path": None,
              "eef_frame": None, "reference_frame": None,
              "views": [{"view_id": CAM, "kind": "camera", "camera_id": CAM, "mount": "fixed_external",
                         "media": {"kind": "video",
                                   "uri": f"videos/chunk-000/observation.images.exterior/episode_{ep:06d}.mp4",
                                   "image_size_wh": [F.WIDTH, F.HEIGHT], "frame_count": n, "fps": float(F.FPS),
                                   "clip_start_s": 0.0, "clip_end_s": n / F.FPS}}],
              "point_definitions": {"block_center": {"meaning": "centre of the red block", "frame_id": None,
                                                     "model": "external_2d", "position_eef_m": None,
                                                     "position_open_eef_m": None, "position_closed_eef_m": None,
                                                     "provenance": prov}},
              "axis_definitions": {}, "raw_pose_sequence": None, "notes": ["test fixture"]}
    return {"episode_index": ep, "sample": sample, "calibration": None, "frames": frames}


def _files(tmp_path, *, corrupt: bool = False, seed_every: int = 15) -> str:
    """trajectory.json for episodes 0-6 plus observations_seed/ next to it; returns its path."""
    d = tmp_path / "eef"
    entries = [_entry(ep, shift=4 if ep == 1 else 0, offset=9.0 if ep == 2 else 0.0) for ep in range(7)]
    if corrupt:
        entries[3]["frames"][10]["cameras"][CAM]["projection"]["points"]["block_center"]["truth"] = [1, 2]
    bundle = {"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
              "dataset": {"id": "mini", "lerobot_codebase_version": "v2.1", "fps": 15.0, "episode_count": 8},
              "media_uri_base": "lerobot_root", "samples": entries}
    (d / "observations_seed").mkdir(parents=True)
    (d / "trajectory.json").write_text(json.dumps(bundle))
    for ep in range(7):
        uv = _truth(ep)
        rows = [{"schema_version": "eef-video/1.0.0", "sample_id": f"mini_{ep:06d}", "frame_index": i,
                 "camera_id": CAM, "video_frame_index": i, "pixel_space": "media", "method": "synthetic_fixture",
                 "model_version": "test", "input_image_sha256": "0" * 64, "projection_visible_to_localizer": False,
                 "points": {"block_center": {"uv_px": [float(uv[i, 0]), float(uv[i, 1])], "visibility": "visible",
                                             "confidence": 1.0, "uncertainty_px": None}}}
                for i in range(0, len(uv), seed_every)]
        (d / "observations_seed" / f"mini_{ep:06d}").mkdir()
        (d / "observations_seed" / f"mini_{ep:06d}" / f"{CAM}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
    return str(d / "trajectory.json")


def test_preflight_reports_the_file_and_the_sub_items(mini_dataset, tmp_path):
    traj = _files(tmp_path)
    doc = run("preflight", "--input", mini_dataset, "--modules", f"{EEF},eef_video_review").doc
    by = {m["id"]: m for m in doc["modules"]}
    assert by[EEF]["availability"] == "needs_input" and by[EEF]["reason_code"] == "trajectory_missing"
    assert by[EEF]["input_hint"] == {"field": "trajectory_json"}          # C1 1.5: the console asks for it
    assert by["eef_video_review"]["availability"] == "needs_input"      # F5.6: it follows the module it
    assert by["eef_video_review"]["input_hint"] == {"field": "trajectory_json"}   # reviews
    doc = run("preflight", "--input", mini_dataset, "--modules", EEF,
              "--param", f"{EEF}.trajectory_json={traj}").doc
    (entry,) = doc["modules"]
    assert entry["availability"] == "available"
    assert entry["episode_counts"] == {"available": 7, "unsupported": 1}           # episode 7 is not declared
    assert entry["subitems"]["position_2d"]["availability"] == "available"
    assert entry["subitems"]["orientation_2d"] == {"availability": "unsupported",
                                                   "reason_code": "axis_mapping_missing"}
    assert entry["subitems"]["state_motion"]["reason_code"] == "pose_semantics_unknown"
    rev = {m["id"]: m for m in run("preflight", "--input", mini_dataset, "--modules", f"{EEF},eef_video_review",
                                   "--param", f"{EEF}.trajectory_json={traj}").doc["modules"]}["eef_video_review"]
    assert rev["availability"] == "needs_input" and rev["input_hint"] == {"field": "vlm"}
    rev = {m["id"]: m for m in run("preflight", "--input", mini_dataset, "--modules", "eef_video_review",
                                   "--vlm-backend", "ark", "--param", f"{EEF}.trajectory_json={traj}").doc["modules"]}
    assert rev["eef_video_review"]["availability"] == "available"
    gone = run("preflight", "--input", mini_dataset, "--modules", "eef_video_review", "--vlm-backend", "ark",
               "--param", f"{EEF}.trajectory_json={traj}.nope").doc["modules"][0]
    assert gone["availability"] == "unsupported" and gone["reason_code"] == "eef_base_unavailable"
    bad = run("preflight", "--input", mini_dataset, "--param", f"{EEF}.no_such=1")
    assert bad.rc != 0 and "no parameter" in bad.doc["error"]["message"]


def test_check_usage_and_whole_module_failures(mini_dataset, tmp_path):
    rd = str(tmp_path / "run")
    missing = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1")
    assert missing.rc != 0 and "trajectory_json" in missing.doc["error"]["message"]
    mixed = run("check", "--modules", f"visual_quality,{EEF}", "--input", mini_dataset, "--run-dir", rd,
                "--episodes", "0-1", "--param", f"{EEF}.trajectory_json=/nope")
    assert mixed.rc != 0 and "call of their own" in mixed.doc["error"]["message"]
    corrupt = _files(tmp_path, corrupt=True)
    bad = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1",
              "--param", f"{EEF}.trajectory_json={corrupt}")
    assert bad.rc == 4 and "truth" in bad.doc["error"]["message"]
    review = run("check", "--modules", "eef_video_review", "--input", mini_dataset, "--run-dir", rd,
                 "--episodes", "0-1", "--param", f"{EEF}.trajectory_json={corrupt}")
    assert review.rc != 0


def _check(cli, dataset, rd, traj, *extra):
    res = cli("check", "--modules", EEF, "--input", dataset, "--run-dir", rd, "--episodes", "0-2",
              "--param", f"{EEF}.trajectory_json={traj}", *extra)
    assert res.rc == 0, res.doc
    return res.doc["modules"][EEF]


def test_another_file_or_other_seeds_are_another_input(cli, mini_dataset, tmp_path):
    """F5.5 acceptance 2: the file's hash is part of the input; --resume redoes what another file made."""
    rd = str(tmp_path / "run")
    traj = _files(tmp_path / "a")
    first = _check(cli, mini_dataset, rd, traj)
    same = _check(cli, mini_dataset, rd, traj, "--resume")
    assert same["skipped_existing"] == 3 and same["input_digest"] == first["input_digest"]
    doc = json.loads(open(traj).read())                              # another file: one point moved
    doc["samples"][0]["frames"][0]["cameras"][CAM]["projection"]["points"]["block_center"]["uv_px"][0] += 1.0
    other = _files(tmp_path / "b")
    open(other, "w").write(json.dumps(doc))
    moved = _check(cli, mini_dataset, rd, other, "--resume")
    assert moved["skipped_existing"] == 0 and moved["input_digest"] != first["input_digest"]
    import hashlib

    sha = {r["details"]["input_file_sha256"] for r in results(rd, EEF).values()}
    assert sha == {hashlib.sha256(open(other, "rb").read()).hexdigest()}
    seed = os.path.join(os.path.dirname(other), "observations_seed", "mini_000001", f"{CAM}.jsonl")
    rows = open(seed).read().splitlines()
    open(seed, "w").write("\n".join(rows[:-1]) + "\n")                # other seeds for episode 1 only
    reseeded = _check(cli, mini_dataset, rd, other, "--resume")
    assert reseeded["skipped_existing"] == 2 and reseeded["input_digest"] != moved["input_digest"]
    changed = _check(cli, mini_dataset, rd, other, "--resume", "--param", f"{EEF}.lag_search_s=0.5")
    assert changed["skipped_existing"] == 0                          # another configuration


def test_a_remote_dataset_streams_the_media_it_needs(cli, cloud, mini_dataset, tmp_path, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "datasets/mini")
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    rd = str(tmp_path / "run")
    doc = _check(cli, "tos://src-bucket/datasets/mini", rd, _files(tmp_path))
    assert doc["episodes"]["error"] == 0 and doc["episodes"]["abstain"] == 3
    recs = results(rd, EEF)
    assert all(recs[e]["details"]["cameras"][CAM]["subitems"]["position_2d"]["points"]["block_center"]
               ["coverage"]["requested"] == len(_truth(e)) for e in range(3))
    fetched = {c[2] for c in cloud.calls if c[0] == "get" and "/videos/" in c[2]}
    assert fetched == {f"datasets/mini/videos/chunk-000/observation.images.exterior/episode_{e:06d}.mp4"
                       for e in range(3)}


@pytest.fixture(scope="module")
def chain(vlm_stage, tmp_path_factory):
    """The reference run through final + report as r0001, then the EEF check, then r0002 with it."""
    from .fakevlm_server import FakeVlmServer

    tmp = tmp_path_factory.mktemp("eef-chain")
    rd = str(tmp / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    traj = _files(tmp)
    ds = vlm_stage["dataset"]
    with FakeVlmServer() as vlm:
        c = Chain(ds, rd, vlm.url)
        c.post(revision=1)
        eef = c.step("eef", "check", "--modules", EEF, *c.common(), "--episodes", "0-7",
                     "--param", f"{EEF}.trajectory_json={traj}", "--survivors-out", c.path("stages", "eef.txt"))
        review = c.step("review", "check", "--modules", "eef_video_review", *c.common(), *c.vlm, "--episodes",
                        "0-7", "--param", f"{EEF}.trajectory_json={traj}", "--survivors-out",
                        c.path("stages", "review.txt"))
        mods = f"{MODS},{EEF},eef_video_review"
        c.step("funnel2", "aggregate", "--run-dir", rd, "--phase", "funnel", "--revision", "2",
               "--episodes", "0-7", "--modules", mods)
        c.step("final2", "aggregate", "--run-dir", rd, "--phase", "final", "--revision", "2", "--episodes", "0-7",
               "--modules", mods, "--input", ds)
        c.step("report2", "report", "--run-dir", rd, "--revision", "2", "--modules", mods)
    return {"rd": rd, "chain": c, "eef": eef, "review": review}


def test_every_selected_episode_is_assessed_including_rejected_ones(chain):
    rd = chain["rd"]
    assert chain["eef"].doc["modules"][EEF]["episodes"] == {"total": 8, "pass": 0, "fail": 0, "abstain": 8,
                                                             "scored": 0, "error": 0}
    recs = results(rd, EEF)
    assert sorted(recs) == list(range(8))
    rejected = [e for e, r in results(rd, "timestamp_check").items() if r["verdict"] == "fail"]
    assert rejected and all(e in recs and recs[e]["details"]["overall"] for e in rejected)   # not the survivors
    assert all(r["passed"] is None and r["score"] is None and r["gate"] == "none" for r in recs.values())
    assert recs[7]["details"]["reasons"] == ["projection_missing"]
    assert open(os.path.join(rd, "stages", "eef.txt")).read().split() == [str(e) for e in range(8)]
    for ep in range(7):
        d = recs[ep]["details"]
        assert d["schema_version"] == "eef-detail/0.1" and d["uncalibrated"] and d["input_file_sha256"]
        pos = d["cameras"][CAM]["subitems"]["position_2d"]
        cov = pos["points"]["block_center"]["coverage"]
        assert cov["requested"] == cov["media_mapped"] == len(_truth(ep))
        if pos["status"] == "unknown":                                  # honest abstention, never a fake ok
            assert "coverage_insufficient" in pos["reasons"]
        assert d["cameras"][CAM]["observation"]["seed_method"] == "synthetic_fixture"
    for rec in recs.values():
        for path in rec["evidence"]:
            assert os.path.isfile(os.path.join(rd, path))


def test_the_review_runs_on_every_selected_episode_and_reports(chain):
    rd = chain["rd"]
    assert chain["review"].doc["modules"]["eef_video_review"]["episodes"]["abstain"] == 8
    recs = results(rd, "eef_video_review")
    assert sorted(recs) == list(range(8)) and recs[7]["details"]["reasons"] == ["projection_missing"]
    assert open(os.path.join(rd, "stages", "review.txt")).read().split() == [str(e) for e in range(8)]
    rep = json.load(open(os.path.join(rd, "revisions", "r0002", "report.json")))
    (sec,) = [s for s in rep["modules"] if s["id"] == "eef_video_review"]
    assert sec["summary"]["reviewed"] == 7 and sec["summary"]["not_reviewed"] == 1
    assert os.path.isfile(os.path.join(rd, "revisions", "r0002", "tables", "eef_review_windows.parquet"))
    md = open(os.path.join(rd, "revisions", "r0002", "report.md"), encoding="utf-8").read()
    assert "建议性复核，不影响判决" in md


def test_the_verdict_and_the_delivered_lists_do_not_move(chain):
    r1 = os.path.join(chain["rd"], "revisions", "r0001")
    r2 = os.path.join(chain["rd"], "revisions", "r0002")
    for name in ("verdicts.jsonl", "keep.txt"):                      # the funnel: byte for byte
        assert filecmp.cmp(os.path.join(r1, name), os.path.join(r2, name), shallow=False), name
    for name in ("passed.json", "reject.json", "held.json", "review.json", "label_audit.json"):
        a = json.loads(open(os.path.join(r1, name)).read())             # the four lists and the audit:
        b = json.loads(open(os.path.join(r2, name)).read())             # equal but for their own revision
        for doc in (a, b):
            if isinstance(doc, dict):
                doc.pop("revision", None)
        assert a == b, name


def test_the_report_shows_the_advisory_section(chain):
    rep = json.loads(open(os.path.join(chain["rd"], "revisions", "r0002", "report.json")).read())
    (sec,) = [s for s in rep["modules"] if s["id"] == EEF]
    adv = sec["summary"]
    assert adv["assessment_mode"] == "advisory" and adv["affects_dataset_verdict"] is False and adv["uncalibrated"]
    assert adv["threshold_profile"].startswith("demo ")
    assert sum(adv[k] for k in ("candidates", "assessed", "partially_assessable", "not_assessable", "errors")) == 8
    assert adv["not_assessable"] >= 1
    for k in ("position_2d", "temporal_alignment", "camera_motion", "state_motion"):
        assert sum(adv["subitem_status"][k].values()) == 8, k
    assert adv["subitem_status"]["state_motion"]["unsupported"] == 8         # 2D-only file: no pose
    assert adv["cameras_measured"] == 7 and adv["coverage_median"] is not None
    assert all(set(x) == {"name", "count"} for x in adv["unknown_by_subitem"])
    assert adv["counts"]["abstain"] == 8 and sec["gate"] == "none"
    tables = {t["id"]: t for t in sec["tables"]}
    assert set(tables) == {"eef_camera_metrics", "eef_segments", "eef_diagnosis"}
    import pandas as pd

    tdir = os.path.join(chain["rd"], "revisions", "r0002", "tables")
    cams = pd.read_parquet(os.path.join(tdir, "eef_camera_metrics.parquet"))
    assert {"episode_index", "camera", "position", "lag_s", "coverage"} <= set(cams.columns)
    assert set(cams["episode_index"]) == set(range(8))
    assert {"camera", "subitem", "start_s", "duration_s"} <= set(
        pd.read_parquet(os.path.join(tdir, "eef_segments.parquet")).columns)
    assert {"camera", "hypothesis"} <= set(pd.read_parquet(os.path.join(tdir, "eef_diagnosis.parquet")).columns)
    old = json.loads(open(os.path.join(chain["rd"], "revisions", "r0001", "report.json")).read())
    assert EEF not in [s["id"] for s in old["modules"]]
    assert read_jsonl(os.path.join(chain["rd"], "revisions", "r0002", "verdicts.jsonl"))
