"""A robot arm's trajectory generated from the dataset declaration (F5.23, design doc 25 §3-§4.1, D83).

tools/parity's mini dataset plus a pose column that puts the TCP where the red block is (a pinhole camera at the
base's origin, 1 m away): with a declaration nobody uploads a trajectory.json - the preflight says it is generated,
``check`` generates each episode's bundle into the run directory and reads the same as the uploaded file does
(episode 1 a stretch late, episode 2 nine pixels off). Without a declaration the preflight drafts one to say what is
missing; the person's seeds follow the generated samples by episode.
"""
from __future__ import annotations

import json
import shutil

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from .pipeline import results, run
from .test_eef_check import CAM, EEF, VLM, _files, _truth, fake_vlm

F_PX = 100.0
SIX = ["x", "y", "z", "roll", "pitch", "yaw"]


def _pose(ep: int) -> np.ndarray:
    """The TCP 1 m in front of the camera, where the fixture's truth (with the upload fixture's faults) puts the block."""
    from parity import fixtures as F

    uv = _truth(ep)
    n = len(uv)
    shift, offset = (4 if ep == 1 else 0), (9.0 if ep == 2 else 0.0)
    out = np.zeros((n, 6))
    for i in range(n):
        u, v = uv[min(max(i - shift, 0), n - 1)]
        out[i, :3] = [(u + offset - F.WIDTH / 2) / F_PX, (v - F.HEIGHT / 2) / F_PX, 1.0]
    return out


@pytest.fixture(scope="module")
def declared(tmp_path_factory, mini_dataset) -> str:
    root = tmp_path_factory.mktemp("declared") / "arm"
    shutil.copytree(mini_dataset, root)
    info = json.loads((root / "meta" / "info.json").read_text())
    info["features"]["observation.state.cartesian_position"] = {"dtype": "float32", "shape": [6], "names": {"axes": SIX}}
    (root / "meta" / "info.json").write_text(json.dumps(info))
    for path in sorted((root / "data").glob("chunk-*/episode_*.parquet")):
        ep = int(path.stem.split("_")[1])
        table = pq.read_table(path)
        n = table.num_rows
        pose = _pose(ep) if ep < 7 else np.zeros((n, 6))
        pose = np.vstack([pose, np.repeat(pose[-1:], max(0, n - len(pose)), 0)])[:n]
        pq.write_table(table.append_column("observation.state.cartesian_position",
                                           pa.array(pose.astype(np.float32).tolist(), pa.list_(pa.float32()))), path)
    return str(root)


def _declaration(tmp_path) -> str:
    from parity import fixtures as F

    doc = {"schema_version": "dataset-declaration/1.0", "name": "mini arm",
           "semantics": {"pose": {"key": "observation.state.cartesian_position", "layout": "xyz_rpy_xyz_extrinsic",
                                  "units": {"position": "m", "angle": "rad"}, "frame_id": "tcp",
                                  "reference_frame": "camera_world", "pose_type": "absolute", "assurance": "declared"},
                         "joints": None, "gripper": None},
           "calibration": {"cameras": {f"observation.images.{CAM}": {
               "camera_id": CAM, "mount": "fixed_external",
               "intrinsics": {"fx_cx_fy_cy": [F_PX, F.WIDTH / 2, F_PX, F.HEIGHT / 2], "model": "pinhole"},
               "extrinsics": {"mode": "static", "xyz_rpy": [0, 0, 0, 0, 0, 0]}}},
               "tool": {"tcp_offset_m": [0, 0, 0], "finger_axis": None, "max_opening_m": None,
                        "axes": {"from": "tcp", "length_m": 0.05}, "assurance": "declared"}, "handheld": None},
           "timing": None}
    path = tmp_path / "declaration.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _seeds(tmp_path) -> str:
    """The person's seeds of the TCP, named as their own trajectory named the samples (mini_<episode>)."""
    d = tmp_path / "seeds"
    for ep in range(3):
        uv = _truth(ep)
        rows = [{"schema_version": "eef-video/1.0.0", "sample_id": f"mini_{ep:06d}", "frame_index": i, "camera_id": CAM,
                 "video_frame_index": i, "pixel_space": "media", "method": "synthetic_fixture", "model_version": "test",
                 "input_image_sha256": "0" * 64, "projection_visible_to_localizer": False,
                 "points": {"tcp": {"uv_px": [float(uv[i, 0]), float(uv[i, 1])], "visibility": "visible",
                                    "confidence": 1.0, "uncertainty_px": None}}}
                for i in range(len(uv))]
        (d / f"mini_{ep:06d}").mkdir(parents=True)
        (d / f"mini_{ep:06d}" / f"{CAM}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(d)


def _entry(doc: dict) -> dict:
    (entry,) = [m for m in doc["modules"] if m["id"] == EEF]
    return entry


def test_without_a_declaration_the_preflight_says_what_is_missing(declared):
    entry = _entry(run("preflight", "--input", declared, "--modules", EEF, "--vlm-backend", "ark").doc)
    assert (entry["availability"], entry["reason_code"]) == ("needs_input", "declaration_incomplete")
    src = entry["trajectory_source"]
    assert src["kind"] == "missing_declaration" and src["declaration"]["drafted"] is True
    assert {m["code"] for m in src["missing"]} >= {"intrinsics_missing", "no_drawable_camera"}
    assert entry["input_hint"] == {"field": "trajectory_json"}            # or upload one instead


def test_a_declared_dataset_generates_its_trajectory_and_reads_like_the_upload(declared, mini_dataset, tmp_path):
    decl = _declaration(tmp_path)
    entry = _entry(run("preflight", "--input", declared, "--modules", EEF, "--vlm-backend", "ark",
                       "--declaration", decl).doc)
    assert entry["availability"] == "available" and entry["trajectory_source"]["kind"] == "generate"
    assert entry["subitems"]["position_2d"]["availability"] == "available"
    seeds = _seeds(tmp_path)
    with fake_vlm(tmp_path / "tape"):
        gen = run("check", "--modules", EEF, "--input", declared, "--run-dir", str(tmp_path / "gen"), "--episodes", "0-2",
                  "--declaration", decl, "--param", f"{EEF}.observation_seeds={seeds}", *VLM)
        up = run("check", "--modules", EEF, "--input", mini_dataset, "--run-dir", str(tmp_path / "up"),
                 "--episodes", "0-2", "--param", f"{EEF}.trajectory_json={_files(tmp_path / 'upload', seed_every=1)}",
                 *VLM)
    assert gen.rc == 0 and up.rc == 0, (gen.doc, up.doc)
    got, want = results(str(tmp_path / "gen"), EEF), results(str(tmp_path / "up"), EEF)

    def statuses(recs):
        return {ep: {k: v["status"] for k, v in r["details"]["cameras"][CAM]["subitems"].items()
                     if k in ("position_2d", "temporal_alignment")} for ep, r in recs.items()}

    # episode 2's rows have a 0.67 s gap in their timestamps the uploaded file never had: its timing is the dataset's
    assert {ep: s["position_2d"] for ep, s in statuses(got).items()} == {ep: s["position_2d"] for ep, s in statuses(want).items()}
    assert statuses(got)[0] == statuses(want)[0] and statuses(got)[1] == statuses(want)[1]
    assert statuses(got)[2]["position_2d"] == "suspect" and statuses(got)[1]["temporal_alignment"] == "suspect"
    for ep, rec in got.items():
        d = rec["details"]
        assert d["trajectory_source"]["kind"] == "generated" and d["trajectory_source"]["status"] == "generated"
        assert (tmp_path / "gen" / "checks" / EEF / "trajectory" / f"episode_{ep:06d}.json").is_file()
        assert d["sample_id"] == f"arm_{ep:06d}"                     # the seeds followed by episode
        # the trajectory is the record's own: only the dataset's two records would be compared (design doc 25 §6.1)
        assert (d["record"]["status"], d["record"]["source"], d["record"]["reasons"]) == (
            "unsupported", "internal", ["record_single_source"])


def test_an_incomplete_declaration_fails_the_module_with_what_is_missing(declared, tmp_path):
    doc = json.loads(open(_declaration(tmp_path)).read())
    doc["calibration"]["cameras"][f"observation.images.{CAM}"]["intrinsics"] = None
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(doc))
    with fake_vlm(tmp_path / "tape"):
        res = run("check", "--modules", EEF, "--input", declared, "--run-dir", str(tmp_path / "rd"), "--episodes", "0",
                  "--declaration", str(path), *VLM)
    assert res.rc == 4, res.doc
    assert res.doc["error"]["details"]["reason"] == "declaration_incomplete"
    assert {m["code"] for m in res.doc["error"]["details"]["missing"]} >= {"intrinsics_missing"}


