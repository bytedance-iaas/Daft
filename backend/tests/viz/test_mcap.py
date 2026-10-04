"""The visualizer's mcap reader (design doc 18 §6, F13.3): probing, drafting, validation, the derived
check mapping, the episode pass, and the REST surface (mappings, templates, frame packs, remux)."""
from __future__ import annotations

import io
import json
import time

import numpy as np
import pytest

from curation.viz import mcap_mapping as MM
from curation.viz import mcap_probe as MP
from curation.viz.mcap_episode import scan

from .conftest import assert_error, assert_schema
from .mcap_fixtures import make_abc, make_default, make_umi

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")

API = "/curation/api/v1"
JSON = {"content-type": "application/json"}


def _probe(root):
    with open(f"{root}/episode_0.mcap", "rb") as fh:
        return MP.probe(fh, "episode_0.mcap")


@pytest.fixture(scope="module")
def roots(tmp_path_factory):
    base = tmp_path_factory.mktemp("mcap")
    return {"umi": make_umi(str(base / "umi")), "abc": make_abc(str(base / "abc")),
            "default": make_default(str(base / "default"))}


# ---------------------------------------------------------------- probe and drafts

def test_probe_reads_schemas_codecs_sizes_fields_and_rates(roots):
    pr = _probe(roots["umi"])
    cam = pr.topics["/robot0/sensor/camera0/compressed"]
    assert (cam.kind, cam.codec, cam.width, cam.height, cam.count) == ("camera", "jpeg", 64, 48, 20)
    pose = pr.topics["/robot0/vio/eef_pose"]
    assert pose.schema == "foxglove.PoseInFrame" and pose.fields == [{"path": "pose.position", "size": 3},
                                                                    {"path": "pose.orientation", "size": 4}]
    assert pr.topics["/robot0/sensor/camera0/camera_info"].fields[-1] == {"path": "K", "size": 9}   # repeated
    assert pr.topics["/robot0/sensor/imu"].rate_hz(pr.end_ns) > 150
    assert pr.metadata["episode"]["task_name"] == "tidy up 0"
    abc = _probe(roots["abc"])
    assert (abc.topics["/camera/top"].codec, abc.topics["/camera/wrist"].codec) == ("h265", "h264")
    assert (abc.topics["/camera/top"].width, abc.topics["/camera/top"].height) == (64, 48)
    assert abc.topics["/left-arm-state"].fields[0] == {"path": "q", "size": 6}


def test_umi_draft_derives_exactly_the_readers_own_recognition(roots):
    from curation.ingest import mcap_reader as MR

    pr = _probe(roots["umi"])
    m, matched = MM.draft(pr)
    assert matched["template_id"] == "builtin:umi" and m["task"] == {"metadata_key": "task_name"}
    assert MM.validate(m, set(pr.topics)) == []
    assert MM.check_mapping(m) == MR._umi_mapping(sorted(pr.topics))
    # and the rows the checks read are the same with the derived mapping as without any
    auto = MR.read_mcap_rows(roots["umi"])
    derived = MR.read_mcap_rows(roots["umi"], mapping=MM.check_mapping(m))
    assert len(auto) == len(derived) == 2
    for a, b in zip(auto, derived):
        assert np.array_equal(a["action"], b["action"]) and np.array_equal(a["timestamps"], b["timestamps"])
        assert (a["instruction"], a["embodiment_id"], a["gripper_dims"], sorted(a["video"])) == (
            b["instruction"], b["embodiment_id"], b["gripper_dims"], sorted(b["video"]))


def test_default_convention_draft_reads_the_same_rows(roots):
    from curation.ingest import mcap_reader as MR

    pr = _probe(roots["default"])
    m, matched = MM.draft(pr)
    assert matched is None and m["task"] == {"topic": "/task"}
    chk = MM.check_mapping(m)
    assert (chk["action"], chk["state"], chk["task"]) == ("/action", "/observation.state", "/task")
    auto = MR.read_mcap_rows(roots["default"])
    derived = MR.read_mcap_rows(roots["default"], mapping=chk)
    assert np.array_equal(auto[0]["action"], derived[0]["action"])
    assert np.array_equal(auto[0]["proprio_state"], derived[0]["proprio_state"])
    assert auto[0]["instruction"] == derived[0]["instruction"] == "pick the cube"
    assert sorted(auto[0]["video"]) == sorted(derived[0]["video"])


