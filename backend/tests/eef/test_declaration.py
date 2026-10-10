"""The dataset declaration (design doc 25 §3, §4.1, D83): drafted from what the dataset says, checked when it is
confirmed, and the robot arm's trajectory generated from it - the same bundle the eef-mapping export makes."""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from curation import declaration as DCL
from curation.declaration import checks as K
from curation.declaration import draft as D
from curation.extensions.eef_consistency import declared as DE
from curation.extensions.eef_consistency import derive, load
from curation.extensions.eef_consistency.adapters import lerobot_mapping as LM

FPS, N, W, H = 10, 30, 320, 240
FRONT, WRIST = "observation.images.exterior_front", "observation.images.wrist_left"


def _info(robot_type="Franka") -> dict:
    six = {"axes": ["x", "y", "z", "roll", "pitch", "yaw"]}
    return {"codebase_version": "v2.1", "robot_type": robot_type, "fps": FPS, "total_episodes": 2, "chunks_size": 1000,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": {
                "observation.state.cartesian_position": {"dtype": "float32", "shape": [6], "names": six},
                "action.cartesian_position": {"dtype": "float32", "shape": [6], "names": six},
                "observation.state.joint_position": {"dtype": "float32", "shape": [7],
                                                     "names": {"axes": [f"joint_{i}" for i in range(7)]}},
                "observation.state.gripper_position": {"dtype": "float32", "shape": [1], "names": {"axes": ["gripper"]}},
                "camera_extrinsics.exterior_front": {"dtype": "float32", "shape": [6], "names": six},
                "timestamp_robot_ms": {"dtype": "int64", "shape": [1], "names": None},
                FRONT: {"dtype": "video", "shape": [H, W, 3], "names": ["height", "width", "channel"]},
                WRIST: {"dtype": "video", "shape": [H, W, 3], "names": ["height", "width", "channel"]},
                "timestamp": {"dtype": "float32", "shape": [1], "names": None},
                "frame_index": {"dtype": "int64", "shape": [1], "names": None},
                "episode_index": {"dtype": "int64", "shape": [1], "names": None},
                "index": {"dtype": "int64", "shape": [1], "names": None},
                "task_index": {"dtype": "int64", "shape": [1], "names": None}}}


#: the front camera: 1.5 m in front of the base, looking back at it (z towards the base)
CAM_XYZ_RPY = [1.5, 0.0, 0.5, -np.pi / 2, 0.0, np.pi / 2]


def _dataset(root: pathlib.Path) -> pathlib.Path:
    from scipy.spatial.transform import Rotation

    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps(_info()))
    (root / "meta" / "episodes.jsonl").write_text("".join(json.dumps({"episode_index": e, "length": N, "tasks": ["pick"]}) + "\n"
                                                         for e in range(2)))
    (root / "meta" / "tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": "pick"}) + "\n")
    for ep in range(2):
        t = np.arange(N) / FPS
        pose = np.stack([0.4 + 0.1 * np.sin(t + ep), 0.05 * np.cos(t), 0.3 + 0.0 * t,
                         np.full(N, np.pi), np.zeros(N), 0.2 * np.sin(t)], 1)
        R = Rotation.from_euler("xyz", pose[:, 3:])
        cols = {"observation.state.cartesian_position": pose.astype(np.float32).tolist(),
                "action.cartesian_position": pose.astype(np.float32).tolist(),
                "observation.state.joint_position": np.zeros((N, 7), np.float32).tolist(),
                "observation.state.gripper_position": (np.linspace(0, 1, N, dtype=np.float32)[:, None]).tolist(),
                "camera_extrinsics.exterior_front": [CAM_XYZ_RPY] * N,
                "timestamp_robot_ms": (1000 * t).astype(np.int64), "timestamp": t.astype(np.float32),
                "frame_index": np.arange(N), "episode_index": np.full(N, ep), "index": np.arange(N) + ep * N,
                "task_index": np.zeros(N, np.int64)}
        assert np.isfinite(R.as_quat()).all()
        out = root / "data" / "chunk-000" / f"episode_{ep:06d}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(cols), out)
    return root


