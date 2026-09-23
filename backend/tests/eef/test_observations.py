"""F5.2: decoding on the clip timeline, the provider whitelist, the P-A tracker and observation files."""
from __future__ import annotations

import copy
import dataclasses
import json

import numpy as np
import pytest

from curation.contracts import schemas
from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency import observations as O
from curation.extensions.eef_consistency import tracking as T
from curation.extensions.eef_consistency import video as V

from . import synth

N = 60
CAM = "cam0"


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    root = tmp_path_factory.mktemp("lerobot")
    (root / "videos/cam").mkdir(parents=True)
    truth = synth.render_gripper_video(root / "videos/cam/episode_000000.mp4", N)
    seed_root = root.parent / "seeds"
    synth.write_seeds(seed_root / "synthetic_000000" / f"{CAM}.jsonl", truth, sample_id="synthetic_000000",
                      camera_id=CAM)
    entry = synth.make_entry(0, N)
    r = load.load_bundle(json.dumps(synth.make_bundle([entry])).encode(), lerobot_root=root)
    assert r.ok
    return {"root": root, "seed_root": seed_root, "truth": truth, "entry": entry, "sample": r.samples[0]}


def _locate(sample, root, seeds, provider=None):
    ctx, targets = O.provider_inputs(sample, CAM, media_root=root, seeds=seeds)
    frames = V.iter_clip(ctx.media_path, clip_start_s=ctx.clip_start_s, clip_end_s=ctx.clip_end_s, fps=ctx.fps,
                         frame_count=ctx.media_frame_count)
    return (provider or T.SeededLKProvider()).locate(frames, targets, ctx)


def test_decoder_numbers_frames_from_the_clip_start(tmp_path):
    """A v3 file concatenates episodes: the clip starts mid-file and its frames count from 0."""
    truth = synth.render_gripper_video(tmp_path / "v.mp4", 30, lead_frames=12)
    frames = list(V.iter_clip(str(tmp_path / "v.mp4"), clip_start_s=12 / synth.FPS, clip_end_s=42 / synth.FPS,
                              fps=synth.FPS, frame_count=30))
    assert [f.index for f in frames] == list(range(30))
    assert frames[0].pts_s == pytest.approx(0.0, abs=1e-6)
    # the lead-in frames are flat grey; the clip's first frame is the textured scene
    assert frames[0].gray.std() > 20
    assert len(truth["tcp"]) == 30


