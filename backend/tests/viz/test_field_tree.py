"""The dataset info tree in the dataset's own words (design doc 21 §3, D71, F15.2): every feature of
info.json once, named by its key, its attributes the info.json entry key by key; mcap topics with their
channel, schema and statistics fields; Lance tables with num_rows and columns. Nothing translated."""
from __future__ import annotations

import json
import re

import pytest

from curation.viz.lerobot_info import raw_detail

from .conftest import assert_schema
from .test_fixes import _register

API = "/curation/api/v1"
CJK = re.compile(r"[一-鿿]")


def _nodes(tree):
    for n in tree:
        yield n
        yield from _nodes(n.get("children") or [])


def _leaves(tree):
    return [n for n in _nodes(tree) if n["kind"] != "group"]


@pytest.mark.parametrize("name", ["lerobot_v2", "lerobot_v3", "lerobot_v3_depth"])
def test_lerobot_features_once_with_their_info_json_entries(client_for, data_root, name):
    c = client_for(base_path="/curation", local_data_root=data_root)
    version = "v2" if name == "lerobot_v2" else "v3"
    ds = _register(c.app.state.runtime, data_root / name, name, version=version, fp="sha256:" + "a" * 64)
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    assert_schema("VizDataset", model)
    info = json.loads((data_root / name / "meta" / "info.json").read_text())
    feats = info["features"]
    groups = {g["id"]: g.get("children") or [] for g in model["field_tree"]}
    nodes = [n for gid in ("cameras", "depth", "streams", "other") for n in groups.get(gid, [])]
    annotated = {c for n in groups["annotations"] if n["id"].startswith("annotation:")
                 for c in n["name"].split("、") if c in feats}
    names = [n["name"] for n in nodes]
    assert len(names) == len(set(names)) and not set(names) & annotated
    assert set(names) | annotated == set(feats)                               # every feature, once
    for n in nodes:
        assert n["detail"] == raw_detail(feats[n["name"]]), n["name"]
    for n in _nodes(model["field_tree"]):
        assert not any(CJK.search(k) for k in (n.get("detail") or {})), n
    streams = {s["key"]: s for s in model["streams"]}
    for n in nodes:
        if n["kind"] == "series":
            assert n["name"] in streams[n["stream"]]["sources"]
        if n["kind"] == "camera":
            assert n["camera"] in {cam["key"] for cam in model["cameras"]}
    cam = next(n for n in nodes if n["kind"] == "camera")
    assert cam["detail"]["dtype"] == "video" and "info.video.codec" in cam["detail"] or "video_info.video.codec" in cam["detail"]
    if name == "lerobot_v3_depth":
        depth = {n["name"]: n for n in nodes if n["kind"] == "depth"}
        assert set(depth) == {"observation.images.front.depth", "observation.depths.top"}
        assert all(n["stream"] in streams and streams[n["stream"]]["kind"] == "depth" for n in depth.values())
        assert json.loads(depth["observation.depths.top"]["detail"]["shape"]) == [48, 64]


def test_lance_tables_with_num_rows_and_columns(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root)
    ds = _register(c.app.state.runtime, data_root / "lance_03", "lance_03", fp="sha256:" + "b" * 64)
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    tables = next(n for n in model["field_tree"] if n["id"] == "lance")["children"]
    for t in tables:
        assert set(t["detail"]) == {"num_rows", "columns"} and isinstance(json.loads(t["detail"]["columns"]), list)


def test_mcap_topics_with_their_channel_schema_and_statistics_fields(client_for, tmp_path):
    pytest.importorskip("mcap_protobuf")
    from .mcap_fixtures import make_umi
    from .test_mcap import _register as register_mcap

    root = make_umi(str(tmp_path / "inputs" / "umi"), episodes=1)
    c = client_for(base_path="/curation", local_data_root=tmp_path / "inputs")
    ds = register_mcap(c.app.state.runtime, root, "umi")
    draft = c.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds.id}}).json()["draft"]
    assert c.put(f"{API}/datasets/{ds.id}/mapping", json={"mapping": draft}).status_code == 200
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    topics = {n["name"]: n for n in next(n for n in model["field_tree"] if n["id"] == "topics")["children"]}
    cam = topics["/robot0/sensor/camera0/compressed"]["detail"]
    assert cam == {"schema.name": "foxglove.CompressedImage", "schema.encoding": "protobuf",
                   "message_encoding": "protobuf", "message_count": 20, "format": "jpeg", "width": 64, "height": 48}
    pose = topics["/robot0/vio/eef_pose"]["detail"]
    assert json.loads(pose["fields"]) == {"pose.position": 3, "pose.orientation": 4}
    meta = next(n for n in model["field_tree"] if n["id"] == "metadata")["children"]
    assert meta[0]["detail"]["task_name"] == "tidy up 0"
    for n in _nodes(model["field_tree"]):
        assert not any(CJK.search(k) for k in (n.get("detail") or {})), n