def _complete(decl: dict) -> dict:
    """The draft with what nobody could read off the dataset: the intrinsics, and the wrist camera's mount."""
    d = json.loads(json.dumps(decl))
    cams = d["calibration"]["cameras"]
    cams[FRONT]["intrinsics"] = {"fx_cx_fy_cy": [300.0, W / 2, 300.0, H / 2], "model": "pinhole", "source": "declared",
                                 "assurance": "declared"}
    d["calibration"]["tool"].update(tcp_offset_m=[0, 0, 0.16], max_opening_m=0.085, assurance="declared")
    return d


def test_a_robot_arm_dataset_drafts_what_its_columns_say():
    out = D.draft_lerobot(_info(), listing=["meta/info.json"])
    decl, todo = out["declaration"], out["unresolved"]
    assert DCL.schema_errors(decl) == []
    pose = decl["semantics"]["pose"]
    assert (pose["key"], pose["layout"], pose["frame_id"], pose["reference_frame"]) == (
        "observation.state.cartesian_position", "xyz_rpy_xyz_extrinsic", "panda_link8", "robot_base")
    assert {a["code"] for a in pose["assumptions"]} >= {"observation_not_action", "pose_frame_by_robot", "euler_extrinsic_xyz"}
    assert decl["semantics"]["joints"]["robot"] == "franka_panda"
    assert decl["semantics"]["gripper"] == {**decl["semantics"]["gripper"], "key": "observation.state.gripper_position",
                                            "closed_fraction": "identity"}
    cams = decl["calibration"]["cameras"]
    assert cams[FRONT]["mount"] == "fixed_external" and cams[FRONT]["extrinsics"] == {
        "mode": "column", "key": "camera_extrinsics.exterior_front", "layout": "xyz_rpy", "assurance": "declared"}
    assert cams[WRIST]["mount"] == "wrist" and cams[WRIST]["owner"] == "arm"
    assert cams[FRONT]["intrinsics"] is None                       # nobody can read them off a LeRobot dataset
    tool = decl["calibration"]["tool"]
    assert (tool["model"], tool["tcp_offset_m"], tool["max_opening_m"], tool["assurance"]) == (
        "franka_hand", [0.0, 0.0, 0.1034], 0.08, "model_assumed")
    assert decl["timing"]["source_clocks"][0]["key"] == "timestamp_robot_ms"
    codes = {(t["field"], t["code"]) for t in todo}
    assert (f"calibration.cameras.{FRONT}.intrinsics", "intrinsics_missing") in codes
    assert (f"calibration.cameras.{WRIST}.extrinsics", "camera_tcp_missing") in codes
    assert {a["field"] for a in DCL.assumed(decl)} >= {"semantics.pose", "calibration.tool"}


def test_meta_calibration_files_give_intrinsics_and_mounts_follow_names():
    files = {"meta/camera_calibration.json": json.dumps({"cameras": {"exterior_front": {
        "intrinsics_fx_cx_fy_cy": [300, 160, 300, 120], "width": W, "height": H}}}).encode()}
    out = D.draft_lerobot(_info(robot_type=None), listing=list(files) + ["meta/info.json"], read=files.__getitem__)
    cams = out["declaration"]["calibration"]["cameras"]
    assert cams[FRONT]["intrinsics"] == {"fx_cx_fy_cy": [300.0, 160.0, 300.0, 120.0], "model": "pinhole",
                                         "image_size_wh": [W, H], "source": "meta_file", "assurance": "declared"}
    assert out["declaration"]["calibration"]["tool"] is None       # no robot_type: nothing to pre-fill
    assert out["declaration"]["semantics"]["pose"]["frame_id"] is None
    assert D.mount_of("/head_camera/rgb")[0] == "moving" and D.mount_of("cam7")[0] is None
    assert D.mount_of("/robot1/sensor/camera0/compressed") == ("wrist", [{"code": "mount_from_umi_template",
                                                                          "args": {"hand": "robot1"}}])


