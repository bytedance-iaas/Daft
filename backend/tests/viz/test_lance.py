"""The Lance reader (design doc 19 §4, F14.3): the make_v3 dataset converted the way each lerobot-lancedb
release does reads back the same as the LeRobot original - model, episodes, curves, annotations, camera
bytes - locally and through an S3-compatible endpoint."""
from __future__ import annotations

import json
import shutil

import pytest

from .conftest import assert_error, assert_schema
from .fake_s3 import FakeS3
from .fixtures import LENGTHS
from .lance_fixtures import CAMERA

API = "/curation/api/v1"


def _preflight(kind: str, supported: bool = True) -> dict:
    return {"schema_version": "1.0",
            "format": {"kind": kind, "version": "v3", "supported": supported, "detail": kind},
            "validation": [], "dataset": {"episode_count": 3, "cameras": ["top"], "fps": 10.0,
                                          "robot_type": "so101", "total_frames": 90,
                                          "labels": {"with_task": 3, "without_task": 0}, "profile": None},
            "modules": [], "meta_fingerprint": "sha256:" + "c" * 64, "warnings": []}


def _register(rt, name: str, uri: str, preflight: dict, source: str = "local", manifest: str | None = None) -> str:
    from daemon.repo import protocol as P

    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name=name, source=source, uri=uri, preflight=preflight, meta_fingerprint="sha256:" + name.ljust(64, "0")[:64],
        source_fingerprint={"objects": 0, "bytes": 0, "digest": name}, preflighted_at=1, manifest_path=manifest))
    return ds.id


def _meta_listing(tmp_path, root) -> str:
    """The listing a LeRobot registration keeps of a 0.1-0.2 root: its meta/ files only."""
    path = tmp_path / f"{root.name}.listing.json"
    path.write_text(json.dumps({"objects": [{"key": str(p.relative_to(root)), "size": p.stat().st_size}
                                            for p in (root / "meta").rglob("*") if p.is_file()]}))
    return str(path)


@pytest.fixture
def app(client_for, data_root, tmp_path):
    c = client_for(base_path="/curation", local_data_root=data_root)
    rt = c.app.state.runtime
    c.ids = {
        "v3": _register(rt, "lerobot_v3", str(data_root / "lerobot_v3"), {**_preflight("lerobot"), "format": {"kind": "lerobot", "version": "v3", "supported": True, "detail": "LeRobot"}}),
        "l03": _register(rt, "lance_03", str(data_root / "lance_03"), _preflight("lance")),
        "l03t": _register(rt, "lance_03_tables", str(data_root / "lance_03_tables"), _preflight("lance")),
        # the preflight takes 0.1-0.2 roots (meta/ beside <name>.lance) for LeRobot missing its data
        "l02v": _register(rt, "lance_02_video", str(data_root / "lance_02_video"), _preflight("lerobot", False),
                          manifest=_meta_listing(tmp_path, data_root / "lance_02_video")),
        "l02f": _register(rt, "lance_02_frames", str(data_root / "lance_02_frames"), _preflight("lerobot", False)),
    }
    return c


def _video_bytes(data_root) -> bytes:
    return (data_root / "lerobot_v3" / "videos" / CAMERA / "chunk-000" / "file-000.mp4").read_bytes()


