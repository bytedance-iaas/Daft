"""F5.3 end to end on rendered clips (form A, 2D projections only): decode -> observe -> measure ->
assess -> diagnose -> artifacts. Baseline, a record that lags the video, a constant image offset, a
shaken camera, and no threshold profile."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from curation.contracts import schemas
from curation.extensions.eef_consistency import contracts as C
from curation.extensions.eef_consistency import load, profile, runner

from . import synth

N = 90
CAM = "cam0"


def _scene(tmp_path, *, shake=False, **declare):
    root = tmp_path / "lerobot"
    (root / "videos/cam").mkdir(parents=True)
    wobble = None
    if shake:
        t = np.arange(N) / synth.FPS
        wobble = np.stack([5 * np.sin(2 * np.pi * 4.0 * t), 4 * np.sin(2 * np.pi * 5.3 * t + 1)], 1)
    truth = synth.render_gripper_video(root / "videos/cam/episode_000000.mp4", N, shake=wobble)
    seeds = tmp_path / "seeds"
    synth.write_seeds(seeds / "synthetic_000000" / f"{CAM}.jsonl", truth, sample_id="synthetic_000000", camera_id=CAM)
    entry = synth.make_entry_2d(truth, **declare)
    r = load.load_bundle(json.dumps(synth.make_bundle([entry])).encode(), lerobot_root=root)
    assert r.ok, [i.as_dict() for i in r.errors[:3]]
    return r.samples[0], root, seeds


def _run(tmp_path, sample, root, seeds, prof="demo", evidence="flagged"):
    cfg = runner.RunConfig(lerobot_root=str(root), seed_root=str(seeds), profile=profile.load(prof),
                           out_dir=str(tmp_path / "out"), evidence_mode=evidence)
    detail, measure = runner.run_episode(sample, cfg)
    json.dumps(detail, allow_nan=False)                                  # the detail is strict JSON
    return detail, measure


def _supported(detail):
    return {h["hypothesis"] if h["hypothesis"] != "jitter_source" else f"jitter_source={h['fitted']['source']}"
            for h in detail["diagnosis"] if h["supported"]}


def test_baseline_is_assessed_ok_and_writes_artifacts(tmp_path):
    sample, root, seeds = _scene(tmp_path)
    d, _ = _run(tmp_path, sample, root, seeds, evidence="all")
    st = {k: v["status"] for k, v in d["summary"].items()}
    assert st[C.POSITION] == st[C.ORIENTATION] == st[C.TEMPORAL] == st[C.CAMERA_MOTION] == C.OK, st
    assert st[C.STATE_MOTION] == C.UNSUPPORTED and st[C.INPUT_CONSISTENCY] == C.UNSUPPORTED
    assert d["overall"] == "assessed" and not _supported(d)
    assert d["uncalibrated"] is True and d["threshold_profile"]["name"] == "demo"
    assert d["schema_version"] == C.DETAIL_SCHEMA_VERSION and len(d["input_hash"]) == 64
    cov = d["cameras"][CAM]["subitems"][C.POSITION]["points"]["tcp"]["coverage"]
    assert cov["requested"] == N and cov["coverage"] > 0.8
    obs = d["cameras"][CAM]["observation"]
    assert obs["not_for_accuracy_acceptance"] and obs["seed_method"] == "synthetic_fixture"
    out = tmp_path / "out"
    rows = [json.loads(x) for x in (out / "observations/000000" / f"{CAM}.jsonl").read_text().splitlines()]
    assert len(rows) == N and all(not schemas.errors("eef/observation.schema.json", r) for r in rows[:5])
    curves = pd.read_parquet(out / "curves/000000" / f"{CAM}.parquet")
    assert {"frame_index", "t_s", "e_px:tcp", "angle_deg:finger_line", "bg_hf_rms_px"} <= set(curves.columns)
    assert d["evidence"] and all((out / e["path"]).is_file() for e in d["evidence"])


def test_record_lagging_the_video_is_a_positive_lag(tmp_path):
    sample, root, seeds = _scene(tmp_path, shift_frames=5)
    d, _ = _run(tmp_path, sample, root, seeds)
    cell = d["cameras"][CAM]["subitems"][C.TEMPORAL]
    assert cell["status"] == C.SUSPECT and cell["metrics"]["lag_s"] == pytest.approx(5 / synth.FPS, abs=0.02)
    assert d["overall"] == "candidate"
    (h,) = [h for h in d["diagnosis"] if h["hypothesis"] == "time_offset"]
    assert h["supported"] and h["fitted"]["lag_s"] > 0 and h["residual_after_px"] < h["residual_before_px"] / 3


def test_constant_offset_is_a_position_candidate_with_evidence(tmp_path):
    sample, root, seeds = _scene(tmp_path, offset_px=(14.0, 0.0))
    d, _ = _run(tmp_path, sample, root, seeds)
    cell = d["cameras"][CAM]["subitems"][C.POSITION]
    assert cell["status"] == C.SUSPECT and "position_median_above_threshold" in cell["reasons"]
    assert cell["points"]["tcp"]["metrics"]["median_u_px"] == pytest.approx(14.0, abs=1.5)
    assert d["summary"][C.TEMPORAL]["status"] == C.OK
    assert d["evidence"] and all(e["camera_id"] == CAM for e in d["evidence"])
    assert "time_offset" not in _supported(d)


def test_shaken_camera_is_camera_motion_and_the_video_is_blamed(tmp_path):
    sample, root, seeds = _scene(tmp_path, shake=True)
    d, _ = _run(tmp_path, sample, root, seeds)
    assert d["summary"][C.CAMERA_MOTION]["status"] == C.SUSPECT
    assert "jitter_source=video" in _supported(d)


def test_without_a_profile_only_curves_come_out(tmp_path):
    sample, root, seeds = _scene(tmp_path)
    d, _ = _run(tmp_path, sample, root, seeds, prof="none")
    statuses = {k: v["status"] for k, v in d["summary"].items()}
    assert C.OK not in statuses.values() and C.SUSPECT not in statuses.values()
    assert C.THRESHOLD_UNCALIBRATED in d["cameras"][CAM]["subitems"][C.POSITION]["reasons"]
    assert d["diagnosis"] == [] and d["threshold_profile"] is None
    assert (tmp_path / "out/curves/000000" / f"{CAM}.parquet").is_file()


def test_missing_seeds_leave_position_needs_input_but_camera_motion_runs(tmp_path):
    sample, root, _ = _scene(tmp_path)
    d, _ = _run(tmp_path, sample, root, tmp_path / "no-seeds")
    assert d["capabilities"][C.POSITION] == {"availability": C.NEEDS_INPUT, "reason_code": C.OBSERVATION_SEED_MISSING}
    assert d["cameras"][CAM]["subitems"][C.POSITION]["status"] == C.UNSUPPORTED
    assert d["summary"][C.CAMERA_MOTION]["status"] == C.OK
