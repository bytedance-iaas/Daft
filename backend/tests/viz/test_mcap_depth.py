"""mcap depth (design doc 21 §5.4, D68, F15.5): depth topics probed and drafted into the mapping's depths
(C7 viz-mapping/1.1) and paired with their camera, scanned into 16-bit PNG packs - a recording's own 16-bit
PNGs kept byte for byte, ROS compressedDepth, 16UC1 and 32FC1 raw pictures decoded - and served as depth
streams; raw colour pictures and point clouds are no depth."""
from __future__ import annotations

import json

import numpy as np
import pytest

from curation.viz import depth as D
from curation.viz import mcap_mapping as MM
from curation.viz import mcap_probe as MP
from curation.viz.mcap_episode import scan

from .conftest import assert_schema
from .mcap_fixtures import depth16, png16

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")

API = "/curation/api/v1"
N = 12


@pytest.fixture(scope="module")
def rgbd(tmp_path_factory):
    from .mcap_fixtures import make_rgbd

    return make_rgbd(str(tmp_path_factory.mktemp("rgbd") / "rgbd"), n=N)


def _probe(root):
    with open(f"{root}/episode_0.mcap", "rb") as fh:
        return MP.probe(fh, "episode_0.mcap")


def test_depth_topics_are_probed_as_depth(rgbd):
    pr = _probe(rgbd)
    kinds = {t: (tp.kind, tp.codec, tp.width, tp.height) for t, tp in pr.topics.items()}
    assert kinds["/front-depth"] == ("depth", "png16", 64, 48)
    assert kinds["/raw-depth"] == ("depth", "raw16", 64, 48)
    assert kinds["/float-depth"] == ("depth", "raw32f", 64, 48)
    assert kinds["/wrist/depth/compressedDepth"] == ("depth", "cdepth", 64, 48)
    assert kinds["/raw-color"][:2] == ("camera", "raw")
    assert kinds["/cloud"][0] == "other" and kinds["/front-camera"][:2] == ("camera", "jpeg")


def test_the_draft_lists_depths_paired_with_their_camera(rgbd):
    pr = _probe(rgbd)
    m, _ = MM.draft(pr)
    assert m["schema_version"] == "viz-mapping/1.1" and MM.validate(m, set(pr.topics)) == []
    depths = {d["topic"]: d for d in m["depths"]}
    assert set(depths) == {"/front-depth", "/raw-depth", "/float-depth", "/wrist/depth/compressedDepth"}
    assert depths["/front-depth"]["pair_with"] == "/front-camera"                 # front-depth <-> front-camera
    assert depths["/wrist/depth/compressedDepth"]["pair_with"] is None             # no wrist camera
    assert "/front-depth" not in {c["topic"] for c in m["cameras"]}
    # the checks never see depth (design doc 21 §5.4: verdicts unchanged)
    assert all("depth" not in t for t in MM.check_mapping(m)["video_topics"])
    bad = json.loads(json.dumps(m))
    bad["depths"][0]["pair_with"] = "/cloud"
    assert any("不是映射里的相机" in p["problem"] for p in MM.validate(bad))


def test_the_scan_makes_16_bit_packs_of_every_writing(rgbd, tmp_path):
    pr = _probe(rgbd)
    m, _ = MM.draft(pr)
    with open(f"{rgbd}/episode_0.mcap", "rb") as fh:
        doc = scan(fh, m, tmp_path)
    from curation.viz.mcap_episode import depth_keys

    keys = depth_keys(m)
    for topic, key in keys.items():
        assert doc["depths"][key]["count"] == N and doc["depths"][key]["error"] is None, (topic, doc["depths"][key])
        idx = json.loads((tmp_path / f"depth-{key}.json").read_text())
        assert_schema("VizFrameIndex", idx)
        assert (idx["codec"], idx["count"], idx["width"], idx["height"]) == ("png16", N, 64, 48)
        lo, hi = idx["depth"]["lo"], idx["depth"]["hi"]
        assert 500 <= lo < hi <= 2000
        pack = (tmp_path / f"depth-{key}.frames").read_bytes()
        for i in range(N):
            frame = pack[idx["offset"][i]:idx["offset"][i] + idx["size"][i]]
            assert np.array_equal(D.decode_png(frame), depth16(i)), (topic, i)
            if topic == "/front-depth":                   # a recording's own 16-bit PNG, byte for byte
                assert frame == png16(depth16(i))
    assert doc["depths"][keys["/front-depth"]]["offset_s"] == 0.0