def test_generic_draft_pairs_state_and_action_and_finds_segments(roots):
    pr = _probe(roots["abc"])
    m, matched = MM.draft(pr)
    assert m["base"] == "builtin:foxglove" and matched is None
    series = {s["topic"]: s for s in m["series"]}
    assert (series["/left-arm-state"]["role"], series["/left-arm-state"]["pair_with"]) == ("state", "/left-arm-action")
    assert series["/left-arm-action"]["fields"] == ["q"]
    assert m["segments"] == {"topic": "/subtask", "start_field": "start", "end_field": "end", "label_field": "label"}
    assert m["task"] == {"topic": "/instruction"}
    chk = MM.check_mapping(m)
    assert chk["action"] == [{"topic": "/left-arm-action", "fields": "q"}]
    assert chk["state"] == [{"topic": "/left-arm-state", "fields": "q"}]


def test_fast_arm_streams_stay_in_the_smart_layout_and_an_imu_does_not():
    """ABC-130k's arms publish at 200-270 Hz; the high-rate rule is for sensors like an IMU."""
    def tp(topic, schema, count, fields):
        return MP.TopicProbe(topic, schema, "protobuf", "protobuf", count, first_ns=0, kind="series",
                             fields=[{"path": f, "size": n} for f, n in fields])

    end = int(10e9)
    probe = MP.FileProbe("episode_0.mcap", {
        "/left-arm-state": tp("/left-arm-state", "RobotState", 2700, [("position", 6), ("velocity", 6), ("torque", 6)]),
        "/left-arm-action": tp("/left-arm-action", "RobotCommand", 2000, [("position", 6)]),
        "/imu": tp("/imu", "IMUMeasurement", 2000, [("ax", 1), ("ay", 1), ("az", 1), ("gx", 1), ("gy", 1)]),
        "/wheel/odom": tp("/wheel/odom", "Odometry", 3000, [("vx", 1), ("vy", 1)])}, start_ns=0, end_ns=end)
    series = {s["topic"]: s for s in MM.draft_generic(probe, "builtin:foxglove")["series"]}
    assert (series["/left-arm-state"]["fields"], series["/left-arm-state"]["role"]) == (["position"], "state")
    assert (series["/left-arm-action"]["fields"], series["/left-arm-action"]["pair_with"]) == (["position"], "/left-arm-state")
    assert series["/left-arm-state"].get("smart", True) and series["/left-arm-action"].get("smart", True)
    assert (series["/imu"]["role"], series["/imu"]["smart"], len(series["/imu"]["fields"])) == ("other", False, 4)
    assert series["/wheel/odom"]["smart"] is False                  # fast, and neither state nor action


def test_site_templates_match_by_coverage(roots):
    pr = _probe(roots["abc"])
    mine, _ = MM.draft(pr)
    site = [{"id": "vt-abcdefghi", "name": "ABC", "mapping": mine}]
    again, matched = MM.draft(pr, site_templates=site)
    assert matched == {"template_id": "vt-abcdefghi", "name": "ABC", "coverage": 1.0}
    assert again["cameras"] == mine["cameras"]
    other = _probe(roots["umi"])
    _, none = MM.draft(other, site_templates=site)
    assert none["template_id"] == "builtin:umi"
    # a UMI mapping the team adjusted and saved wins over the built-in recognition on a tie
    umi, _ = MM.draft(other)
    adjusted = {**umi, "cameras": [{**umi["cameras"][0], "name": "腕部相机"}]}
    mine, matched = MM.draft(other, site_templates=[{"id": "vt-umiteamab", "name": "我们的 UMI", "mapping": adjusted}])
    assert matched == {"template_id": "vt-umiteamab", "name": "我们的 UMI", "coverage": 1.0}
    assert mine["cameras"][0]["name"] == "腕部相机" and MM.check_mapping(mine) == MM.check_mapping(umi)


@pytest.mark.parametrize("change, problem", [
    (lambda m: m["cameras"].append({"topic": "/nope", "name": "x"}), "数据集里没有 topic /nope"),
    (lambda m: m["series"].append(dict(m["series"][0])), "同时出现在"),
    (lambda m: m["series"][0].update(pair_with="/robot0/sensor/magnetic_encoder"), "一组状态、一组动作"),
    (lambda m: m["ignore"].append("/robot0/vio/eef_pose"), "不能同时忽略"),
    (lambda m: m["timeline"].update(frame_reference="/robot0/sensor/imu"), "帧号基准"),
    (lambda m: m.update(schema_version="viz-mapping/9"), "viz-mapping/1.0"),
])
def test_validation_says_what_is_wrong(roots, change, problem):
    pr = _probe(roots["umi"])
    m, _ = MM.draft(pr)
    m["ignore"] = [t for t in m["ignore"] if t != "/robot0/sensor/imu"]
    change(m)
    problems = MM.validate(m, set(pr.topics))
    assert any(problem in p["problem"] for p in problems), problems


