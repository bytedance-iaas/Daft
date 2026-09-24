"""The EEF-video consistency module on the CLI chain (design doc 12 §11, §13.4; D49 / C.9).

On tools/parity's mini dataset the exterior camera shows a red block that moves with joint 0 over a
static checkerboard. A 2D-only trajectory.json declares the block centre for episodes 0-6 (episode 1
lags the video by 4 frames, episode 2 is offset by 9 px) with P-A seeds from the true centre. Since
D49 the module is a vlm-tier hard gate: it runs on the frame stage's survivors, measures with the CPU,
asks the model (the parity fake here) and passes, rejects or leaves an episode to a person; only its
rejects change the verdict. With seeds every 15 frames the 128x96 clips are too poor for the tracker,
which abstains between the seeds (``coverage_insufficient``); that is a person's question now, never a
fake ok. The verdict branches with scripted answers are in test_eef_review.py.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil

import numpy as np
import pytest

from .pipeline import Chain, read_jsonl, results, run

URL = "http://fake-vlm.test/v1"
VLM = ("--vlm-endpoint", URL, "--vlm-model", "fake-vlm", "--retry", "0")

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


@contextlib.contextmanager
def fake_vlm(tmp_path, answer=None):
    """The parity fake model in-process (the ``cli`` fixture runs the command in this process)."""
    from parity import vlm_tape as T
    from parity.fakevlm import FakeVlm

    from curation.adapters import vlm_client

    fake = FakeVlm("fake-vlm")
    if answer is not None:
        fake.answer = answer
    os.makedirs(tmp_path, exist_ok=True)
    hooks = T.TapeHooks("record", tape_out=os.path.join(str(tmp_path), f"tape-{id(fake)}.jsonl.gz"),
                        transport=fake.transport())
    hooks.install(vlm_client)
    try:
        yield fake
    finally:
        hooks.uninstall()


def test_preflight_asks_for_the_file_then_a_model(mini_dataset, tmp_path):
    traj = _files(tmp_path)
    doc = run("preflight", "--input", mini_dataset, "--modules", EEF).doc
    (entry,) = doc["modules"]
    assert entry["availability"] == "needs_input" and entry["reason_code"] == "trajectory_missing"
    assert entry["input_hint"] == {"field": "trajectory_json"}          # C1 1.5: the console asks for it
    (entry,) = run("preflight", "--input", mini_dataset, "--modules", EEF,
                   "--param", f"{EEF}.trajectory_json={traj}").doc["modules"]
    assert entry["availability"] == "needs_input" and entry["input_hint"] == {"field": "vlm"}    # D49
    (entry,) = run("preflight", "--input", mini_dataset, "--modules", EEF, "--vlm-backend", "ark",
                   "--param", f"{EEF}.trajectory_json={traj}").doc["modules"]
    assert entry["availability"] == "available"
    # the same file without seeds or a template next to it: nothing would find the gripper
    alone = tmp_path / "alone" / "trajectory.json"
    alone.parent.mkdir()
    alone.write_text(open(traj, encoding="utf-8").read())
    (bare,) = run("preflight", "--input", mini_dataset, "--modules", EEF, "--vlm-backend", "ark",
                  "--param", f"{EEF}.trajectory_json={alone}").doc["modules"]
    assert (bare["availability"], bare["reason_code"], bare["input_hint"]) == \
        ("needs_input", "observation_seed_missing", {"field": "observation_seeds"})
    assert bare["subitems"]["position_2d"]["availability"] == "needs_input"
    assert entry["episode_counts"] == {"available": 7, "unsupported": 1}           # episode 7 is not declared
    assert entry["subitems"]["position_2d"]["availability"] == "available"
    assert entry["subitems"]["orientation_2d"] == {"availability": "unsupported",
                                                   "reason_code": "axis_mapping_missing"}
    assert entry["subitems"]["state_motion"]["reason_code"] == "pose_semantics_unknown"
    bad = run("preflight", "--input", mini_dataset, "--param", f"{EEF}.no_such=1")
    assert bad.rc != 0 and "no parameter" in bad.doc["error"]["message"]


def test_check_usage_and_whole_module_failures(mini_dataset, tmp_path):
    rd = str(tmp_path / "run")
    missing = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1")
    assert missing.rc != 0 and "trajectory_json" in missing.doc["error"]["message"]
    mixed = run("check", "--modules", f"visual_quality,{EEF}", "--input", mini_dataset, "--run-dir", rd,
                "--episodes", "0-1", "--param", f"{EEF}.trajectory_json=/nope")
    assert mixed.rc != 0 and "one call runs one stage" in mixed.doc["error"]["message"]
    corrupt = _files(tmp_path, corrupt=True)
    bad = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1",
              "--param", f"{EEF}.trajectory_json={corrupt}")
    assert bad.rc == 4 and "truth" in bad.doc["error"]["message"]
    no_model = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-1",
                   "--param", f"{EEF}.trajectory_json={_files(tmp_path / 'ok')}",
                   "--vlm-endpoint", "http://127.0.0.1:9/v1", "--vlm-model", "none")     # nothing listens there
    assert no_model.rc == 4 and "VLM endpoint cannot be used" in no_model.doc["error"]["message"]
    assert not os.path.exists(os.path.join(rd, "checks", EEF, "results.jsonl"))   # probed before any episode


def _check(cli, dataset, rd, traj, *extra):
    res = cli("check", "--modules", EEF, "--input", dataset, "--run-dir", rd, "--episodes", "0-2",
              "--param", f"{EEF}.trajectory_json={traj}", *VLM, *extra)
    assert res.rc == 0, res.doc
    return res.doc["modules"][EEF]


def test_another_file_or_other_seeds_are_another_input(cli, mini_dataset, tmp_path):
    """F5.5 acceptance 2: the file's hash is part of the input; --resume redoes what another file made."""
    rd = str(tmp_path / "run")
    traj = _files(tmp_path / "a")
    with fake_vlm(tmp_path):
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
        other_model = cli("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", rd, "--episodes", "0-2",
                          "--param", f"{EEF}.trajectory_json={other}", "--param", f"{EEF}.lag_search_s=0.5",
                          "--vlm-endpoint", URL, "--vlm-model", "fake-vlm-2", "--retry", "0", "--resume")
        assert other_model.rc == 0 and other_model.doc["modules"][EEF]["skipped_existing"] == 0