def test_a_moving_camera_declared_fixed_takes_part_and_is_noted(declared, tmp_path):
    """F5.24a acceptance ⑤ (design doc 25 §3.3): a moving camera is not drawn, nor offered a gripper reference; a
    person declaring it fixed (assumption ``mount_declared_fixed``) makes it a third-person camera - the reference
    applies, the trajectory is generated with it - and the records and the report say it was declared fixed."""
    from curation.extensions.eef_consistency import report as eef_report

    doc = json.loads(open(_declaration(tmp_path)).read())
    cam = doc["calibration"]["cameras"][f"observation.images.{CAM}"]
    cam["mount"] = "moving"
    moving = tmp_path / "moving.json"
    moving.write_text(json.dumps(doc))
    entry = _entry(run("preflight", "--input", declared, "--modules", EEF, "--declaration", str(moving)).doc)
    assert entry["trajectory_source"]["kind"] == "missing_declaration"
    assert {(c["camera_id"], c["reason"]) for c in entry["cameras"]} >= {(CAM, "moving_camera_unsupported")}
    assert "observation_seeds" not in entry["applicable_params"]
    cam.update(mount="fixed_external", assurance="model_assumed", assumptions=[{"code": "mount_declared_fixed"}])
    fixed = tmp_path / "fixed.json"
    fixed.write_text(json.dumps(doc))
    entry = _entry(run("preflight", "--input", declared, "--modules", EEF, "--declaration", str(fixed)).doc)
    assert entry["availability"] == "available" and entry["trajectory_source"]["kind"] == "generate"
    assert [(c["camera_id"], c["mount"], c["drawable"]) for c in entry["cameras"] if c["camera_id"] == CAM] == [
        (CAM, "fixed_external", True)]
    assert {"observation_seeds", "gripper_template"} <= set(entry["applicable_params"])
    res = run("check", "--modules", EEF, "--input", declared, "--run-dir", str(tmp_path / "rd"), "--episodes", "0-1",
              "--declaration", str(fixed), "--param", f"{EEF}.observation_seeds={_seeds(tmp_path)}", "--no-vlm")
    assert res.rc == 0, res.doc
    recs = results(str(tmp_path / "rd"), EEF)
    assert all(r["details"]["trajectory_source"]["declared_fixed"] == [CAM] for r in recs.values())
    assert all(CAM in r["details"]["cameras"] for r in recs.values())               # measured as a fixed camera
    summary = eef_report.source_summary(recs)
    assert summary == {"trajectory_sources": [{"name": "generated", "count": 2}], "declared_fixed_cameras": [CAM]}