@pytest.mark.parametrize("name,layout", [("l03", "lance-0.3"), ("l03t", "lance-0.3"), ("l02v", "lance-0.2-video")])
def test_a_lance_dataset_reads_like_its_lerobot_original(app, data_root, name, layout):
    ref = app.get(f"{API}/datasets/{app.ids['v3']}/viz").json()
    r = app.get(f"{API}/datasets/{app.ids[name]}/viz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert_schema("VizDataset", body)
    assert body["format"] == {"kind": "lance", "version": "v3", "reader": "lance", "layout": layout}
    assert (body["episode_count"], body["fps"], body["total_frames"]) == (3, 10.0, sum(LENGTHS))
    assert [(c["key"], c["access"], c["codec"]) for c in body["cameras"]] == [("top", "blob", "h264")]
    assert [s["key"] for s in body["streams"]] == [s["key"] for s in ref["streams"]]
    assert [s["key"] for s in body["annotation_sources"]] == [s["key"] for s in ref["annotation_sources"]]
    tables = next(n for n in body["field_tree"] if n["id"] == "lance")["children"]
    frames_table = "frames.lance" if layout == "lance-0.3" else "lift_cup.lance"
    assert any(t["name"] == frames_table and t["detail"]["num_rows"] == sum(LENGTHS) for t in tables)
    # an episode: same clock, curves, annotations and video window as the parquet / mp4 original
    ep_ref = app.get(f"{API}/datasets/{app.ids['v3']}/episodes/1/viz").json()
    ep = app.get(f"{API}/datasets/{app.ids[name]}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    assert (ep["duration_s"], ep["frames"]) == (ep_ref["duration_s"], ep_ref["frames"]) == (2.4, LENGTHS[1])
    cam = ep["cameras"][0]
    assert cam["access"] == "blob" and cam["url"].endswith(f"/datasets/{app.ids[name]}/episodes/1/cameras/top.mp4")
    assert (cam["from_ts"], cam["to_ts"]) == (ep_ref["cameras"][0]["from_ts"], ep_ref["cameras"][0]["to_ts"]) == (3.0, 5.4)
    assert cam["samples_url"] is None and cam["transcode_url"].endswith("top.mp4?transcode=1")
    if layout == "lance-0.3":                       # 0.1-0.2 keeps numeric features only: no language column
        assert ep["annotations"] == ep_ref["annotations"]
    for stream in ("observation_state", "observation_state.gripper"):
        s = app.get(f"{API}/datasets/{app.ids[name]}/episodes/1/series", params={"stream": stream}).json()
        s_ref = app.get(f"{API}/datasets/{app.ids['v3']}/episodes/1/series", params={"stream": stream}).json()
        assert_schema("VizSeries", s)
        assert s["t"] == s_ref["t"] and s["lines"] == s_ref["lines"]
    # the camera: the mp4 out of the blob, by Range
    mp4 = _video_bytes(data_root)
    url = f"{API}/datasets/{app.ids[name]}/episodes/1/cameras/top.mp4"
    part = app.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == mp4[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{len(mp4)}" and part.headers["accept-ranges"] == "bytes"
    assert app.get(url, headers={"Range": "bytes=-64"}).content == mp4[-64:]
    whole = app.get(url)
    assert whole.status_code == 200 and whole.content == mp4
    assert app.get(url, headers={"Range": f"bytes={len(mp4)}-"}).status_code == 416


def test_the_frames_layout_gives_jpeg_frame_packs(app):
    body = app.get(f"{API}/datasets/{app.ids['l02f']}/viz").json()
    assert_schema("VizDataset", body)
    assert body["format"]["layout"] == "lance-0.2-frames"
    assert [(c["key"], c["kind"], c["access"], c["codec"]) for c in body["cameras"]] == [("top", "frames", "frames", "jpeg")]
    ep = app.get(f"{API}/datasets/{app.ids['l02f']}/episodes/2/viz").json()
    cam = ep["cameras"][0]
    assert cam["url"].endswith("/episodes/2/cameras/top.frames") and cam["index_url"].endswith("/episodes/2/cameras/top.json")
    idx = app.get(f"{API}/datasets/{app.ids['l02f']}/episodes/2/cameras/top.json").json()
    assert_schema("VizFrameIndex", idx)
    assert (idx["count"], idx["codec"], idx["width"], idx["height"]) == (LENGTHS[2], "jpeg", 64, 48)
    assert idx["t"][:3] == [0.0, 0.1, 0.2] and idx["bytes"] == sum(idx["size"])
    k = 5
    lo, n = idx["offset"][k], idx["size"][k]
    r = app.get(f"{API}/datasets/{app.ids['l02f']}/episodes/2/cameras/top.frames", headers={"Range": f"bytes={lo}-{lo + n - 1}"})
    assert r.status_code == 206 and r.content[:2] == b"\xff\xd8" and r.content[-2:] == b"\xff\xd9"
    # made once: the second ask is the same file
    assert app.get(f"{API}/datasets/{app.ids['l02f']}/episodes/2/cameras/top.json").json() == idx
    assert_error(app.get(f"{API}/datasets/{app.ids['l02f']}/episodes/2/cameras/top.mp4"), "not_found")


def test_a_tables_only_root_reads_its_metadata_from_meta_lance(app):
    info = app.get(f"{API}/datasets/{app.ids['l03t']}/viz/meta", params={"path": "meta/info.json"})
    assert info.status_code == 200, info.text
    assert json.loads(info.json()["text"])["storage_format"] == "lance"
    items = app.get(f"{API}/datasets/{app.ids['l03t']}/viz/episodes").json()["items"]
    assert [e["index"] for e in items] == [0, 1, 2]


def test_status_lists_and_unsupported_lance(app):
    rt = app.app.state.runtime
    listed = {d["id"]: d for d in app.get(f"{API}/datasets", params={"viz": "true", "page_size": 50}).json()["items"]}
    for name in ("l03", "l03t", "l02v", "l02f"):
        assert listed[app.ids[name]]["viz"] == {"state": "ready", "reason": None}
    # Lance tables without LeRobot metadata (the preflight's lancedb) have no reader
    lancedb = _register(rt, "lancedb", "/nowhere/table", {**_preflight("lancedb", False), "format": {"kind": "lancedb", "version": None, "supported": False, "detail": "x"}})
    body = app.get(f"{API}/datasets/{lancedb}/viz").json()
    assert_schema("VizDataset", body)
    assert body["format"]["reader"] is None and body["warnings"][0]["code"] == "unsupported"
    assert_error(app.get(f"{API}/datasets/{lancedb}/episodes/0/viz"), "validation_failed")
    assert lancedb not in listed


def test_the_mini_player_reads_a_lance_task(app, data_root):
    rt = app.app.state.runtime
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P

    rows = [P.TaskModule(task_id="", module_id=m, selected=False, availability="available") for m in registry.ids()]
    task = rt.repo.create_task(P.TaskCreate(
        name="lance", input_source="local", input_uri=str(data_root / "lance_03"), output_uri="tos://b/out",
        delivery_key="tos://b/out", episode_selector={"mode": "all"}, params={}, modules=rows,
        dataset_id=app.ids["l03"]))
    body = app.get(f"{API}/tasks/{task.id}/viz").json()
    assert_schema("VizDataset", body)
    assert body["format"]["reader"] == "lance" and body["scope"] == "task"
    ep = app.get(f"{API}/tasks/{task.id}/episodes/0/viz").json()
    assert_schema("VizEpisode", ep)
    assert ep["check_clock"] == {"offset_s": 0.0, "fps": 10.0}
    assert ep["cameras"][0]["url"].endswith(f"/tasks/{task.id}/episodes/0/cameras/top.mp4")
    r = app.get(f"{API}/tasks/{task.id}/episodes/0/cameras/top.mp4", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == _video_bytes(data_root)[:10]


def test_lance_on_an_s3_compatible_endpoint(client_for, data_root, tmp_path):
    """TOS is read through its S3-compatible endpoint by range, never copied whole (design doc 19 §4.3):
    the same tables on a minimal local S3 (path style, unsigned), the dataset registered as the public
    bucket's with its listing kept at registration."""
    store = tmp_path / "s3"
    shutil.copytree(data_root / "lance_03_tables", store / "bkt" / "sets" / "lance_03_tables")
    root = store / "bkt" / "sets" / "lance_03_tables"
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps({"objects": [{"key": str(p.relative_to(root)).replace("\\", "/"), "size": p.stat().st_size}
                                               for p in root.rglob("*") if p.is_file()]}))
    with FakeS3(str(store)) as s3:
        c = client_for(base_path="/curation", local_data_root=data_root, viz_lance_s3_endpoint=s3.endpoint)
        rt = c.app.state.runtime
        ds = _register(rt, "lance_s3", "tos://bkt/sets/lance_03_tables", _preflight("lance"), source="public", manifest=str(listing))
        body = c.get(f"{API}/datasets/{ds}/viz").json()
        assert_schema("VizDataset", body)
        assert body["format"]["layout"] == "lance-0.3" and body["cameras"][0]["access"] == "blob"
        s = c.get(f"{API}/datasets/{ds}/episodes/2/series", params={"stream": "observation_state"}).json()
        assert len(s["t"]) == LENGTHS[2]
        mp4 = _video_bytes(data_root)
        r = c.get(f"{API}/datasets/{ds}/episodes/2/cameras/top.mp4", headers={"Range": "bytes=10-29"})
        assert r.status_code == 206 and r.content == mp4[10:30]
        # keys are sets/lance_03_tables/<table>/...: every table was read over S3
        asked = {key.split("/")[2] for method, key, _ in s3.requests if method in ("GET", "LIST") and key.count("/") >= 2}
        assert {"frames.lance", "videos.lance", "meta.lance"} <= asked


def test_tos_tables_open_on_the_buckets_virtual_host(client_for, data_root):
    """Lance's object store sends virtual-hosted requests to the endpoint as given, so TOS's S3 endpoint
    has to name the bucket in its host - with the bare service endpoint every table listed empty and
    read "not found" (TOS refuses path style with 403). Found on the requester's TOS datasets, 2026-10-05."""
    from curation.viz.lance_layout import s3_options
    from daemon.repo.protocol import DEFAULT_OWNER
    from daemon.viz.service import viz_of
    from daemon.viz.source import Access

    c = client_for(base_path="/curation", local_data_root=data_root)
    rt = c.app.state.runtime
    ds = _register(rt, "lance_tos", "tos://galbot/sets/so101-lance", _preflight("lance"), source="public")
    uri, opts = Access(rt, viz_of(rt).dataset_source(ds, DEFAULT_OWNER)).lance_target("frames.lance")
    assert uri == "s3://galbot/sets/so101-lance/frames.lance"
    assert opts["aws_endpoint"] == "https://galbot.tos-s3-cn-beijing.volces.com"
    assert opts["aws_virtual_hosted_style_request"] == "true" and opts["aws_skip_signature"] == "true"
    # an endpoint that already names the bucket is kept; path style (a local S3) is left as it is
    assert s3_options("https://galbot.tos-s3-cn-beijing.ivolces.com/", "cn-beijing", bucket="galbot")["aws_endpoint"] \
        == "https://galbot.tos-s3-cn-beijing.ivolces.com"
    local = s3_options("http://127.0.0.1:9000", "cn-beijing", bucket="bkt", key_id="ak", secret="sk", virtual_hosted=False)
    assert local["aws_endpoint"] == "http://127.0.0.1:9000" and local["aws_allow_http"] == "true"
    assert local["aws_access_key_id"] == "ak" and "aws_skip_signature" not in local



def test_metadata_that_is_a_git_lfs_pointer_is_said_so(client_for, data_root, tmp_path):
    """meta/ parquet that does not parse was a 500; a Git LFS pointer (a HuggingFace clone made
    without LFS, the requester's pusht-lance on 2026-10-05) is named as one."""
    root = tmp_path / "lance_lfs"
    shutil.copytree(data_root / "lance_03", root)
    episodes = sorted((root / "meta" / "episodes").rglob("*.parquet"))[0]
    episodes.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"c" * 64 + b"\nsize 106584\n")
    c = client_for(base_path="/curation", local_data_root=tmp_path)
    ds = _register(c.app.state.runtime, "lance_lfs", str(root), _preflight("lance"))
    for path in (f"{API}/datasets/{ds}/viz", f"{API}/datasets/{ds}/viz/episodes"):
        body = assert_error(c.get(path), "not_found")
        assert "Git LFS 指针文件" in body["error"]["message"] and "hf download" in body["error"]["message"]
    # a parquet that is broken some other way says which file, still not a 500
    episodes.write_bytes(b"PAR1 not really")
    c2 = client_for(base_path="/curation", local_data_root=tmp_path)
    ds2 = _register(c2.app.state.runtime, "lance_broken", str(root), _preflight("lance"))
    body = assert_error(c2.get(f"{API}/datasets/{ds2}/viz"), "not_found")
    assert body["error"]["message"].startswith(f"读不出 meta/episodes/{episodes.parent.name}/{episodes.name}")
