"""The visualizer's REST surface on local LeRobot datasets (C4 2.4.0, design doc 18, F13.2)."""
from __future__ import annotations

import io
import json
import time
import zipfile

import numpy as np
import pyarrow.parquet as pq
import pytest

from .conftest import assert_error, assert_schema
from .fixtures import LENGTHS, SUBTASKS

API = "/curation/api/v1"


def _preflight(version: str) -> dict:
    return {"schema_version": "1.0",
            "format": {"kind": "lerobot", "version": version, "supported": True, "detail": "LeRobot"},
            "validation": [], "dataset": {"episode_count": 3, "cameras": ["front"], "fps": 10.0,
                                          "robot_type": "franka", "total_frames": 90,
                                          "labels": {"with_task": 3, "without_task": 0}, "profile": None},
            "modules": [], "meta_fingerprint": "sha256:" + version[1] * 64, "warnings": []}


@pytest.fixture
def app(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root)
    rt = c.app.state.runtime
    from daemon.repo import protocol as P

    ids = {}
    for version in ("v2", "v3"):
        ds, _ = rt.repo.register_dataset(P.Dataset(
            id="", name=f"lerobot_{version}", source="local", uri=str(data_root / f"lerobot_{version}"),
            preflight=_preflight(version), meta_fingerprint="sha256:" + version[1] * 64,
            source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"}, preflighted_at=1))
        ids[version] = ds.id
    c.ids = ids
    return c


def _wait(c, url, *, headers=None, timeout=60.0):
    deadline = time.monotonic() + timeout
    while True:
        r = c.get(url, headers=headers or {})
        if r.status_code != 202 or time.monotonic() > deadline:
            return r
        assert_schema("VizMediaPending", r.json())
        time.sleep(0.2)


def test_dataset_model_v2(app):
    r = app.get(f"{API}/datasets/{app.ids['v2']}/viz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("VizDataset", body)
    assert body["format"] == {"kind": "lerobot", "version": "v2", "reader": "lerobot", "layout": None}
    assert (body["episode_count"], body["fps"], body["total_frames"]) == (3, 10.0, sum(LENGTHS))
    cams = {c["key"]: c for c in body["cameras"]}
    assert cams["front"]["access"] == "local" and cams["front"]["codec"] == "h264"
    assert cams["wrist"]["access"] == "transcode" and cams["wrist"]["transcoded"] and "mpeg4" in cams["wrist"]["reason"]
    streams = {s["key"]: s for s in body["streams"]}
    assert streams["observation_state"]["smart"] and streams["observation_state.gripper"]["name"] == "observation.state / action · gripper"
    assert not streams["observation_force"]["smart"]
    sources = {s["key"]: s for s in body["annotation_sources"]}
    assert sources["low_level_task_index"]["primary"] and sources["subtask"]["format"] == "string_column"
    tree = {n["id"]: n for n in body["field_tree"]}
    assert [n["camera"] for n in tree["cameras"]["children"]] == ["front", "wrist"]
    assert any(n.get("file") == "meta/info.json" for n in tree["meta"]["children"])


def test_episode_list_filters_sorts_and_pages(app):
    url = f"{API}/datasets/{app.ids['v2']}/viz/episodes"
    r = app.get(url, params={"sort": "duration", "order": "desc", "limit": 2})
    body = r.json()
    assert_schema("VizEpisodePage", body)
    assert [e["index"] for e in body["items"]] == [2, 0] and body["has_more"] and body["total"] == 3
    rest = app.get(url, params={"sort": "duration", "order": "desc", "limit": 2, "cursor": body["next_cursor"]}).json()
    assert [e["index"] for e in rest["items"]] == [1] and not rest["has_more"]
    assert [e["index"] for e in app.get(url, params={"q": "ep1"}).json()["items"]] == [1]
    assert app.get(url, params={"q": "lift"}).json()["total"] == 3
    assert app.get(url, params={"q": "nothing"}).json()["items"] == []


def test_episode_v2_annotations_cameras_and_series(app, data_root):
    ds = app.ids["v2"]
    r = app.get(f"{API}/datasets/{ds}/episodes/1/viz")
    assert r.status_code == 200, r.text
    ep = r.json()
    assert_schema("VizEpisode", ep)
    assert (ep["frames"], ep["duration_s"], ep["fps"], ep["check_clock"]) == (LENGTHS[1], LENGTHS[1] / 10, 10.0, None)
    assert ep["task"] == {"text": "Lift the cup", "source": "原始标注"}
    tracks = {t["key"]: t for t in ep["annotations"]["tracks"]}
    assert tracks["low_level_task_index"]["primary"]
    assert [s["label"] for s in tracks["low_level_task_index"]["segments"]] == SUBTASKS
    assert [s["flags"] for s in tracks["flags"]["segments"]] == [["is_intervention_segment"]]
    assert {lb["value"] for lb in ep["annotations"]["labels"]} == {"recovered"}
    assert any(w["code"] == "annotation_placeholder" for w in ep["annotations"]["warnings"])
    cams = {c["key"]: c for c in ep["cameras"]}
    assert cams["front"]["url"].endswith(f"/datasets/{ds}/episodes/1/cameras/front.mp4")
    assert cams["front"]["transcode_url"].endswith("front.mp4?transcode=1")
    assert cams["wrist"]["transcoded"] and cams["wrist"]["transcode_url"] is None

    s = app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "observation_state"})
    series = s.json()
    assert_schema("VizSeries", series)
    raw = pq.read_table(data_root / "lerobot_v2/data/chunk-000/episode_000001.parquet").to_pandas()
    state = np.stack(raw["observation.state"].to_numpy())
    j0 = next(ln for ln in series["lines"] if ln["name"] == "joint_0" and ln["role"] == "state")
    assert np.allclose(j0["values"], state[:, 0], atol=1e-4)                     # full precision, every point
    assert series["total_points"] == LENGTHS[1] and not series["downsampled"]
    part = app.get(f"{API}/datasets/{ds}/episodes/1/series",
                   params={"stream": "observation_state", "from": 1.0, "to": 1.5}).json()
    assert part["t"][0] == 1.0 and part["t"][-1] == 1.5 and part["total_points"] == 6
    assert_error(app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "nope"}), "not_found")
    assert_error(app.get(f"{API}/datasets/{ds}/episodes/9/viz"), "not_found")
    assert_error(app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "observation_state", "from": 2, "to": 1}),
                 "validation_failed")