def test_a_handheld_gripper_mcap_drafts_two_wrist_cameras_on_assumed_values():
    mapping = {"schema_version": "viz-mapping/1.0", "base": "builtin:umi", "name": "UMI",
               "cameras": [{"topic": f"/robot{i}/sensor/camera0/compressed", "name": f"robot{i}"} for i in (0, 1)],
               "series": []}
    topics = {f"/robot{i}/sensor/camera0/camera_info": "foxglove.CameraCalibration" for i in (0, 1)}
    out = D.draft_mcap(mapping, topics)
    decl = out["declaration"]
    assert DCL.schema_errors(decl) == [] and DCL.mapping_of(decl)["cameras"] == mapping["cameras"]
    cams = decl["calibration"]["cameras"]
    assert [(c["mount"], c["owner"]) for c in cams.values()] == [("wrist", "robot0"), ("wrist", "robot1")]
    hh = decl["calibration"]["handheld"]
    assert hh["builtin"] == "das_gripper_demo"
    items = hh["calibration"]["provenance"]["items"]
    assert {v["assurance"] for v in items.values()} == {"model_assumed"}
    assert out["unresolved"] == []


def test_a_mapping_is_a_declaration_of_its_first_layer():
    m = {"schema_version": "viz-mapping/1.1", "name": "x", "cameras": [{"topic": "/c", "name": "c"}], "series": []}
    decl = DCL.normalize(m)
    assert decl["schema_version"] == DCL.SCHEMA_VERSION and DCL.mapping_of(decl) == m
    full = {**decl, "semantics": {"pose": None}}
    again = DCL.with_mapping(full, {**m, "cameras": []})
    assert again["semantics"] == {"pose": None} and again["cameras"] == []     # the other layers stay
    assert DCL.mapping_of({"schema_version": DCL.SCHEMA_VERSION}) is None


def test_confirming_says_what_is_wrong_and_what_is_doubtful():
    decl = _complete(D.draft_lerobot(_info(), listing=[])["declaration"])
    facts = K.Facts.lerobot(_info())
    assert facts.cameras == {FRONT: [W, H], WRIST: [W, H]}
    assert K.check(decl, facts) == {"errors": [], "suspects": []}
    bad = json.loads(json.dumps(decl))
    bad["semantics"]["pose"]["key"] = "observation.state.joint_position"         # 7 numbers for a 6-wide layout
    bad["calibration"]["cameras"][FRONT]["extrinsics"] = {"mode": "static", "T_reference_camera": np.diag([2, 1, 1, 1]).tolist()}
    bad["calibration"]["cameras"]["observation.images.nope"] = {"mount": "fixed_external"}
    errs = {e["field"] for e in K.check(bad, facts)["errors"]}
    assert errs == {"semantics.pose", f"calibration.cameras.{FRONT}.extrinsics.T_reference_camera",
                    "calibration.cameras.observation.images.nope"}
    odd = json.loads(json.dumps(decl))
    odd["calibration"]["cameras"][FRONT]["intrinsics"] = {"fx_cx_fy_cy": [300, 400, 330, 120], "model": "pinhole"}
    odd["calibration"]["tool"]["max_opening_m"] = 0.3
    got = K.check(odd, facts)
    assert got["errors"] == [] and {s["code"] for s in got["suspects"]} == {"intrinsics_aspect", "intrinsics_center",
                                                                             "opening_range"}


def test_the_declaration_says_whether_the_trajectory_can_be_generated():
    draft = D.draft_lerobot(_info(), listing=[])["declaration"]
    cams = [FRONT, WRIST]
    r = DE.readiness(draft, cams)
    assert not r["ready"] and [(m["field"], m["code"]) for m in r["missing"]] == [
        (f"calibration.cameras.{FRONT}.intrinsics", "intrinsics_missing"),
        (f"calibration.cameras.{WRIST}.intrinsics", "intrinsics_missing"), ("calibration.cameras", "no_drawable_camera")]
    done = _complete(draft)
    r = DE.readiness(done, cams)
    assert r["ready"] and [c["drawable"] for c in r["cameras"]] == [True, False]
    plan = derive.plan_source(listing={}, upload=False, handheld=False, decl=done, drafted=False, cameras=cams, kind="lerobot")
    assert plan["kind"] == "generate"
    assert derive.plan_source(listing={}, upload=False, handheld=False, decl=done, drafted=True, cameras=cams,
                              kind="lerobot")["missing"] == [{"field": "<declaration>", "code": "declaration_unconfirmed"}]
    assert derive.plan_source(listing={}, upload=False, handheld=False, decl=draft, drafted=True, cameras=cams,
                              kind="lerobot")["kind"] == "missing_declaration"
    assert derive.plan_source(listing={"trajectory.json": None}, upload=False, handheld=False, decl=draft, drafted=True,
                              cameras=cams, kind="lerobot")["kind"] == "dataset_file"
    nopose = {"schema_version": DCL.SCHEMA_VERSION, "semantics": {"pose": None}}
    assert derive.plan_source(listing={}, upload=False, handheld=False, decl=nopose, drafted=False, cameras=cams,
                              kind="lerobot")["kind"] == "missing_pose"