def test_a_remote_dataset_streams_the_media_it_needs(cli, cloud, mini_dataset, tmp_path, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "datasets/mini")
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    rd = str(tmp_path / "run")
    with fake_vlm(tmp_path):
        doc = _check(cli, "tos://src-bucket/datasets/mini", rd, _files(tmp_path))
    assert doc["episodes"]["error"] == 0 and doc["episodes"]["total"] == 3
    recs = results(rd, EEF)
    assert all(recs[e]["details"]["cameras"][CAM]["subitems"]["position_2d"]["points"]["block_center"]
               ["coverage"]["requested"] == len(_truth(e)) for e in range(3))
    fetched = {c[2] for c in cloud.calls if c[0] == "get" and "/videos/" in c[2]}
    assert fetched == {f"datasets/mini/videos/chunk-000/observation.images.exterior/episode_{e:06d}.mp4"
                       for e in range(3)}


@pytest.fixture(scope="module")
def chain(vlm_stage, tmp_path_factory):
    """The reference run through final + report as r0001, then the EEF module on the frame stage's
    survivors against the fake model, then r0002 with it."""
    from .fakevlm_server import FakeVlmServer

    tmp = tmp_path_factory.mktemp("eef-chain")
    rd = str(tmp / "run")
    shutil.copytree(vlm_stage["reference_dir"], rd)
    traj = _files(tmp)
    ds = vlm_stage["dataset"]
    survivors = [int(x) for x in open(vlm_stage["episodes"][1:]).read().split()]
    with FakeVlmServer() as vlm:
        c = Chain(ds, rd, vlm.url)
        c.post(revision=1)
        eef = c.step("eef", "check", "--modules", EEF, *c.common(), *c.vlm, "--episodes", vlm_stage["episodes"],
                     "--param", f"{EEF}.trajectory_json={traj}", "--survivors-out", c.path("stages", "eef.txt"))
        mods = f"{MODS},{EEF}"
        c.step("funnel2", "aggregate", "--run-dir", rd, "--phase", "funnel", "--revision", "2",
               "--episodes", "0-7", "--modules", mods)
        c.step("final2", "aggregate", "--run-dir", rd, "--phase", "final", "--revision", "2", "--episodes", "0-7",
               "--modules", mods, "--input", ds)
        c.step("report2", "report", "--run-dir", rd, "--revision", "2", "--modules", mods)
    return {"rd": rd, "chain": c, "eef": eef, "survivors": survivors}