# ---------------------------------------------------------------- the episode pass

def test_scan_writes_frame_packs_remuxes_video_and_keeps_curves(roots, tmp_path):
    import av

    pr = _probe(roots["umi"])
    m, _ = MM.draft(pr)
    out = tmp_path / "umi"
    with open(f"{roots['umi']}/episode_0.mcap", "rb") as fh:
        doc = scan(fh, m, out)
    cam = doc["cameras"]["robot0_sensor_camera0_compressed"]
    pack = (out / "robot0_sensor_camera0_compressed.frames").read_bytes()
    assert cam["count"] == 20 and len(pack) == cam["bytes"] == sum(cam["size"])
    first = pack[cam["offset"][0]:cam["offset"][0] + cam["size"][0]]
    assert first[:2] == b"\xff\xd8" and cam["offset_s"] == 0.05                      # camera half a step late
    assert doc["frame_reference"] == "/robot0/vio/eef_pose" and len(doc["frame_times"]) == 20
    assert doc["check_clock"] == {"offset_s": 0.0, "fps": 10.0} and doc["task"] == "tidy up 0"
    z = np.load(out / "series.npz")
    assert z["robot0_vio_eef_pose__v"].shape == (20, 7)
    assert np.isclose(z["robot0_vio_eef_pose__v"][3, 0], 0.3)
    abc = tmp_path / "abc"
    m2, _ = MM.draft(_probe(roots["abc"]))
    with open(f"{roots['abc']}/episode_0.mcap", "rb") as fh:
        doc2 = scan(fh, m2, abc)
    for key, tag in (("camera_top", "hvc1"), ("camera_wrist", "avc1")):
        assert doc2["cameras"][key]["mp4"]
        with av.open(str(abc / f"{key}.mp4")) as inp:
            st = inp.streams.video[0]
            assert st.codec_tag == tag and len(list(inp.decode(st))) == 20
    assert doc2["segments"] == [{"start_s": 0.0, "end_s": 1.0, "label": "reach", "quality": None, "contribution": None,
                                 "arm": None, "flags": []}]
    assert doc2["check_clock"]["fps"] == 20.0 and doc2["task"] == "arrange the flowers in the vase"


def test_a_stream_that_starts_mid_gop_starts_at_its_first_keyframe(tmp_path):
    """GenRobot recordings start about a second into a GOP: those P-frames cannot decode, so the
    remux leaves them out and the camera starts later; the probe still finds the picture size."""
    import av

    from curation.viz import mcap_messages as M
    from curation.viz.remux import remux_annexb_file

    from .mcap_fixtures import _annexb

    for codec in ("h264", "h265"):
        samples = _annexb(codec, 30)[3:]               # a keyframe every 10: the first 7 are P-frames
        assert M.video_size(codec, samples[:6]) == (None, None)
        assert M.video_size(codec, samples) == (64, 48)
        path = tmp_path / f"{codec}.annexb"
        path.write_bytes(b"".join(samples))
        made = remux_annexb_file(str(path), [i / 10 for i in range(len(samples))], codec, str(tmp_path / f"{codec}.mp4"))
        assert (made.skipped, made.packets, made.lead_s, made.width, made.height) == (7, 20, 0.7, 64, 48)
        with av.open(str(tmp_path / f"{codec}.mp4")) as inp:
            frames = list(inp.decode(inp.streams.video[0]))
        assert len(frames) == 20 and frames[0].key_frame and frames[0].time == 0


# ---------------------------------------------------------------- REST

def _register(rt, root, name):
    from daemon.repo import protocol as P

    pf = {"schema_version": "1.0", "format": {"kind": "mcap", "version": None, "supported": True, "detail": "mcap"},
          "validation": [], "dataset": None, "modules": [], "meta_fingerprint": "sha256:" + "c" * 64, "warnings": []}
    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name=name, source="local", uri=str(root), preflight=pf, meta_fingerprint="sha256:" + "c" * 64,
        source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"}, preflighted_at=1))
    return ds