def test_a_recording_without_its_summary_keeps_its_depth(rgbd, tmp_path):
    # cut after the data (the fixture writes one chunk): no summary, no footer - one pass from the top
    # with a non-seeking reader, which takes no second query, and no message counts to space the range samples by
    m, _ = MM.draft(_probe(rgbd))
    raw = open(f"{rgbd}/episode_0.mcap", "rb").read()
    cut = tmp_path / "cut.mcap"
    cut.write_bytes(raw[: len(raw) * 4 // 5])
    from curation.viz.mcap_episode import depth_keys

    with open(cut, "rb") as fh:
        doc = scan(fh, m, tmp_path / "out")
    assert "truncated" in [w["code"] for w in doc["warnings"]]
    for topic, key in depth_keys(m).items():
        assert doc["depths"][key]["count"] == N and doc["depths"][key]["error"] is None, topic
        idx = json.loads((tmp_path / "out" / f"depth-{key}.json").read_text())
        pack = (tmp_path / "out" / f"depth-{key}.frames").read_bytes()
        assert idx["depth"]["lo"] < idx["depth"]["hi"]
        assert np.array_equal(D.decode_png(pack[idx["offset"][N - 1]:idx["offset"][N - 1] + idx["size"][N - 1]]), depth16(N - 1))


def test_32fc1_compressed_depth_is_decoded_from_its_inverse_depth():
    import struct

    d = depth16(3).astype(np.float64) / 1000.0
    qa, qb = 100.0, 1.0
    inv = np.where(d > 0, qa / d + qb, 0).round().astype(np.uint16)
    data = struct.pack("<iff", 1, qa, qb) + D.encode_png16(inv)
    mm = D.depth_message("cdepth32", data)
    ok = depth16(3) > 0
    assert np.all(mm[~ok] == 0)
    assert np.max(np.abs(mm[ok].astype(int) - depth16(3)[ok].astype(int))) <= 12   # the quantisation's error


def test_the_api_serves_mcap_depth_streams(client_for, rgbd, tmp_path):
    import shutil

    from .test_mcap import _register

    data = tmp_path / "inputs"
    shutil.copytree(rgbd, data / "rgbd")
    c = client_for(base_path="/curation", local_data_root=data)
    ds = _register(c.app.state.runtime, data / "rgbd", "rgbd")
    probe = c.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds.id}}).json()
    assert_schema("McapProbe", probe)
    uses = {t["topic"]: (t["use"], t["image"]) for t in probe["topics"]}
    assert uses["/front-depth"] == ("depth", {"codec": "png16", "width": 64, "height": 48})
    assert c.put(f"{API}/datasets/{ds.id}/mapping", json={"mapping": probe["draft"]}).status_code == 200
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    assert_schema("VizDataset", model)
    depth = {s["sources"][0]: s for s in model["streams"] if s["kind"] == "depth"}
    assert depth["/front-depth"]["available"] and depth["/front-depth"]["depth"]["pair_camera"] == "front-camera"
    ep = c.get(f"{API}/datasets/{ds.id}/episodes/0/viz").json()
    assert_schema("VizEpisode", ep)
    s = next(x for x in ep["streams"] if x["key"] == depth["/float-depth"]["key"])
    r = c.get(s["index_url"].replace("/curation/api/v1", API))
    assert r.status_code == 200                           # scanned with the episode: no 202
    idx = r.json()
    a, b = idx["offset"][5], idx["offset"][5] + idx["size"][5] - 1
    part = c.get(s["url"].replace("/curation/api/v1", API), headers={"Range": f"bytes={a}-{b}"})
    assert part.status_code == 206 and np.array_equal(D.decode_png(part.content), depth16(5))
    # topic and stream nodes of the field tree point at the depth stream
    topics = {n["name"]: n for n in next(n for n in model["field_tree"] if n["id"] == "topics")["children"]}
    assert topics["/front-depth"]["stream"] == depth["/front-depth"]["key"]
    assert topics["/front-depth"]["detail"]["format"] == "png"


def test_a_mapping_made_before_depths_says_what_to_change(client_for, rgbd, tmp_path):
    import shutil

    from .test_mcap import _register

    data = tmp_path / "inputs"
    shutil.copytree(rgbd, data / "rgbd")
    c = client_for(base_path="/curation", local_data_root=data)
    ds = _register(c.app.state.runtime, data / "rgbd", "rgbd")
    old = {"schema_version": "viz-mapping/1.0", "name": "旧映射", "base": None,
           "timeline": {"source": "log_time", "frame_reference": None},
           "cameras": [{"topic": "/front-camera", "name": "front"}, {"topic": "/front-depth", "name": "front depth"}],
           "series": [{"topic": "/arm-action", "name": "arm", "role": "action"}], "task": None, "segments": None, "ignore": []}
    assert c.put(f"{API}/datasets/{ds.id}/mapping", json={"mapping": old}).status_code == 200
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    cam = next(x for x in model["cameras"] if x["source"] == "/front-depth")
    assert cam["access"] == "unsupported" and "深度图" in cam["reason"]
    assert not any(s["kind"] == "depth" for s in model["streams"])