def test_the_generated_trajectory_is_the_mapping_export(tmp_path):
    root = _dataset(tmp_path / "ds")
    decl = _complete(D.draft_lerobot(_info(), listing=[])["declaration"])

    class Local:
        remote = False

    Local.root = str(root)
    g = DE.Generated(decl, storage=Local, listing=None, out_dir=str(tmp_path / "out"), dataset_id="arm",
                     cameras=[FRONT, WRIST], sizes={FRONT: [W, H], WRIST: [W, H]})
    sample, source = g.sample(1)
    assert source["kind"] == "generated" and source["status"] == "generated"
    assert (tmp_path / "out" / "trajectory" / "episode_000001.json").is_file()
    assert sorted(sample.cameras) == ["exterior_front"] and sample.sample_id == g.sample_id(1) == "arm_000001"
    track = load.recompute_projection(sample, "exterior_front", "tcp")
    assert np.isfinite(track.uv).all() and (track.uv[:, 0] > 0).all() and (track.uv[:, 0] < W).all()
    by_hand = LM.export(DE.eef_mapping(decl, cameras=[FRONT, WRIST], sizes={FRONT: [W, H]}, dataset_id="arm"),
                        root, episodes=[1])
    ref = load.load_bundle(by_hand, check_media=False, episodes=[1]).samples[1]
    assert np.allclose(load.recompute_projection(ref, "exterior_front", "tcp").uv, track.uv)
    assert g.sample(7)[1]["reason"] == DE.EPISODE_MISSING


def test_a_wrist_camera_rides_the_tool(tmp_path):
    root = _dataset(tmp_path / "ds")
    decl = _complete(D.draft_lerobot(_info(), listing=[])["declaration"])
    wrist = decl["calibration"]["cameras"][WRIST]
    wrist["intrinsics"] = {"fx_cx_fy_cy": [200.0, W / 2, 200.0, H / 2], "model": "pinhole"}
    T = np.eye(4)
    T[:3, 3] = [0.0, 0.05, 0.12]                    # the TCP 12 cm in front of the camera
    wrist["extrinsics"] = {"mode": "camera_tcp", "T_camera_tcp": T.tolist()}

    class Local:
        remote = False

    Local.root = str(root)
    g = DE.Generated(decl, storage=Local, listing=None, out_dir=str(tmp_path / "out"), dataset_id="arm",
                     cameras=[FRONT, WRIST], sizes={FRONT: [W, H], WRIST: [W, H]})
    sample, _ = g.sample(0)
    cam = sample.cameras["wrist_left"]
    assert cam.mount == "wrist"
    uv = load.recompute_projection(sample, "wrist_left", "tcp").uv
    assert np.allclose(uv, [W / 2 + 200 * 0.0 / 0.12, H / 2 + 200 * 0.05 / 0.12], atol=1e-6)   # fixed in its picture


def test_seeds_follow_the_generated_samples_by_episode(tmp_path):
    seeds = tmp_path / "seeds"
    (seeds / "dataset2_000001").mkdir(parents=True)
    row = {"sample_id": "dataset2_000001", "camera_id": "27432424_left", "frame_index": 0, "points": {}}
    (seeds / "dataset2_000001" / "27432424_left.jsonl").write_text(json.dumps(row) + "\n")

    class G:
        def sample_id(self, ep):
            return f"arm_{ep:06d}"

    out = DE.remap_seeds(str(seeds), G(), str(tmp_path / "out"))
    got = json.loads((pathlib.Path(out) / "arm_000001" / "27432424_left.jsonl").read_text())
    assert got["sample_id"] == "arm_000001" and got["camera_id"] == "27432424_left"