@pytest.fixture
def mc(client_for, roots, tmp_path):
    import shutil

    data = tmp_path / "inputs"
    for k, v in roots.items():
        shutil.copytree(v, data / k)
    c = client_for(base_path="/curation", local_data_root=data)
    rt = c.app.state.runtime
    c.ids = {k: _register(rt, data / k, k).id for k in roots}
    c.data = data
    return c


def _confirm(c, key):
    probe = c.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": c.ids[key]}})
    assert probe.status_code == 200, probe.text
    assert_schema("McapProbe", probe.json())
    r = c.put(f"{API}/datasets/{c.ids[key]}/mapping", json={"mapping": probe.json()["draft"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_mapping_lifecycle_and_the_model(mc):
    ds = mc.ids["umi"]
    body = mc.get(f"{API}/datasets/{ds}/viz").json()
    assert_schema("VizDataset", body)
    assert body["mapping"]["state"] == "none" and body["cameras"] == [] and body["warnings"][0]["code"] == "mapping_pending"
    assert_error(mc.get(f"{API}/datasets/{ds}/episodes/0/viz"), "validation_failed")
    item = next(d for d in mc.get(f"{API}/datasets").json()["items"] if d["id"] == ds)
    assert item["viz"]["state"] == "mapping_pending" and item["viz_mapping"]["state"] == "none"
    probe = mc.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds}}).json()
    uses = {t["topic"]: t["use"] for t in probe["topics"]}
    assert uses["/robot0/sensor/camera0/compressed"] == "camera" and uses["/robot0/sensor/imu"] == "ignore"
    assert probe["matched"]["template_id"] == "builtin:umi" and probe["files"] == 2
    assert probe["warnings"] == []                      # the check reader reads every UMI source
    doc = _confirm(mc, "umi")
    assert_schema("DatasetMapping", doc)
    assert (doc["state"], doc["version"]) == ("confirmed", 1) and doc["check_mapping"]["profile"] == "umi_das"
    assert mc.get(f"{API}/datasets/{ds}").json()["viz_mapping"]["version"] == 1
    # the list says it under the format (映射：<name>)
    item = next(d for d in mc.get(f"{API}/datasets").json()["items"] if d["id"] == ds)
    assert_schema("DatasetItem", item)
    assert (item["viz_mapping"]["state"], item["viz_mapping"]["name"]) == ("confirmed", doc["mapping"]["name"])
    model = mc.get(f"{API}/datasets/{ds}/viz").json()
    assert_schema("VizDataset", model)
    assert [(c["key"], c["kind"], c["access"], c["codec"]) for c in model["cameras"]] == [
        ("robot0_sensor_camera0_compressed", "frames", "frames", "jpeg")]
    assert [s["key"] for s in model["streams"]][:2] == ["robot0_vio_eef_pose", "robot0_sensor_magnetic_encoder"]
    assert model["streams"][0]["lines"][0]["name"] == "robot0_x"
    bad = mc.put(f"{API}/datasets/{ds}/mapping", json={"mapping": {**probe["draft"], "cameras": [{"topic": "/x", "name": "x"}]}})
    assert_error(bad, "validation_failed")
    assert mc.get(f"{API}/datasets/{ds}/mapping").json()["version"] == 1
    assert_error(mc.get(f"{API}/datasets/{mc.ids['umi']}/viz/meta", params={"path": "x"}), "validation_failed")


def test_episode_frames_curves_and_video(mc):
    _confirm(mc, "umi")
    ds = mc.ids["umi"]
    ep = mc.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    assert ep["timeline"]["kind"] == "timestamp" and len(ep["timeline"]["frame_times"]) == 20
    assert ep["task"] == {"text": "tidy up 1", "source": "原始标注"}
    cam = ep["cameras"][0]
    assert cam["url"].endswith(".frames") and cam["index_url"].endswith(".json") and cam["offset_s"] == 0.05
    idx = mc.get(cam["index_url"]).json()
    assert_schema("VizFrameIndex", idx)
    r = mc.get(cam["url"], headers={"Range": f"bytes={idx['offset'][2]}-{idx['offset'][2] + idx['size'][2] - 1}"})
    assert r.status_code == 206 and r.content[:2] == b"\xff\xd8" and len(r.content) == idx["size"][2]
    s = mc.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "robot0_vio_eef_pose"}).json()
    assert_schema("VizSeries", s)
    assert s["total_points"] == 20 and s["lines"][0]["name"] == "robot0_x"
    # a JPEG camera has no remux; the transcode reads the MJPEG mux of its frames
    first = mc.get(f"{API}/datasets/{ds}/episodes/1/cameras/robot0_sensor_camera0_compressed.mp4?transcode=1")
    assert first.status_code in (200, 202)
    deadline = time.monotonic() + 60
    while first.status_code == 202 and time.monotonic() < deadline:
        time.sleep(0.2)
        first = mc.get(f"{API}/datasets/{ds}/episodes/1/cameras/robot0_sensor_camera0_compressed.mp4?transcode=1")
    assert first.status_code == 200 and first.content[4:8] == b"ftyp"


