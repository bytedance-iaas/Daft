"""Design doc 21 §2 (F15.1): the small fixes - transcodes of a shared v3 file start at the episode, a silent
transcode is killed, shutdown kills what still runs, stale ``.part`` files go, a deleted registration's
products go, raw images and point clouds are no cameras, ``*`` / indexed field paths count their numbers,
depth is no curve, nested numeric columns flatten, an mcap hit is a use."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import stat
import time

import numpy as np
import pytest

from curation.viz import mcap_mapping as MM
from curation.viz import mcap_probe as MP
from curation.viz.mcap_episode import scan

from .conftest import assert_schema
from .fixtures import FPS, LENGTHS, W

API = "/curation/api/v1"


def _preflight(version: str, fp: str) -> dict:
    return {"schema_version": "1.0",
            "format": {"kind": "lerobot", "version": version, "supported": True, "detail": "LeRobot"},
            "validation": [], "dataset": {"episode_count": 3, "cameras": ["top"], "fps": 10.0,
                                          "robot_type": "so101", "total_frames": 90,
                                          "labels": {"with_task": 3, "without_task": 0}, "profile": None},
            "modules": [], "meta_fingerprint": fp, "warnings": []}


def _register(rt, root, name, version="v3", fp="sha256:" + "7" * 64):
    from daemon.repo import protocol as P

    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name=name, source="local", uri=str(root), preflight=_preflight(version, fp), meta_fingerprint=fp,
        source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"}, preflighted_at=1))
    return ds


def _wait(c, url, timeout=60.0):
    deadline = time.monotonic() + timeout
    while True:
        r = c.get(url)
        if r.status_code != 202 or time.monotonic() > deadline:
            return r
        time.sleep(0.2)


def _column(frame) -> int:
    """The white column of a fixture frame (frame i of a file has it at (3 i) mod W)."""
    img = frame.to_ndarray(format="rgb24").astype(int)
    return int(np.argmax(img[:, :, 0].mean(axis=0)))


def _frames(data: bytes):
    import av

    with av.open(io.BytesIO(data)) as inp:
        return list(inp.decode(inp.streams.video[0]))


@pytest.fixture
def v3(client_for, data_root, tmp_path):
    """The v3 dataset twice: as it is (H.264, played directly) and with its camera declared MPEG-4 (so the
    platform transcodes it; the bytes stay H.264, which the transcoder reads all the same)."""
    inputs = tmp_path / "inputs"
    shutil.copytree(data_root / "lerobot_v3", inputs / "v3")
    shutil.copytree(data_root / "lerobot_v3", inputs / "v3_mpeg4")
    info_path = inputs / "v3_mpeg4" / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["features"]["observation.images.top"]["info"]["video.codec"] = "mpeg4"
    info_path.write_text(json.dumps(info))
    c = client_for(base_path="/curation", local_data_root=inputs)
    rt = c.app.state.runtime
    c.ids = {"v3": _register(rt, inputs / "v3", "v3").id,
             "mpeg4": _register(rt, inputs / "v3_mpeg4", "v3_mpeg4", fp="sha256:" + "8" * 64).id}
    c.inputs = inputs
    return c


# ---------------------------------------------------------------- 1. transcodes of a shared file

def test_a_transcoded_camera_of_a_shared_v3_file_starts_at_its_episode(v3):
    ds = v3.ids["mpeg4"]
    ep = v3.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    cam = next(c for c in ep["cameras"] if c["key"] == "top")
    # times in what url serves: the transcode's 0 is the episode's start (design doc 21 §4.5)
    assert cam["access"] == "transcode" and cam["from_ts"] == 0.0
    assert cam["to_ts"] == pytest.approx(LENGTHS[1] / FPS)
    r = _wait(v3, f"{API}/datasets/{ds}/episodes/1/cameras/top.mp4")
    assert r.status_code == 200, r.text
    frames = _frames(r.content)
    assert len(frames) == LENGTHS[1]
    # its first frame is the episode's first: frame LENGTHS[0] of the shared file
    assert _column(frames[0]) == (3 * LENGTHS[0]) % W
    assert _column(frames[5]) == (3 * (LENGTHS[0] + 5)) % W


def test_the_transcode_fallback_of_a_direct_v3_camera_starts_at_its_episode(v3):
    ds = v3.ids["v3"]
    cam = next(c for c in v3.get(f"{API}/datasets/{ds}/episodes/2/viz").json()["cameras"] if c["key"] == "top")
    start = (LENGTHS[0] + LENGTHS[1]) / FPS
    assert cam["access"] == "local" and cam["from_ts"] == pytest.approx(start) and cam["transcode_url"]
    r = _wait(v3, cam["transcode_url"].replace("/curation/api/v1", API))
    frames = _frames(r.content)
    # played instead of url, it runs from 0 to to_ts - from_ts (the player binds it so)
    assert len(frames) == LENGTHS[2]
    assert _column(frames[0]) == (3 * (LENGTHS[0] + LENGTHS[1])) % W


# ---------------------------------------------------------------- 3. the transcoder's child

def _hang_script(tmp_path) -> str:
    path = tmp_path / "hang.sh"
    path.write_text("#!/bin/sh\nexec sleep 60\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _settled(job, timeout=10.0):
    deadline = time.monotonic() + timeout
    while job.state == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
    return job


def test_a_transcode_that_hangs_without_a_word_is_killed(tmp_path):
    from daemon.viz.media import DiskCache, Transcoder

    cache = DiskCache(tmp_path / "cache", 1 << 30)
    t = Transcoder(cache, 1, python=_hang_script(tmp_path), timeout_s=0.5)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    started = time.monotonic()
    job = _settled(t.ensure("k", tmp_path / "cache" / "out.mp4", lambda: src))
    assert job.state == "failed" and "转码超时" in job.message
    assert time.monotonic() - started < 8
    t.shutdown()


def test_shutdown_kills_a_running_transcode(tmp_path):
    from daemon.viz.media import DiskCache, Transcoder

    cache = DiskCache(tmp_path / "cache", 1 << 30)
    t = Transcoder(cache, 1, python=_hang_script(tmp_path), timeout_s=60)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    job = t.ensure("k", tmp_path / "cache" / "out.mp4", lambda: src)
    deadline = time.monotonic() + 10
    while not t._procs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert t._procs
    t.shutdown()
    assert _settled(job).state == "failed"


# ---------------------------------------------------------------- 4. stale .part files

def test_stale_part_files_are_cleared_on_the_first_scan(tmp_path):
    from daemon.viz.media import STALE_PART_S, DiskCache

    root = tmp_path / "cache"
    (root / "transcode" / "x").mkdir(parents=True)
    old = root / "transcode" / "x" / "a.mp4.part"
    new = root / "transcode" / "x" / "b.mp4.part"
    old.write_bytes(b"1")
    new.write_bytes(b"2")
    then = time.time() - STALE_PART_S - 60
    os.utime(old, (then, then))
    cache = DiskCache(root, 1 << 30)
    product = root / "transcode" / "x" / "c.mp4"
    product.write_bytes(b"3")
    cache.added(product)
    assert not old.exists() and new.exists() and product.exists()


# ---------------------------------------------------------------- 12. a deleted registration

def test_deleting_a_registration_drops_its_products(v3):
    from daemon.viz.media import digest
    from daemon.viz.service import viz_of

    ds = v3.ids["mpeg4"]
    assert _wait(v3, f"{API}/datasets/{ds}/episodes/0/cameras/top.mp4").status_code == 200
    rt = v3.app.state.runtime
    svc = viz_of(rt)
    reg = rt.repo.get_dataset(ds, owner=None)
    src = svc.dataset_source(ds, reg.owner_id)
    products = svc.disk.root / "transcode" / digest(src.scope, src.id, src.fingerprint)
    assert any(products.rglob("*.mp4"))
    r = v3.delete(f"{API}/datasets/{ds}", headers={"content-type": "application/json"})
    assert r.status_code == 204, r.text
    assert not products.exists()


# ---------------------------------------------------------------- 8. raw images and point clouds

@pytest.fixture(scope="module")
def rgbd(tmp_path_factory):
    pytest.importorskip("mcap_protobuf")
    pytest.importorskip("foxglove_schemas_protobuf")
    from .mcap_fixtures import make_rgbd

    return make_rgbd(str(tmp_path_factory.mktemp("rgbd") / "rgbd"))


def test_raw_images_and_point_clouds_are_no_cameras(rgbd, tmp_path):
    with open(f"{rgbd}/episode_0.mcap", "rb") as fh:
        pr = MP.probe(fh, "episode_0.mcap")
    color, cloud = pr.topics["/raw-color"], pr.topics["/cloud"]
    assert (color.kind, color.codec, color.width, color.height) == ("camera", "raw", 64, 48)
    assert color.image["encoding"] == "rgb8"
    assert cloud.kind == "other"
    m, _ = MM.draft(pr)
    assert "/cloud" in m["ignore"] and "/cloud" not in {c["topic"] for c in m["cameras"]}
    # a raw image mapped as a camera: no bytes sniffed for a codec, the camera says why
    mapping = {**MM._empty("t", None), "cameras": [{"topic": "/raw-color", "name": "raw"}],
               "series": [{"topic": "/arm-action", "name": "arm", "role": "action"}]}
    with open(f"{rgbd}/episode_0.mcap", "rb") as fh:
        doc = scan(fh, mapping, tmp_path / "out")
    assert doc["cameras"]["raw-color"]["error"] == "原始图像（raw）本期不支持"
    assert not list((tmp_path / "out").glob("raw-color.*"))


# ---------------------------------------------------------------- 9. * and indexed field paths

def test_star_and_indexed_field_paths_count_their_numbers(client_for, tmp_path):
    pytest.importorskip("mcap_protobuf")
    from .mcap_fixtures import make_umi
    from .test_mcap import _register as register_mcap

    root = make_umi(str(tmp_path / "inputs" / "umi"), episodes=1)
    c = client_for(base_path="/curation", local_data_root=tmp_path / "inputs")
    ds = register_mcap(c.app.state.runtime, root, "umi")
    draft = c.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds.id}}).json()["draft"]
    pose = next(s for s in draft["series"] if s["topic"] == "/robot0/vio/eef_pose")
    pose.update(fields=["pose.position.*", "pose.orientation"], labels=None, transforms={})
    pose.pop("labels")
    assert c.put(f"{API}/datasets/{ds.id}/mapping", json={"mapping": draft}).status_code == 200
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    stream = next(s for s in model["streams"] if s["sources"][0] == "/robot0/vio/eef_pose")
    assert [ln["name"] for ln in stream["lines"]][:3] == ["pose.position.*.0", "pose.position.*.1", "pose.position.*.2"]
    assert len(stream["lines"]) == 7
    series = c.get(f"{API}/datasets/{ds.id}/episodes/0/series", params={"stream": stream["key"]}).json()
    x = series["lines"][0]["values"]
    assert x[:3] == [0.0, 0.1, 0.2]
    assert all(v is not None for v in series["lines"][2]["values"])


# ---------------------------------------------------------------- 10, 7. depth is no curve, listed once

def test_depth_columns_are_no_curves_and_are_listed_once(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root)
    ds = _register(c.app.state.runtime, data_root / "lerobot_v3_depth", "depth", fp="sha256:" + "9" * 64)
    model = c.get(f"{API}/datasets/{ds.id}/viz").json()
    assert_schema("VizDataset", model)
    curves = [s for s in model["streams"] if s["kind"] == "series"]
    assert not any("depth" in src for s in curves for src in s["sources"])
    names = []

    def walk(nodes):
        for n in nodes:
            names.append(n["name"])
            walk(n.get("children") or [])

    walk(model["field_tree"])
    assert names.count("observation.images.front.depth") == 1
    assert names.count("observation.depths.top") == 1


# ---------------------------------------------------------------- 11. nested numeric columns

def test_nested_numeric_columns_flatten_row_by_row():
    import pyarrow as pa

    from curation.viz.series import column_values

    col = pa.chunked_array([pa.array([[[1.0, 2.0], [3.0, 4.0]], None, [[5.0, 6.0], [7.0, 8.0]]],
                                     pa.list_(pa.list_(pa.float64())))])
    out = column_values(col)
    assert out.shape == (3, 4)
    assert out[0].tolist() == [1.0, 2.0, 3.0, 4.0] and np.isnan(out[1]).all() and out[2, 3] == 8.0


# ---------------------------------------------------------------- 5. an mcap hit is a use

def test_an_mcap_cache_hit_refreshes_its_products(client_for, tmp_path):
    pytest.importorskip("mcap_protobuf")
    from .mcap_fixtures import make_umi
    from .test_mcap import _register as register_mcap

    root = make_umi(str(tmp_path / "inputs" / "umi"), episodes=1)
    c = client_for(base_path="/curation", local_data_root=tmp_path / "inputs")
    ds = register_mcap(c.app.state.runtime, root, "umi")
    draft = c.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds.id}}).json()["draft"]
    assert c.put(f"{API}/datasets/{ds.id}/mapping", json={"mapping": draft}).status_code == 200
    assert c.get(f"{API}/datasets/{ds.id}/episodes/0/viz").status_code == 200
    from daemon.viz.service import viz_of

    svc = viz_of(c.app.state.runtime)
    products = list(svc.disk.root.joinpath("mcap").rglob("*"))
    files = [p for p in products if p.is_file()]
    assert files
    then = time.time() - 86400
    for p in files:
        os.utime(p, (then, then))
    assert c.get(f"{API}/datasets/{ds.id}/episodes/0/viz").status_code == 200
    assert all(p.stat().st_mtime > then + 3600 for p in files if p.name in ("episode.json", "series.npz") or p.suffix == ".frames")