def test_local_camera_bytes_with_range_and_the_transcode(app, data_root):
    ds = app.ids["v2"]
    whole = (data_root / "lerobot_v2/videos/chunk-000/observation.images.front/episode_000000.mp4").read_bytes()
    r = app.get(f"{API}/datasets/{ds}/episodes/0/cameras/front.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.content == whole[:100]
    assert r.headers["content-type"] == "video/mp4"
    first = app.get(f"{API}/datasets/{ds}/episodes/0/cameras/wrist.mp4")
    assert first.status_code in (200, 202)
    done = _wait(app, f"{API}/datasets/{ds}/episodes/0/cameras/wrist.mp4")
    assert done.status_code == 200 and done.content[4:8] == b"ftyp"
    import av

    with av.open(io.BytesIO(done.content)) as inp:
        st = inp.streams.video[0]
        frames = list(inp.decode(st))
        assert st.codec_context.name == "h264" and len(frames) == LENGTHS[0]
    # the player's fallback for a direct / local camera it cannot decode
    asked = _wait(app, f"{API}/datasets/{ds}/episodes/0/cameras/front.mp4?transcode=1")
    assert asked.status_code == 200
    assert_error(app.get(f"{API}/datasets/{ds}/episodes/0/cameras/nope.mp4"), "not_found")


def test_v3_reads_its_row_window_and_subtasks(app, data_root):
    ds = app.ids["v3"]
    body = app.get(f"{API}/datasets/{ds}/viz").json()
    assert_schema("VizDataset", body)
    assert [s["key"] for s in body["annotation_sources"] if s["primary"]] == ["subtask_index"]
    ep = app.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    cam = ep["cameras"][0]
    assert (cam["from_ts"], cam["to_ts"]) == (LENGTHS[0] / 10, (LENGTHS[0] + LENGTHS[1]) / 10)
    tracks = {t["key"]: t for t in ep["annotations"]["tracks"]}
    assert [s["label"] for s in tracks["subtask_index"]["segments"]] == SUBTASKS
    assert [s["label"] for s in tracks["language_persistent"]["segments"]] == SUBTASKS
    series = app.get(f"{API}/datasets/{ds}/episodes/1/series", params={"stream": "observation_state"}).json()
    assert series["total_points"] == LENGTHS[1]
    raw = pq.read_table(data_root / "lerobot_v3/data/chunk-000/file-000.parquet").to_pandas()
    mine = raw[raw["episode_index"] == 1]
    j3 = next(ln for ln in series["lines"] if ln["name"] == "joint_3" and ln["role"] == "action")
    assert np.allclose(j3["values"], np.stack(mine["action"].to_numpy())[:, 3], atol=1e-4)


def test_meta_preview(app):
    ds = app.ids["v2"]
    r = app.get(f"{API}/datasets/{ds}/viz/meta", params={"path": "meta/info.json"})
    body = r.json()
    assert_schema("VizMetaFile", body)
    assert body["kind"] == "json" and json.loads(body["text"])["fps"] == 10 and not body["truncated"]
    assert_error(app.get(f"{API}/datasets/{ds}/viz/meta", params={"path": "data/chunk-000/episode_000000.parquet"}),
                 "validation_failed")
    assert_error(app.get(f"{API}/datasets/{ds}/viz/meta", params={"path": "../../etc/passwd"}), "validation_failed")


def test_external_annotations_upload_and_attach(app):
    ds = app.ids["v2"]
    doc = {"event_labels": [{"t_s": 0.0, "end_s": 1.0, "verb_class": "reach", "object": "cup", "contribution": "advancing"}],
           "key_events": [{"t_s": 0.5, "label": "touch", "outcome": "success"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("episode_000001.json", json.dumps(doc))
    r = app.post(f"{API}/uploads", params={"kind": "viz_annotations", "name": "labels.zip"}, content=buf.getvalue(),
                 headers={"Content-Type": "application/zip"})
    assert r.status_code == 201, r.text
    up = r.json()
    assert_schema("Upload", up)
    assert up["validation"]["summary"]["episodes"] == 1
    bad = app.post(f"{API}/uploads", params={"kind": "viz_annotations", "name": "x.json"},
                   content=json.dumps({"timeline_x": 1}), headers={"Content-Type": "application/json"})
    assert_error(bad, "validation_failed")
    zip_wrong_kind = app.post(f"{API}/uploads", params={"kind": "eef_trajectory", "name": "x.zip"}, content=buf.getvalue(),
                              headers={"Content-Type": "application/zip"})
    assert_error(zip_wrong_kind, "validation_failed")
    put = app.put(f"{API}/datasets/{ds}/annotations", json={"upload_id": up["upload_id"]})
    assert put.status_code == 200, put.text
    assert_schema("DatasetDetail", put.json())
    assert put.json()["annotations"]["episodes"] == 1
    model = app.get(f"{API}/datasets/{ds}/viz").json()
    assert any(s["format"] == "argus" for s in model["annotation_sources"])
    ep = app.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    ext = next(t for t in ep["annotations"]["tracks"] if t["key"] == "external")
    assert ext["segments"][0]["label"] == "reach · cup" and ep["annotations"]["events"][0]["label"] == "touch"
    off = app.put(f"{API}/datasets/{ds}/annotations", json={"upload_id": None})
    assert off.json()["annotations"] is None


def test_task_scope_reads_the_tasks_input(app, data_root):
    rt = app.app.state.runtime
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P

    rows = [P.TaskModule(task_id="", module_id=m, selected=False, availability="available") for m in registry.ids()]
    task = rt.repo.create_task(P.TaskCreate(
        name="viz", input_source="local", input_uri=str(data_root / "lerobot_v2"), output_uri="tos://b/out",
        delivery_key="tos://b/out", episode_selector={"mode": "all"}, params={}, modules=rows,
        dataset_id=app.ids["v2"]))
    body = app.get(f"{API}/tasks/{task.id}/viz").json()
    assert_schema("VizDataset", body)
    assert (body["scope"], body["id"], body["dataset_id"]) == ("task", task.id, app.ids["v2"])
    ep = app.get(f"{API}/tasks/{task.id}/episodes/2/viz").json()
    assert_schema("VizEpisode", ep)
    assert ep["check_clock"] == {"offset_s": 0.0, "fps": 10.0}
    assert ep["cameras"][0]["url"].endswith(f"/tasks/{task.id}/episodes/2/cameras/front.mp4")
    s = app.get(f"{API}/tasks/{task.id}/episodes/2/series", params={"stream": "observation_state.gripper"}).json()
    assert_schema("VizSeries", s)
    r = app.get(f"{API}/tasks/{task.id}/episodes/2/cameras/front.mp4", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    assert_error(app.get(f"{API}/tasks/task-nothereno/viz"), "not_found")


def test_transcoding_switched_off(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root, viz_transcode=False)
    rt = c.app.state.runtime
    from daemon.repo import protocol as P

    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name="v2", source="local", uri=str(data_root / "lerobot_v2"), preflight=_preflight("v2"),
        meta_fingerprint="sha256:" + "f" * 64, source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"},
        preflighted_at=1))
    body = c.get(f"{API}/datasets/{ds.id}/viz").json()
    wrist = next(cam for cam in body["cameras"] if cam["key"] == "wrist")
    assert wrist["access"] == "unsupported" and "CURATOR_VIZ_TRANSCODE=0" in wrist["reason"]
    assert body["transcode"] == {"enabled": False}
    assert any(w["code"] == "camera_unsupported" for w in body["warnings"])
    ep = c.get(f"{API}/datasets/{ds.id}/episodes/0/viz").json()
    assert all(cam["transcode_url"] is None for cam in ep["cameras"])
    err = assert_error(c.get(f"{API}/datasets/{ds.id}/episodes/0/cameras/front.mp4?transcode=1"), "not_found")
    assert err["error"]["details"]["reason"] == "transcode_disabled"


def test_a_lerobot_dataset_the_checks_cannot_read_is_still_shown(app, data_root):
    """Galaxea-like (F13.8): the check reader refuses a LeRobot dataset with no ``action`` column, so
    the preflight says unsupported; the visualizer reads info.json, the data and the videos all the same."""
    import shutil

    rt = app.app.state.runtime
    from daemon.repo import protocol as P

    if not (data_root / "lerobot_v2_no_action").exists():
        shutil.copytree(data_root / "lerobot_v2", data_root / "lerobot_v2_no_action")
    pf = _preflight("v2")
    pf["format"] = {"kind": "lerobot", "version": "v2", "supported": False,
                    "detail": "LeRobot dataset with invalid metadata: features 里没有 'action'"}
    pf["dataset"] = None
    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name="no_action", source="local", uri=str(data_root / "lerobot_v2_no_action"), preflight=pf,
        meta_fingerprint="sha256:" + "9" * 64, source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"},
        preflighted_at=2))
    item = next(d for d in app.get(f"{API}/datasets").json()["items"] if d["id"] == ds.id)
    assert (item["format"], item["viz"]) == ("unsupported", {"state": "ready", "reason": None})
    assert ds.id in [d["id"] for d in app.get(f"{API}/datasets", params={"viz": "true"}).json()["items"]]
    body = app.get(f"{API}/datasets/{ds.id}/viz").json()
    assert_schema("VizDataset", body)
    assert body["format"]["reader"] == "lerobot" and body["cameras"]
    ep = app.get(f"{API}/datasets/{ds.id}/episodes/0/viz")
    assert ep.status_code == 200, ep.text


def test_unsupported_and_pending_formats(app):
    rt = app.app.state.runtime
    from daemon.repo import protocol as P

    # Lance tables that are not lerobot-lancedb's (no LeRobot meta/): no reader (Lance itself: test_lance.py)
    lance_pf = {**_preflight("v3"), "format": {"kind": "lancedb", "version": None, "supported": False, "detail": "lance"}}
    lance, _ = rt.repo.register_dataset(P.Dataset(
        id="", name="lance", source="local", uri="/nowhere/lance", preflight=lance_pf, meta_fingerprint="x",
        source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"}, preflighted_at=1))
    body = app.get(f"{API}/datasets/{lance.id}/viz").json()
    assert_schema("VizDataset", body)
    assert body["format"]["reader"] is None and body["warnings"][0]["code"] == "unsupported"
    assert_error(app.get(f"{API}/datasets/{lance.id}/episodes/0/viz"), "validation_failed")
    item = next(d for d in app.get(f"{API}/datasets").json()["items"] if d["id"] == lance.id)
    assert item["viz"]["state"] == "unsupported"