def test_remuxed_video_with_range(mc):
    import av

    _confirm(mc, "abc")
    ds = mc.ids["abc"]
    model = mc.get(f"{API}/datasets/{ds}/viz").json()
    cams = {c["key"]: c for c in model["cameras"]}
    assert (cams["camera_top"]["access"], cams["camera_top"]["codec"], cams["camera_top"]["codec_string"]) == (
        "remux", "hevc", "hvc1.1.6.L120.90")
    pair = model["streams"][0]
    assert sorted({ln["role"] for ln in pair["lines"]}) == ["action", "state"] and len(pair["lines"]) == 12
    ep = mc.get(f"{API}/datasets/{ds}/episodes/0/viz").json()
    assert [t["key"] for t in ep["annotations"]["tracks"]] == ["segments"]
    url = f"{API}/datasets/{ds}/episodes/0/cameras/camera_wrist.mp4"
    whole = mc.get(url)
    assert whole.status_code == 200
    part = mc.get(url, headers={"Range": "bytes=0-31"})
    assert part.status_code == 206 and part.content == whole.content[:32]
    with av.open(io.BytesIO(whole.content)) as inp:
        assert len(list(inp.decode(inp.streams.video[0]))) == 20
    s = mc.get(f"{API}/datasets/{ds}/episodes/0/series", params={"stream": pair["key"]}).json()
    assert s["total_points"] == 40 and len(s["lines"]) == 12


def test_a_dataset_the_checks_cannot_read_with_the_defaults(mc):
    """ABC-130k-like: the preflight finds mcap files but not the default topics, so the checks call
    it unsupported; the visualizer still takes it as mcap with its mapping pending, and saving the
    mapping takes the registration's preflight again with it (here the check reader still cannot
    read a repeated protobuf field, which the mapping answer says)."""
    import shutil

    shutil.copytree(mc.data / "abc", mc.data / "abc_raw")             # not registered by the fixture
    r = mc.post(f"{API}/datasets", headers=JSON, json={"input": {"source": "local", "uri": str(mc.data / "abc_raw")}})
    assert r.status_code in (200, 201), r.text
    ds = r.json()
    assert (ds["format"], ds["viz"]["state"], ds["viz_mapping"]["state"]) == ("unsupported", "mapping_pending", "none")
    before = mc.get(f"{API}/datasets/{ds['id']}/mapping").json()
    assert [w["code"] for w in before["warnings"]] == ["checks_unreadable"]
    probe = mc.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds["id"]}}).json()
    assert [w["code"] for w in probe["warnings"]] == ["checks_gap", "checks_gap"]
    saved = mc.put(f"{API}/datasets/{ds['id']}/mapping", json={"mapping": probe["draft"]})
    assert saved.status_code == 200, saved.text
    # with the mapping the preflight finds every topic, but the reader cannot take `q` (a repeated
    # protobuf field picked by a path) nor an H.265 camera: the answer says the checks cannot use it
    assert saved.json()["state"] == "confirmed"
    gaps = [w["message"] for w in saved.json()["warnings"]]
    assert [w["code"] for w in saved.json()["warnings"]] == ["checks_gap", "checks_gap"], gaps
    assert "/left-arm-action 的 q" in gaps[0] and "/camera/top" in gaps[1]
    events = [e.action for e in mc.app.state.runtime.repo.list_events(resource=ds["id"], limit=50).items]
    assert "dataset.repreflight" in events
    assert mc.get(f"{API}/datasets/{ds['id']}").json()["viz"]["state"] == "ready"
    assert mc.get(f"{API}/datasets/{ds['id']}/episodes/0/viz").status_code == 200


