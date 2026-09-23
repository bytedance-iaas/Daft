"""Design 12 §3.3: trajectory.json from LeRobot columns by an explicit eef-mapping/1.0 file.

The two DEMO mappings (tests/eef/mappings/) are the regressions: exported without the native annotations,
the bundles validate and the platform's recomputed projection matches the projection the reference bundles
provide (dataset1 exactly, dataset2 within the source's float32 quantisation). On tools/parity's mini
dataset (always present) the reading of LeRobot v2.1, the video clips and the refusal of vague mappings.
"""
from __future__ import annotations

import copy
import json
import pathlib

import numpy as np
import pytest

from curation.extensions.eef_consistency import load
from curation.extensions.eef_consistency.__main__ import main as cli
from curation.extensions.eef_consistency.adapters import lerobot_mapping as M

from . import demo_data

MAPPINGS = pathlib.Path(__file__).parent / "mappings"
MINI = {"schema_version": "eef-mapping/1.0", "dataset_id": "parity/mini",
        "eef": {"pose_key": "observation.state", "slice": [0, 6], "layout": "xyz_rpy_xyz_extrinsic",
                "frame_id": "flange", "reference_frame": "robot_base", "units": {"position": "m", "angle": "rad"},
                "pose_type": "absolute"},
        "gripper": {"key": "observation.state", "index": 6, "closed_fraction": {"min": -1.0, "max": 1.0}},
        "tool": {"tcp_offset_m": [0, 0, 0.1], "axes": {"from": "tcp", "length_m": 0.05}},
        "cameras": {"observation.images.exterior": {
            "video_key": "observation.images.exterior", "camera_id": "exterior", "mount": "fixed_external",
            "calibration": {"intrinsics_fx_cx_fy_cy": [100.0, 64.0, 100.0, 48.0],
                            "extrinsics": {"mode": "static", "cam2base_xyz_rpy": [1.0, 0.0, 0.5, -1.9, 0.0, 1.57]}}}},
        "timing": {"source_clocks": []}}


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    from parity.fixtures import make_mini_lerobot

    return make_mini_lerobot(str(tmp_path_factory.mktemp("mini") / "mini"))


def test_the_mapping_must_say_what_the_columns_are():
    for broken, words in (({"eef": {**MINI["eef"], "layout": None}}, "eef.layout is required"),
                          ({"eef": {**MINI["eef"], "layout": "auto"}}, "never guessed"),
                          ({"eef": {**MINI["eef"], "pose_type": "relative_to_start"}}, "absolute"),
                          ({"eef": {**MINI["eef"], "units": {"position": "cm"}}}, "units"),
                          ({"cameras": {}}, "cameras is required"),
                          ({"schema_version": "eef-mapping/0.9"}, "schema_version")):
        with pytest.raises(M.MappingError, match=words):
            M.check_mapping({**copy.deepcopy(MINI), **broken})
    cam = copy.deepcopy(MINI)
    del cam["cameras"]["observation.images.exterior"]["calibration"]["extrinsics"]["cam2base_xyz_rpy"]
    with pytest.raises(M.MappingError, match="extrinsics needs"):
        M.check_mapping(cam)


def test_a_v21_dataset_exports_form_b(mini, tmp_path):
    bundle = M.export(MINI, mini, episodes=[0, 3])
    r = load.load_bundle(json.dumps(bundle).encode(), lerobot_root=mini)
    assert r.ok, [i.as_dict() for i in r.errors[:3]]
    s = r.samples[3]
    assert s.sample_id == "mini_000003" and s.cameras["exterior"].projection_source is None
    view = bundle["samples"][1]["sample"]["views"][0]
    assert view["media"]["uri"] == "videos/chunk-000/observation.images.exterior/episode_000003.mp4"
    assert view["media"]["image_size_wh"] == [128, 96] and view["media"]["clip_start_s"] == 0.0
    assert set(bundle["samples"][0]["sample"]["point_definitions"]) == {"eef_origin", "tcp", "tcp_x", "tcp_y", "tcp_z"}
    assert np.all((s.gripper >= 0) & (s.gripper <= 1))
    assert load.recompute_projection(s, "exterior", "tcp") is not None
    missing = copy.deepcopy(MINI)
    missing["eef"]["pose_key"] = "observation.cartesian"
    with pytest.raises(M.MappingError, match="observation.cartesian"):
        M.export(missing, mini, episodes=[0])
    path = tmp_path / "mini.yaml"
    import yaml

    path.write_text(yaml.safe_dump(MINI))
    out = tmp_path / "trajectory.json"
    assert cli(["export", "--mapping", str(path), "--lerobot-root", mini, "--episodes", "1", "--out", str(out)]) == 0
    assert [e["episode_index"] for e in json.loads(out.read_text())["samples"]] == [1]
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({**MINI, "eef": {**MINI["eef"], "layout": "auto"}}))
    assert cli(["export", "--mapping", str(bad), "--lerobot-root", mini, "--out", str(out)]) == 2


@pytest.mark.parametrize("name, tolerance_px", [("dataset1", 1e-6), ("dataset2", 0.05)])
def test_the_demo_mappings_reproduce_the_reference_projection(name, tolerance_px):
    root = demo_data.require(name)
    lr = demo_data.lerobot_root(name)
    bundle = M.export(M.load_mapping(MAPPINGS / f"{name}.yaml"), lr)
    mine = load.load_bundle(json.dumps(bundle).encode(), lerobot_root=lr)
    assert mine.ok, [i.as_dict() for i in mine.errors[:3]]
    ref = load.load_bundle(root / "trajectory.json", lerobot_root=lr)
    assert sorted(mine.samples) == sorted(ref.samples)
    worst, n = 0.0, 0
    for ep, s in ref.samples.items():
        assert mine.samples[ep].n_frames == s.n_frames
        for cid, cam in s.cameras.items():
            assert mine.samples[ep].cameras[cid].media["clip_start_s"] == pytest.approx(cam.media["clip_start_s"])
            for pid, track in cam.provided.items():
                got = load.recompute_projection(mine.samples[ep], cid, pid)
                both = np.isfinite(track.uv).all(1) & np.isfinite(got.uv).all(1)
                worst = max(worst, float(np.abs(track.uv[both] - got.uv[both]).max(initial=0.0)))
                n += int(both.sum())
    assert n > 10000 and worst <= tolerance_px, (n, worst)
