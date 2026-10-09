"""The EEF overlay over a task's cameras (design doc 22 §3, C4 4.3.0): every sample frame is placed where
the player shows its paired video frame (``times_s`` from the player's own frame times: LeRobot frame k at
k / fps, an mcap topic's message k at its scanned time, never before the first keyframe) - not at the
bundle's own clock, nor on the checks' clock; the observed group of a measured episode, the cache keyed
by the observation files, and a cleaned run directory brought back first. A handheld gripper's pose gaps
are bridged as far as the viewer asks (§5.2, C4 4.4.0 ``max_gap_ms``)."""
from __future__ import annotations

import json
import shutil

import pytest

from ..eef import synth
from .conftest import assert_error, assert_schema
from .fixtures import LENGTHS
from .mcap_fixtures import make_abc, make_default, make_umi

API = "/curation/api/v1"
MODULE = "eef_video_consistency"
HANDLE = "upload:up_eef_bundle"
FRONT_V2 = "videos/chunk-000/observation.images.front/episode_000002.mp4"


def _lerobot_dataset(c, data_root):
    from daemon.repo import protocol as P

    from .test_api import _preflight

    ds, _ = c.app.state.runtime.repo.register_dataset(P.Dataset(
        id="", name="lerobot_v2", source="local", uri=str(data_root / "lerobot_v2"), preflight=_preflight("v2"),
        meta_fingerprint="sha256:" + "2" * 64, source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"},
        preflighted_at=1))
    return ds.id


def _mcap_dataset(c, root, name):
    from .test_mcap import _confirm, _register

    rt = c.app.state.runtime
    key = f"mc_{name}"
    ds = _register(rt, root, name)
    c.ids = {**getattr(c, "ids", {}), key: ds.id}
    _confirm(c, key)
    return ds.id


def _entry(ep: int, n: int, media: dict) -> dict:
    e = synth.make_entry(ep=ep, n=n)
    e["sample"]["views"][0]["media"].update(media)
    return e


def _task(c, dataset_id: str, uri, entries: list[dict], *, params: dict | None = None, bundle: dict | None = None):
    """A task with the EEF module selected, its bundle (``entries``, or a whole ``bundle``) copied into the run
    directory as at start."""
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P
    from daemon.results.store import store_of

    rt = c.app.state.runtime
    p = {"trajectory_json": HANDLE, **(params or {})}
    rows = [P.TaskModule(task_id="", module_id=m, selected=m == MODULE, availability="available",
                         params=p if m == MODULE else None) for m in registry.ids()]
    task = rt.repo.create_task(P.TaskCreate(
        name="eef", input_source="local", input_uri=str(uri), output_uri="tos://b/out", delivery_key="tos://b/out",
        episode_selector={"mode": "all"}, params={}, modules=rows, dataset_id=dataset_id))
    run_dir = store_of(rt).task_dir(task.id)
    (run_dir / "inputs").mkdir(parents=True, exist_ok=True)
    synth.write_bundle(run_dir / "inputs" / "trajectory.json", bundle or synth.make_bundle(entries))
    (run_dir / "inputs" / "uploads.json").write_text(json.dumps({HANDLE: {"path": "inputs/trajectory.json"}}))
    return task, run_dir


def _overlay(c, task_id: str, ep: int) -> dict:
    r = c.get(f"{API}/tasks/{task_id}/episodes/{ep}/eef-overlay")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("EefOverlay", body)
    return body