@pytest.mark.parametrize("missing", ["intrinsics", "tool"])
def test_what_the_record_comparison_takes_from_the_declaration(missing):
    decl = _complete(D.draft_lerobot(_info(), listing=[])["declaration"])
    block = DE.record_block(decl)
    assert set(block["record"]) == {"pose", "joints"} and block["record"]["pose"]["frame_id"] == "panda_link8"
    if missing == "tool":
        decl["semantics"]["joints"] = None
        assert set(DE.record_block(decl)["record"]) == {"pose"}
    else:
        decl["semantics"]["pose"] = None
        decl["semantics"]["joints"] = None
        assert DE.record_block(decl) is None


def test_dataset2_generated_from_its_declaration_is_the_reference(tmp_path):
    """F5.23 acceptance ①: dataset2 drafts two exterior cameras, the pose, joint and extrinsic columns, no intrinsics
    and an assumed Franka Hand; with the intrinsics and dataset2's own tool (TCP 0.16 m, 0.085 m open) its trajectory is
    generated, every frame's projection within 0.05 px of the reference trajectory.json (DEMO data, outside the repo)."""
    from . import demo_data

    root = demo_data.require("dataset2")
    lr = demo_data.lerobot_root("dataset2")
    info = json.loads((lr / "meta" / "info.json").read_text())
    out = D.draft_lerobot(info, listing=["meta/info.json"])
    decl = out["declaration"]
    cams = decl["calibration"]["cameras"]
    assert sorted((c["mount"], c["extrinsics"]["key"]) for c in cams.values()) == [
        ("fixed_external", "camera_extrinsics.exterior_1_left"), ("fixed_external", "camera_extrinsics.exterior_2_left")]
    assert {t["code"] for t in out["unresolved"]} == {"intrinsics_missing"}
    assert decl["calibration"]["tool"]["assurance"] == "model_assumed"
    sidecar = json.loads((root / "calibration.json").read_text())
    for src, c in cams.items():
        short = src.rsplit(".", 1)[-1]
        c["intrinsics"] = {"fx_cx_fy_cy": sidecar["cameras"][short]["intrinsics_fx_cx_fy_cy"], "model": "pinhole",
                           "source": "declared", "assurance": "declared"}
    decl["calibration"]["tool"].update(model="robotiq_2f85", tcp_offset_m=[0, 0, 0.16], max_opening_m=0.085,
                                       assurance="declared")
    facts = K.Facts.lerobot(info)
    assert K.check(decl, facts)["errors"] == []

    class Local:
        remote = False

    Local.root = str(lr)
    g = DE.Generated(decl, storage=Local, listing=None, out_dir=str(tmp_path), dataset_id="dataset2",
                     cameras=list(facts.cameras), sizes=facts.cameras)
    ref = load.load_bundle(root / "trajectory.json", lerobot_root=str(lr))
    worst, n = 0.0, 0
    for ep, want in ref.samples.items():
        mine, _ = g.sample(ep)
        for cid, cam in want.cameras.items():
            mid = next(c for c, m in mine.cameras.items() if m.media["uri"] == cam.media["uri"])
            for pid, track in cam.provided.items():
                got = load.recompute_projection(mine, mid, pid)
                both = np.isfinite(track.uv).all(1) & np.isfinite(got.uv).all(1)
                worst = max(worst, float(np.abs(track.uv[both] - got.uv[both]).max(initial=0.0)))
                n += int(both.sum())
    assert n > 10000 and worst < 0.05, (n, worst)