def test_decoder_reports_unreadable_media(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(V.DecodeError):
        list(V.iter_clip(str(bad), clip_start_s=0, clip_end_s=None, fps=15))


def test_provider_inputs_carry_no_projection_pose_or_calibration(scene):
    seeds = O.find_seeds(scene["seed_root"], "synthetic_000000", CAM)
    ctx, targets = O.provider_inputs(scene["sample"], CAM, media_root=scene["root"], seeds=seeds)
    assert {f.name for f in dataclasses.fields(ctx)} == {
        "sample_id", "camera_id", "media_path", "image_size_wh", "media_frame_count", "fps", "clip_start_s",
        "clip_end_s"}
    assert {f.name for f in dataclasses.fields(targets)} == {"point_ids", "seeds"}
    blob = json.dumps({"ctx": dataclasses.asdict(ctx), "targets": targets.point_ids,
                       "seed_fields": [f.name for f in dataclasses.fields(seeds)]}, default=str)
    for leaked in ("projection", "uv_px", "calibration", "T_reference", "eef", "notes", "truth"):
        assert leaked not in blob


def test_tracker_follows_the_rigid_gripper(scene):
    seeds = O.find_seeds(scene["seed_root"], "synthetic_000000", CAM)
    b = _locate(scene["sample"], scene["root"], seeds)
    assert b.method == "optical_flow" and "synthetic_fixture" in b.model_version
    assert b.stats["anchors"] == 4 and b.stats["frames_decoded"] == N
    for pid, uv_true in scene["truth"].items():
        vis = b.visibility[pid] == O.VISIBLE
        assert vis.mean() > 0.85, (pid, vis.mean())
        err = np.linalg.norm(b.uv[pid][vis] - uv_true[vis], axis=1)
        assert np.percentile(err, 95) < 1.5 and err.max() < 3.0, (pid, err.max())
        assert np.isnan(b.uv[pid][~vis]).all()


def test_occlusion_is_abstention_not_interpolation(tmp_path):
    (tmp_path / "videos/cam").mkdir(parents=True)
    occ, n = (22, 38), 75
    truth = synth.render_gripper_video(tmp_path / "videos/cam/episode_000000.mp4", n, occlude=occ)
    synth.write_seeds(tmp_path / "seeds/synthetic_000000" / f"{CAM}.jsonl", truth, sample_id="synthetic_000000",
                      camera_id=CAM, occluded=occ)
    r = load.load_bundle(json.dumps(synth.make_bundle([synth.make_entry(0, n)])).encode(), lerobot_root=tmp_path)
    seeds = O.find_seeds(tmp_path / "seeds", "synthetic_000000", CAM)
    b = _locate(r.samples[0], tmp_path, seeds)
    for pid in truth:
        hidden = b.visibility[pid][occ[0]:occ[1]]
        assert (hidden != O.VISIBLE).all(), (pid, hidden)
        assert b.visibility[pid][30] == O.OCCLUDED                          # the seed said so
        assert np.isnan(b.uv[pid][occ[0]:occ[1]]).all()
        # after the occlusion the gripper is found again at the next anchor and tracked through the
        # following segment; the one-sided stretch next to the occluded anchor stays short
        assert b.visibility[pid][45] == O.VISIBLE
        assert (b.visibility[pid][38:42] != O.VISIBLE).all()
        assert (b.visibility[pid][46:60] == O.VISIBLE).mean() > 0.8
        err = np.linalg.norm(b.uv[pid][46:60] - truth[pid][46:60], axis=1)
        assert np.nanmax(err) < 3.0


def test_moving_the_declared_projection_does_not_move_the_observations(scene, tmp_path):
    """Design 12 §7.1: translate the projection by 30 px, the observations must not change."""
    e = copy.deepcopy(scene["entry"])
    for f in e["frames"]:
        for p in f["cameras"][CAM]["projection"]["points"].values():
            p["uv_px"] = [p["uv_px"][0] + 30.0, p["uv_px"][1]]
            p["in_frame"] = 0 <= p["uv_px"][0] < synth.W
            p["status"] = "valid" if p["in_frame"] else "out_of_frame"
    moved = load.load_bundle(json.dumps(synth.make_bundle([e])).encode(), lerobot_root=scene["root"]).samples[0]
    assert abs(moved.cameras[CAM].provided["tcp"].uv[0, 0] - scene["sample"].cameras[CAM].provided["tcp"].uv[0, 0]
               - 30.0) < 1e-9
    seeds = O.find_seeds(scene["seed_root"], "synthetic_000000", CAM)
    a = _locate(scene["sample"], scene["root"], seeds)
    b = _locate(moved, scene["root"], seeds)
    for pid in a.uv:
        np.testing.assert_array_equal(a.uv[pid], b.uv[pid])
        np.testing.assert_array_equal(a.visibility[pid], b.visibility[pid])


def test_observation_rows_follow_the_schema_and_map_to_sample_frames(scene, tmp_path):
    seeds = O.find_seeds(scene["seed_root"], "synthetic_000000", CAM)
    b = _locate(scene["sample"], scene["root"], seeds)
    rows = O.observation_rows(b, scene["sample"], CAM)
    assert len(rows) == N
    v = schemas.validator("eef/observation.schema.json")
    for row in rows:
        assert not list(v.iter_errors(row))
        assert row["projection_visible_to_localizer"] is False and len(row["input_image_sha256"]) == 64
    O.write_rows(tmp_path / "obs" / f"{CAM}.jsonl", rows)
    back = [json.loads(x) for x in (tmp_path / "obs" / f"{CAM}.jsonl").read_text().splitlines()]
    assert back == rows
    uv, vis, conf = O.on_sample_frames(b, scene["sample"].cameras[CAM], "tcp")
    assert uv.shape == (N, 2) and ((vis == O.VISIBLE) == np.isfinite(uv).all(1)).all()


def test_seed_files_are_validated(scene, tmp_path):
    good = scene["seed_root"] / "synthetic_000000" / f"{CAM}.jsonl"
    with pytest.raises(O.SeedError, match="row is for"):
        O.load_seed_file(good, sample_id="synthetic_000000", camera_id="other")
    rows = [json.loads(x) for x in good.read_text().splitlines()]
    rows[1]["projection_visible_to_localizer"] = True
    bad = tmp_path / "bad.jsonl"
    bad.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(O.SeedError):
        O.load_seed_file(bad, sample_id="synthetic_000000", camera_id=CAM)
    assert O.find_seeds(tmp_path, "synthetic_000000", CAM) is None
    assert O.seeded_points(scene["seed_root"], scene["sample"]) == {CAM: set(synth.GRIPPER_POINTS)}


def test_without_seeds_nothing_is_observed(scene):
    b = _locate(scene["sample"], scene["root"], None)
    assert b.stats["anchors"] == 0 and not b.uv