def _observations(run_dir, ep: int, camera: str, n: int, dx: float, track) -> None:
    d = run_dir / "checks" / MODULE / "observations" / f"{ep:06d}"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"schema_version": "eef-video/1.0.0", "sample_id": f"synthetic_{ep:06d}", "frame_index": i,
             "camera_id": camera, "video_frame_index": i, "pixel_space": "media",
             "points": {"tcp": {"uv_px": [float(track[i][0]) + dx, float(track[i][1])], "visibility": "visible"}}}
            for i in range(n)]
    (d / f"{camera}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def lr(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root)
    c.ds = _lerobot_dataset(c, data_root)
    return c


def test_a_lerobot_camera_draws_each_sample_on_its_own_frame(lr, data_root):
    n = LENGTHS[2]
    task, _ = _task(lr, lr.ds, data_root / "lerobot_v2", [_entry(2, n, {"uri": FRONT_V2})])
    cam = _overlay(lr, task.id, 2)["cameras"][0]
    assert cam["viz_camera"] == "front"
    # frame k of the 10 fps clip, not the bundle's own 15 Hz timestamps
    assert cam["times_s"] == [round(k / 10, 6) for k in range(n)]
    layers = {x["id"]: x for x in cam["layers"]}
    # the synthetic gripper's approach axis is short on screen: A is the fingers' line, and there is no B
    assert set(layers) == {"point", "axis", "axis_x", "axis_y", "axis_z", "trail_past", "trail_future"}
    assert layers["axis"]["title"] == "朝向 A（finger_line）"
    assert {k for k, x in layers.items() if x["in_model"]} == {"point", "axis", "trail_past"}
    assert {k for k, x in layers.items() if not x["default_on"]} == {"axis", "trail_future"}
    assert (layers["axis"]["color"], layers["axis"]["model_color"]) == ("#b26bff", "#ff0000")
    assert layers["axis_x"]["kind"] == "arrow" and layers["axis_x"]["group"] == "axes"
    assert cam["hands"] == [{"id": "eef", "title": "panda_link8", "color": "#ff0000", "opening_m": None}]
    assert _overlay(lr, task.id, 2)["interpolation"] is None            # an arm's bundle has nothing to bridge
    # a task without the module, an episode the bundle does not have
    assert_error(lr.get(f"{API}/tasks/{task.id}/episodes/1/eef-overlay"), "not_found")


def test_a_camera_the_player_does_not_have_gets_no_times(lr, data_root):
    task, _ = _task(lr, lr.ds, data_root / "lerobot_v2", [_entry(2, 10, {"uri": "videos/elsewhere.mp4"})])
    cam = _overlay(lr, task.id, 2)["cameras"][0]
    assert cam["viz_camera"] is None and cam["times_s"] == [None] * 10


def test_a_measured_episode_draws_the_observed_point_and_the_residual(lr, data_root):
    n = 20
    entry = _entry(2, n, {"uri": FRONT_V2})
    task, run_dir = _task(lr, lr.ds, data_root / "lerobot_v2", [entry], params={"observation_seeds": "upload:up_seeds"})
    track = [f["cameras"]["cam0"]["projection"]["points"]["tcp"]["uv_px"] for f in entry["frames"]]
    _observations(run_dir, 2, "cam0", n, 5.0, track)
    cam = _overlay(lr, task.id, 2)["cameras"][0]
    layers = {x["id"]: x for x in cam["layers"]}
    assert {"observed_point", "observed_trail", "residual"} <= set(layers)
    assert layers["observed_point"]["kind"] == "cross" and layers["residual"]["default_on"] is False
    # the model saw the review's windows: P, its observed cross and A
    assert {k for k, x in layers.items() if x["in_model"]} == {"point", "axis", "observed_point"}
    x1, y1, x2, y2 = layers["residual"]["frames"][7]
    assert (round(x2 - x1, 1), round(y2 - y1, 1)) == (5.0, 0.0)
    # new rows: the cached layers are not served again
    _observations(run_dir, 2, "cam0", n, 9.0, track)
    x1, _, x2, _ = {x["id"]: x for x in _overlay(lr, task.id, 2)["cameras"][0]["layers"]}["residual"]["frames"][7]
    assert round(x2 - x1, 1) == 9.0


def test_a_cleaned_run_directory_is_brought_back_first(lr, data_root, tmp_path):
    from daemon.results.store import store_of

    rt = lr.app.state.runtime
    n = 12
    entry = _entry(2, n, {"uri": FRONT_V2})
    task, run_dir = _task(lr, lr.ds, data_root / "lerobot_v2", [entry], params={"gripper_template": "upload:up_tpl"})
    track = [f["cameras"]["cam0"]["projection"]["points"]["tcp"]["uv_px"] for f in entry["frames"]]
    _observations(run_dir, 2, "cam0", n, 3.0, track)
    (run_dir / "revisions" / "r0001").mkdir(parents=True)
    (run_dir / "revisions" / "r0001" / "commit.json").write_text("{}")
    assert rt.repo.switch_result_rev(task.id, 0, 1)
    saved = tmp_path / "delivered"
    shutil.copytree(run_dir, saved)
    shutil.rmtree(run_dir)
    calls = []

    def backfill(t, missing):
        calls.append(missing)
        shutil.copytree(saved, run_dir)
        return True

    store_of(rt).backfill = backfill
    layers = {x["id"] for x in _overlay(lr, task.id, 2)["cameras"][0]["layers"]}
    assert calls == ["revisions/r0001/commit.json"] and "observed_point" in layers


@pytest.fixture
def mc(client_for, tmp_path):
    data = tmp_path / "inputs"
    make_umi(str(data / "umi"), episodes=1)
    make_abc(str(data / "abc"), wrist_skip=3)
    make_default(str(data / "lagged"), action_lag=3)
    c = client_for(base_path="/curation", local_data_root=data)
    c.data = data
    return c


def _mcap_media(topic: str, n: int) -> dict:
    return {"uri": "episode_0.mcap", "topic": topic, "frame_count": n, "fps": 10.0, "clip_start_s": 0.0,
            "clip_end_s": None}


def test_an_mcap_camera_draws_each_sample_at_its_message_time(mc):
    ds = _mcap_dataset(mc, mc.data / "umi", "umi")
    task, _ = _task(mc, ds, mc.data / "umi", [_entry(0, 20, _mcap_media("/robot0/sensor/camera0/compressed", 20))])
    cam = _overlay(mc, task.id, 0)["cameras"][0]
    assert cam["viz_camera"] == "robot0_sensor_camera0_compressed"
    # the camera's messages come half a step after the poses the episode starts with
    assert cam["times_s"] == pytest.approx([0.05 + 0.1 * k for k in range(20)])
    assert mc.get(f"{API}/tasks/{task.id}/episodes/0/viz").json()["check_clock"]["offset_s"] == 0.0


def test_frames_before_the_first_keyframe_are_never_drawn(mc):
    ds = _mcap_dataset(mc, mc.data / "abc", "abc")
    task, _ = _task(mc, ds, mc.data / "abc", [_entry(0, 20, _mcap_media("/camera/wrist", 20))])
    cam = _overlay(mc, task.id, 0)["cameras"][0]
    assert cam["viz_camera"] is not None
    # the wrist stream starts three frames into a 10-frame GOP: its first 7 messages cannot be shown
    assert cam["times_s"][:7] == [None] * 7
    assert cam["times_s"][7:] == pytest.approx([0.1 * k for k in range(7, 20)])


def test_the_checks_clock_starting_later_moves_nothing(mc):
    ds = _mcap_dataset(mc, mc.data / "lagged", "lagged")
    task, _ = _task(mc, ds, mc.data / "lagged", [_entry(0, 15, _mcap_media("/observation.images.front", 15))])
    assert mc.get(f"{API}/tasks/{task.id}/episodes/0/viz").json()["check_clock"]["offset_s"] == pytest.approx(0.3)
    cam = _overlay(mc, task.id, 0)["cameras"][0]
    assert cam["times_s"] == pytest.approx([0.1 * k for k in range(15)])


def test_an_old_scan_is_read_again(mc):
    """A scan cached before the frame times were kept (no ``format``) is not used: the overlay would have
    no times to place the samples at."""
    ds = _mcap_dataset(mc, mc.data / "umi", "umi")
    task, _ = _task(mc, ds, mc.data / "umi", [_entry(0, 20, _mcap_media("/robot0/sensor/camera0/compressed", 20))])
    from daemon.viz.service import viz_of

    assert mc.get(f"{API}/tasks/{task.id}/episodes/0/viz").status_code == 200
    docs = list(viz_of(mc.app.state.runtime).disk.root.rglob("episode.json"))
    assert docs
    for d in docs:
        doc = json.loads(d.read_text())
        doc.pop("format", None)
        for cd in doc["cameras"].values():
            cd.pop("times", None)
        d.write_text(json.dumps(doc))
    cam = _overlay(mc, task.id, 0)["cameras"][0]
    assert cam["times_s"] == pytest.approx([0.05 + 0.1 * k for k in range(20)])


def test_a_handheld_gripper_bridges_its_pose_gaps_as_far_as_asked(mc, tmp_path):
    """The checks' default (3 sample intervals) unless the viewer asks for another gap (the side panel's
    setting, ``max_gap_ms``); only the drawing changes."""
    from curation.extensions.eef_consistency.adapters import umi_mcap

    from ..eef import das_mcap

    cfg = das_mcap.calibration(pairing_tolerance_s=0.06)      # make_umi's frames come 50 ms after the poses
    del cfg["intrinsics_fallback"]
    (tmp_path / "cal.json").write_text(json.dumps(cfg))
    out = tmp_path / "umi" / "trajectory.json"
    umi_mcap.export(mc.data / "umi", tmp_path / "cal.json", out, episodes=[0], ego_check=False)
    bundle = json.loads(out.read_text())
    for f in bundle["samples"][0]["frames"][5:7]:              # the VIO lost two poses
        f["hands"]["robot0"] = None
        f["cameras"]["robot0_camera0"].update(T_reference_camera=None, calibration_id=None)
    ds = _mcap_dataset(mc, mc.data / "umi", "umi")
    task, _ = _task(mc, ds, mc.data / "umi", [], bundle=bundle)

    def point(body: dict) -> list:
        (cam,) = body["cameras"]
        assert cam["viz_camera"] == "robot0_sensor_camera0_compressed"
        return next(x for x in cam["layers"] if x["id"] == "point")["frames"]

    body = _overlay(mc, task.id, 0)
    assert body["interpolation"] == {"max_gap_s": pytest.approx(0.3), "default_s": pytest.approx(0.3),
                                     "step_s": pytest.approx(0.1), "range_steps": [2, 5], "frames": {"robot0": 2}}
    assert all(p is not None for p in point(body))
    r = mc.get(f"{API}/tasks/{task.id}/episodes/0/eef-overlay", params={"max_gap_ms": 150})
    assert r.status_code == 200, r.text
    narrow = r.json()
    assert_schema("EefOverlay", narrow)
    assert narrow["interpolation"]["max_gap_s"] == pytest.approx(0.15) and narrow["interpolation"]["frames"] == {"robot0": 0}
    marks = point(narrow)
    assert marks[5] is None and marks[6] is None and marks[4] is not None and marks[7] is not None
    assert all(p is not None for p in point(_overlay(mc, task.id, 0)))  # the default again, from the cache
    for bad in (0, -5, 2001, "soon"):
        assert_error(mc.get(f"{API}/tasks/{task.id}/episodes/0/eef-overlay", params={"max_gap_ms": bad}),
                     "validation_failed")