def _arm_mcap(root: pathlib.Path, n: int = 12) -> pathlib.Path:
    """One episode of a robot arm's mcap: a front camera (JPEG, its calibration beside it) and the TCP pose, both 10 Hz."""
    pytest.importorskip("mcap_protobuf")
    from foxglove_schemas_protobuf.CameraCalibration_pb2 import CameraCalibration
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from foxglove_schemas_protobuf.PoseInFrame_pb2 import PoseInFrame
    from mcap_protobuf.writer import Writer

    from ..viz.mcap_fixtures import _jpeg

    root.mkdir(parents=True)
    t0, step = 1_000_000_000, 100_000_000
    with open(root / "episode_0.mcap", "wb") as fh:
        w = Writer(fh)
        cal = CameraCalibration()
        cal.width, cal.height = 64, 48
        cal.distortion_model = "plumb_bob"
        cal.K.extend([50, 0, 32, 0, 50, 24, 0, 0, 1])
        cal.D.extend([0, 0, 0, 0, 0])
        w.write_message("/cam/front/camera_info", cal, log_time=t0, publish_time=t0)
        for i in range(n):
            t = t0 + i * step
            img = CompressedImage()
            img.format = "jpeg"
            img.data = _jpeg(i)
            w.write_message("/cam/front/compressed", img, log_time=t + 3_000_000, publish_time=t)
            p = PoseInFrame()
            p.frame_id = "base"
            p.pose.position.x, p.pose.position.y, p.pose.position.z = 0.02 * i, 0.0, 1.0
            p.pose.orientation.w = 1.0
            w.write_message("/arm/eef_pose", p, log_time=t, publish_time=t)
        w.finish()
    return root


def test_a_robot_arms_mcap_generates_from_its_topics(tmp_path):
    from curation.viz import mcap_probe as MP

    root = _arm_mcap(tmp_path / "arm")
    with open(root / "episode_0.mcap", "rb") as fh:
        pr = MP.probe(fh, "episode_0.mcap")
    assert pr.topics["/cam/front/camera_info"].calibration == {
        "K": [[50.0, 0.0, 32.0], [0.0, 50.0, 24.0], [0.0, 0.0, 1.0]], "model": "pinhole", "coefficients": [],
        "image_size_wh": [64, 48]}
    layer1 = {"schema_version": "viz-mapping/1.1", "name": "arm", "base": None,
              "cameras": [{"topic": "/cam/front/compressed", "name": "front"}],
              "series": [{"topic": "/arm/eef_pose", "name": "pose", "role": "action", "fields": ["pose"]}]}
    out = D.draft_mcap(layer1, {t: tp.schema for t, tp in pr.topics.items()}, robot_type="ur5e",
                       calibrations={t: tp.calibration for t, tp in pr.topics.items() if tp.calibration})
    decl = out["declaration"]
    pose = decl["semantics"]["pose"]
    assert (pose["topic"], pose["fields"], pose["layout"]) == ("/arm/eef_pose", ["pose"], "xyz_quat_xyzw")
    cam = decl["calibration"]["cameras"]["/cam/front/compressed"]
    assert cam["mount"] == "fixed_external" and cam["intrinsics"]["source"] == "camera_info"
    assert {u["code"] for u in out["unresolved"]} == {"extrinsics_missing", "pose_frame_unknown", "tool_unknown"}
    pose["frame_id"] = "tcp"
    cam["extrinsics"] = {"mode": "static", "xyz_rpy": [0, 0, 0, 0, 0, 0]}          # the camera at the base, along z
    decl["calibration"]["tool"] = {"tcp_offset_m": [0, 0, 0], "axes": {"from": "tcp", "length_m": 0.05}}
    facts = K.Facts.mcap({t: tp.schema for t, tp in pr.topics.items()}, {"/cam/front/compressed": [64, 48]},
                         cameras=["/cam/front/compressed"])
    assert K.check(decl, facts)["errors"] == []
    assert DE.readiness(decl, ["/cam/front/compressed"], kind="mcap")["ready"]
    g = DE.GeneratedMcap(decl, root=str(root), numbering={0: "episode_0.mcap"}, out_dir=str(tmp_path / "out"),
                         dataset_id="arm", cameras=["/cam/front/compressed"])
    sample, source = g.sample(0)
    assert source["status"] == "generated", source
    cam = sample.cameras["cam_front"]
    assert cam.media["topic"] == "/cam/front/compressed" and cam.media["uri"] == "episode_0.mcap"
    assert list(cam.video_frame_index) == list(range(12))
    uv = load.recompute_projection(sample, "cam_front", "tcp").uv
    assert np.allclose(uv[:, 0], 32 + 50 * 0.02 * np.arange(12)) and np.allclose(uv[:, 1], 24)