def test_a_truncated_first_file_does_not_take_the_dataset_with_it(mc):
    path = mc.data / "umi" / "episode_0.mcap"
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])                 # no footer, no summary
    ds = mc.ids["umi"]
    probe = mc.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds}})
    assert probe.status_code == 200, probe.text
    assert probe.json()["file"] == "episode_1.mcap" and probe.json()["matched"]["template_id"] == "builtin:umi"
    assert mc.put(f"{API}/datasets/{ds}/mapping", json={"mapping": probe.json()["draft"]}).status_code == 200
    model = mc.get(f"{API}/datasets/{ds}/viz").json()
    assert [w["code"] for w in model["warnings"]] == ["file_unreadable"]
    assert "episode_0.mcap" in model["warnings"][0]["message"]
    assert mc.get(f"{API}/datasets/{ds}/episodes/1/viz").status_code == 200
    cut = mc.get(f"{API}/datasets/{ds}/episodes/0/viz")             # what was read before the break
    assert cut.status_code == 200, cut.text
    assert [w["code"] for w in cut.json()["warnings"]] == ["truncated"]
    assert 0 < cut.json()["frames"] < 20 and cut.json()["cameras"][0]["access"] == "frames"


def test_probe_before_registering_and_templates(mc):
    r = mc.post(f"{API}/viz/mcap-probe", json={"input": {"source": "local", "uri": str(mc.data / "abc")},
                                               "template": "builtin:ros2"})
    assert r.status_code == 200, r.text
    assert r.json()["matched"]["template_id"] == "builtin:ros2"
    assert_error(mc.post(f"{API}/viz/mcap-probe", json={"input": {"source": "local", "uri": str(mc.data / "abc")},
                                                        "template": "vt-zzzzzzzzz"}), "not_found")
    lst = mc.get(f"{API}/viz/templates").json()
    assert [t["id"] for t in lst["items"]] == ["builtin:umi", "builtin:foxglove", "builtin:ros2"]
    draft = r.json()["draft"]
    made = mc.post(f"{API}/viz/templates", json={"name": "ABC 双臂", "mapping": draft})
    assert made.status_code == 201, made.text
    assert_schema("VizTemplate", made.json())
    assert_error(mc.post(f"{API}/viz/templates", json={"name": "ABC 双臂", "mapping": draft}), "name_taken")
    again = mc.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": mc.ids["abc"]}}).json()
    assert again["matched"]["template_id"] == made.json()["id"]
    assert mc.delete(f"{API}/viz/templates/{made.json()['id']}", headers=JSON).status_code == 204
    assert_error(mc.delete(f"{API}/viz/templates/{made.json()['id']}", headers=JSON), "not_found")
    assert_error(mc.delete(f"{API}/viz/templates/builtin:umi", headers=JSON), "validation_failed")
    assert_error(mc.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": mc.ids["default"]}, "file": "nope.mcap"}),
                 "validation_failed")


def test_the_mapping_reaches_preflight_and_runs(mc):
    """D62: the derived mapping goes to the CLI with --set; the run freezes it into run.json."""
    import types

    rt = mc.app.state.runtime
    _confirm(mc, "umi")
    from daemon.orchestr.datasets import Source
    from daemon.orchestr.planning import frozen_viz_mapping
    from daemon.orchestr.service import orchestrator_of

    ops = orchestrator_of(rt).datasets
    ds = rt.repo.get_dataset(mc.ids["umi"])
    args = ops.mapping_args(Source("local", ds.uri, None, None), "default")
    assert args[0] == "--set" and json.loads(args[1].split("=", 1)[1])["profile"] == "umi_das"
    assert ops.mapping_args(Source("local", "/elsewhere", None, None), "default") == []
    task = types.SimpleNamespace(dataset_id=ds.id, owner_id="default")
    frozen = frozen_viz_mapping(types.SimpleNamespace(task=task, repo=rt.repo))
    assert frozen["version"] == 1 and frozen["check_mapping"]["video_topics"] == ["/robot0/sensor/camera0/compressed"]
    import contextlib

    from curation.cli.app import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["preflight", "--input", ds.uri, "--json", *args])
    assert code == 0, out.getvalue()
    pf = json.loads(out.getvalue())
    assert pf["format"]["kind"] == "mcap" and pf["dataset"]["episode_count"] == 2
    assert "topics mapped by ingest.mcap_mapping" in pf["format"]["detail"]
