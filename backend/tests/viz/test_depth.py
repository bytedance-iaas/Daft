"""Depth streams of LeRobot and Lance datasets (design doc 21 §5, D68, F15.4): 16-bit PNG frame packs
the Daemon makes from the depth columns, their index (unit, holes, 2 % / 98 % range), the model's depth
streams paired with their cameras, and the episode's stream URLs."""
from __future__ import annotations

import time

import numpy as np
import pyarrow as pa
import pytest

from curation.viz import depth as D

from .conftest import assert_error, assert_schema
from .fixtures import DEPTH_LENGTHS, FPS, H, W, depth_mm
from .test_fixes import _register

API = "/curation/api/v1"


# ---------------------------------------------------------------- the kernel

def test_png16_round_trips_and_passes_for_a_depth_png():
    a = depth_mm(7)
    png = D.encode_png16(a)
    assert D.is_png16(png) and D.png_header(png)["width"] == W
    assert np.array_equal(D.decode_png(png), a)


def test_frames_of_nested_and_fixed_size_columns_and_metres():
    frames = [depth_mm(i) for i in range(3)]
    nested = pa.array([f.tolist() for f in frames], pa.list_(pa.list_(pa.uint16())))
    fixed = pa.FixedSizeListArray.from_arrays(
        pa.FixedSizeListArray.from_arrays(pa.array(np.stack(frames).reshape(-1)), W), H)
    for col in (nested, fixed):
        got = D.batch_frames(col, (H, W))
        assert all(np.array_equal(g, f) for g, f in zip(got, frames))
    with_hole = pa.array([frames[0].tolist(), None], pa.list_(pa.list_(pa.uint16())))
    got = D.batch_frames(with_hole, (H, W))
    assert np.array_equal(got[0], frames[0]) and got[1] is None
    metres = (frames[1].astype(np.float32) / 1000.0)
    assert np.array_equal(D.to_mm(metres, "m"), frames[1])
    assert D.to_mm(np.array([[np.nan, -1.0, 70.0]], np.float32), "m").tolist() == [[0, 0, 0]]


def test_depth_pairs_with_its_camera_by_name():
    cams = {"front": "observation.images.front", "camera_top": "observation.images.camera_top"}
    assert D.pair_camera("observation.images.front.depth", cams) == "front"
    assert D.pair_camera("observation.depths.camera_top", cams) == "camera_top"
    assert D.pair_camera("observation.depths.top", {"front": "observation.images.front"}) is None


# ---------------------------------------------------------------- the API

def _wait_json(c, url, timeout=60.0):
    deadline = time.monotonic() + timeout
    while True:
        r = c.get(url)
        if r.status_code != 202:
            return r
        assert_schema("VizMediaPending", r.json())
        assert time.monotonic() < deadline, "the depth pack was never made"
        time.sleep(0.1)


@pytest.fixture
def depth_app(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root)
    rt = c.app.state.runtime
    c.ids = {"v3": _register(rt, data_root / "lerobot_v3_depth", "depth", fp="sha256:" + "2" * 64).id,
             "lance": _register(rt, data_root / "lance_depth", "lance_depth", fp="sha256:" + "3" * 64).id}
    return c


def test_the_model_lists_the_depth_streams_paired_with_their_cameras(depth_app):
    model = depth_app.get(f"{API}/datasets/{depth_app.ids['v3']}/viz").json()
    assert_schema("VizDataset", model)
    depth = {s["name"]: s for s in model["streams"] if s["kind"] == "depth"}
    assert set(depth) == {"observation.images.front.depth", "observation.depths.top"}
    front = depth["observation.images.front.depth"]
    assert front["available"] and not front["smart"] and front["unit"] == "mm" and front["rate_hz"] == FPS
    assert front["depth"] == {"width": W, "height": H, "unit": "mm", "pair_camera": "front"}
    assert depth["observation.depths.top"]["depth"]["pair_camera"] is None


@pytest.mark.parametrize("which", ["v3", "lance"])
def test_an_episodes_depth_pack_holds_its_pictures(depth_app, which):
    ds = depth_app.ids[which]
    ep = depth_app.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    streams = {s["key"]: s for s in ep["streams"]}
    key = "observation_images_front_depth"
    assert streams[key]["url"].endswith(f"/episodes/1/streams/{key}.frames") and streams[key]["offset_s"] == 0.0
    r = _wait_json(depth_app, streams[key]["index_url"].replace("/curation/api/v1", API))
    assert r.status_code == 200, r.text
    idx = r.json()
    assert_schema("VizFrameIndex", idx)
    n = DEPTH_LENGTHS[1]
    assert (idx["codec"], idx["count"], idx["width"], idx["height"]) == ("png16", n, W, H)
    assert idx["t"] == pytest.approx([i / FPS for i in range(n)])
    assert idx["depth"]["unit"] == "mm" and idx["depth"]["invalid"] == 0
    first = DEPTH_LENGTHS[0]
    lo, hi = idx["depth"]["lo"], idx["depth"]["hi"]
    valid = np.concatenate([depth_mm(first + i)[depth_mm(first + i) > 0] for i in range(n)])
    assert valid.min() <= lo < hi <= valid.max()
    for k in (0, n - 1):
        a, b = idx["offset"][k], idx["offset"][k] + idx["size"][k] - 1
        part = depth_app.get(streams[key]["url"].replace("/curation/api/v1", API),
                              headers={"Range": f"bytes={a}-{b}"})
        assert part.status_code == 206
        assert np.array_equal(D.decode_png(part.content), depth_mm(first + k))   # the column's own values
    # metres in the parquet / table, millimetres in the pack
    top = streams["observation_depths_top"]
    idx2 = _wait_json(depth_app, top["index_url"].replace("/curation/api/v1", API)).json()
    data = depth_app.get(top["url"].replace("/curation/api/v1", API)).content
    k = 3
    assert np.array_equal(D.decode_png(data[idx2["offset"][k]:idx2["offset"][k] + idx2["size"][k]]), depth_mm(first + k))


def test_unknown_streams_and_the_task_scope(depth_app, data_root):
    ds = depth_app.ids["v3"]
    err = assert_error(depth_app.get(f"{API}/datasets/{ds}/episodes/0/streams/nope.json"), "not_found")
    assert err["error"]["details"]["reason"] == "unknown_stream"
    rt = depth_app.app.state.runtime
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P

    rows = [P.TaskModule(task_id="", module_id=m, selected=False, availability="available") for m in registry.ids()]
    task = rt.repo.create_task(P.TaskCreate(
        name="depth", input_source="local", input_uri=str(data_root / "lerobot_v3_depth"), output_uri="tos://b/out",
        delivery_key="tos://b/out", episode_selector={"mode": "all"}, params={}, modules=rows, dataset_id=ds))
    ep = depth_app.get(f"{API}/tasks/{task.id}/episodes/0/viz").json()
    s = next(x for x in ep["streams"] if x["key"] == "observation_images_front_depth")
    assert s["url"].endswith(f"/tasks/{task.id}/episodes/0/streams/observation_images_front_depth.frames")
    idx = _wait_json(depth_app, s["index_url"].replace("/curation/api/v1", API)).json()
    assert idx["count"] == DEPTH_LENGTHS[0]