def test_the_frame_survivors_are_judged(chain):
    rd, survivors = chain["rd"], chain["survivors"]
    recs = results(rd, EEF)
    assert sorted(recs) == sorted(survivors)                      # D49: the funnel's survivors, not the selection
    counts = chain["eef"].doc["modules"][EEF]["episodes"]
    assert counts["total"] == len(survivors) and counts["error"] == 0
    for ep, r in recs.items():
        d = r["details"]
        assert r["gate"] == "hard" and r["score"] is None and d["assessment_mode"] == "verdict"
        want = {"pass": True, "reject": False, "human": None}[d["decision"]["outcome"]]
        assert r["passed"] is want and r["verdict"] == {True: "pass", False: "fail", None: "abstain"}[want]
        if ep == 7:
            assert d["decision"]["human"][0]["code"] == "not_in_file"
            continue
        assert d["schema_version"] == "eef-detail/0.1" and d["uncalibrated"] and d["input_file_sha256"]
        pos = d["cameras"][CAM]["subitems"]["position_2d"]
        cov = pos["points"]["block_center"]["coverage"]
        assert cov["requested"] == cov["media_mapped"] == len(_truth(ep))
        if pos["status"] == "unknown":                            # honest abstention -> a person, never a fake ok
            assert "coverage_insufficient" in pos["reasons"] and d["decision"]["outcome"] == "human"
        assert d["review"]["status"] in ("completed", "incomplete", "not_reviewed")
    kept = open(os.path.join(rd, "stages", "eef.txt")).read().split()
    assert kept == [str(e) for e in sorted(recs) if recs[e]["passed"] is not False]
    for rec in recs.values():
        for path in rec["evidence"]:
            assert os.path.isfile(os.path.join(rd, path))


def test_only_its_rejects_move_the_verdict(chain):
    """F5.9 acceptance 3: an episode the module passes or leaves to a person keeps its funnel verdict."""
    recs = results(chain["rd"], EEF)
    r1 = {x["episode_index"]: x for x in read_jsonl(os.path.join(chain["rd"], "revisions", "r0001", "verdicts.jsonl"))}
    r2 = {x["episode_index"]: x for x in read_jsonl(os.path.join(chain["rd"], "revisions", "r0002", "verdicts.jsonl"))}
    for ep in r1:
        if ep in recs and recs[ep]["passed"] is False:
            assert r2[ep]["verdict"] == "drop" and "EEF–视频一致性" in r2[ep]["reason"]
        else:
            assert r2[ep]["verdict"] == r1[ep]["verdict"], ep


def test_the_report_shows_the_eef_section(chain):
    rep = json.loads(open(os.path.join(chain["rd"], "revisions", "r0002", "report.json")).read())
    (sec,) = [s for s in rep["modules"] if s["id"] == EEF]
    s = sec["summary"]
    n = len(chain["survivors"])
    assert s["judged_pass"] + s["judged_reject"] + s["to_human"] == n and s["uncalibrated"]
    assert s["threshold_profile"].startswith("demo ") and sec["gate"] == "hard"
    assert all(set(x) == {"name", "count"} for x in s["human_reasons"])
    assert s["windows"] >= s["windows_answered"] and "model_cpu_agreement" in s
    tables = {t["id"]: t for t in sec["tables"]}
    assert set(tables) == {"eef_camera_metrics", "eef_segments", "eef_diagnosis", "eef_review_windows"}
    import pandas as pd

    tdir = os.path.join(chain["rd"], "revisions", "r0002", "tables")
    cams = pd.read_parquet(os.path.join(tdir, "eef_camera_metrics.parquet"))
    assert {"episode_index", "camera", "position", "lag_s", "coverage"} <= set(cams.columns)
    assert set(cams["episode_index"]) == set(chain["survivors"])
    wins = pd.read_parquet(os.path.join(tdir, "eef_review_windows.parquet"))
    assert {"episode_index", "camera", "kind", "point", "status", "review_status"} <= set(wins.columns)
    md = open(os.path.join(chain["rd"], "revisions", "r0002", "report.md"), encoding="utf-8").read()
    assert "判过" in md and "转人工的原因" in md and "待人工看" not in md
    # the cards the adjudication page asks (F5.12): the ones sent to a person that are still delivered
    review = json.load(open(os.path.join(chain["rd"], "revisions", "r0002", "review.json")))["episodes"]
    asked = sum(1 for e in review if any(i["kind"] == "eef_consistency" for i in e["review"]))
    assert sec["adjudication"]["pending"] == asked <= s["to_human"]
    assert f"- 人工裁决:待裁 {asked} 条" in md
