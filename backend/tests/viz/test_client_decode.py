"""The browser decoding mcap H.264 / H.265 cameras itself (design doc 19 §3, F14.2): what the Daemon knows of
an access unit (keyframes, parameter sets, B slices, codec strings), the sample pack and index the scan
keeps, the remux made only when a ``<video>`` asks, and the REST surface."""
from __future__ import annotations

import base64
import io

import pytest

from curation.viz import annexb as AB
from curation.viz import mcap_mapping as MM
from curation.viz import mcap_probe as MP
from curation.viz.mcap_episode import remux_samples, scan

from .conftest import assert_error, assert_schema
from .mcap_fixtures import STEP, _annexb, make_abc

pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")

API = "/curation/api/v1"


def _encode(codec: str, n: int, bframes: int) -> list[bytes]:
    import av
    import numpy as np

    enc = av.CodecContext.create("libx264" if codec == "h264" else "libx265", "w")
    enc.width, enc.height, enc.pix_fmt = 64, 48, "yuv420p"
    enc.options = ({"bf": str(bframes), "g": "10"} if codec == "h264"
                   else {"x265-params": f"log-level=none:bframes={bframes}:keyint=10"})
    out = []
    for i in range(n):
        img = np.full((48, 64, 3), 40, np.uint8)
        img[:, (i * 3) % 64] = 255
        f = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        f.pts = i
        out += [bytes(p) for p in enc.encode(f)]
    return out + [bytes(p) for p in enc.encode(None)]


def _decoded(codec: str, samples: list[bytes]) -> int:
    import av

    ctx = av.CodecContext.create("hevc" if codec == "h265" else "h264", "r")
    n = 0
    for s in samples:
        for pkt in ctx.parse(s):
            n += len(ctx.decode(pkt))
    for pkt in ctx.parse(b""):
        n += len(ctx.decode(pkt))
    return n + len(ctx.decode(None))


@pytest.mark.parametrize("codec", ["h264", "h265"])
def test_keyframes_parameter_sets_b_slices_and_the_codec_string(codec):
    samples = _annexb(codec, 30)                       # a keyframe every 10
    assert [i for i, s in enumerate(samples) if AB.is_keyframe(codec, s)] == [0, 10, 20]
    sets: dict = {}
    bits: dict = {}
    for s in samples:
        found = AB.parameter_sets(codec, s)
        sets.update(found)
        if AB.H265_PPS in found:
            pps, extra = AB.pps_extra_bits(found[AB.H265_PPS])
            bits[pps] = extra
    assert AB.has_all(codec, sets) and AB.joined(codec, sets).startswith(b"\x00\x00\x00\x01")
    cs = AB.codec_string(codec, sets)
    assert cs.startswith("avc1.64") if codec == "h264" else cs.startswith("hvc1.1.6.L")
    assert not any(AB.has_b_slice(codec, s, bits) for s in samples)
    with_b = _encode(codec, 24, bframes=2)
    b_bits: dict = {}
    for s in with_b:
        if AB.H265_PPS in AB.parameter_sets(codec, s):
            pps, extra = AB.pps_extra_bits(AB.parameter_sets(codec, s)[AB.H265_PPS])
            b_bits[pps] = extra
    assert any(AB.has_b_slice(codec, s, b_bits) for s in with_b)


@pytest.mark.parametrize("codec", ["h264", "h265"])
def test_a_keyframe_without_its_parameter_sets_decodes_with_the_config_in_front(codec):
    """A recording may send the parameter sets once: the browser puts the index's config in front of the
    keyframe it starts at, which is what lets it start at any keyframe."""
    samples = _annexb(codec, 30)
    config = AB.joined(codec, AB.parameter_sets(codec, samples[0]))

    def bare(s: bytes) -> bytes:
        keep = [s[a:b] for a, b in AB.nal_units(s)
                if AB.nal_type(codec, s, a) not in (AB.H264_SPS, AB.H264_PPS, AB.H265_VPS, AB.H265_SPS, AB.H265_PPS)]
        return b"".join(b"\x00\x00\x00\x01" + u for u in keep)

    later = [bare(s) for s in samples[10:]]
    assert not AB.parameter_sets(codec, later[0])
    try:                                             # without them nothing decodes (or the decoder refuses)
        alone = _decoded(codec, later)
    except Exception:  # noqa: BLE001
        alone = 0
    assert alone == 0
    assert _decoded(codec, [config + later[0]] + later[1:]) == 20


def test_the_scan_keeps_a_sample_pack_indexed_from_the_first_keyframe_and_remuxes_later(tmp_path):
    import av

    root = make_abc(str(tmp_path / "abc"), n=20, wrist_skip=3)        # the wrist's first keyframe is its 8th frame
    with open(f"{root}/episode_0.mcap", "rb") as fh:
        m, _ = MM.draft(MP.probe(fh, "episode_0.mcap"))
    out = tmp_path / "out"
    with open(f"{root}/episode_0.mcap", "rb") as fh:
        doc = scan(fh, m, out, client_decode=True)
    wrist, top = doc["cameras"]["camera_wrist"], doc["cameras"]["camera_top"]
    for cam in (wrist, top):
        assert cam["samples"] and not cam["mp4"] and cam["kf"][0] and cam["codec_string"]
        assert (out / f"{cam['key']}.annexb").is_file() and not (out / f"{cam['key']}.mp4").exists()
    assert (wrist["skipped"], len(wrist["t"]), wrist["width"], wrist["height"]) == (7, 13, 64, 48)
    assert wrist["offset_s"] == wrist["t"][0] == round(7 * STEP / 1e9, 6)
    assert wrist["codec_string"].startswith("avc1.") and top["codec_string"].startswith("hvc1.")
    assert AB.has_all("h264", AB.parameter_sets("h264", base64.b64decode(wrist["config"])))
    assert any(w["code"] == "leading_frames" and "camera/wrist" in w["message"] for w in doc["warnings"])
    pack = (out / "camera_wrist.annexb").read_bytes()
    first = pack[wrist["offset"][0]:wrist["offset"][0] + wrist["size"][0]]
    assert AB.is_keyframe("h264", first) and len(pack) == wrist["bytes"]
    # the <video> fallback: remuxed from the first keyframe with the config in front, on the index's clock
    assert remux_samples(out, {**wrist, "key": "camera_wrist"}) == {"mp4": True, "b_frames": False}
    with av.open(str(out / "camera_wrist.mp4")) as inp:
        frames = list(inp.decode(inp.streams.video[0]))
    assert len(frames) == 13 and frames[0].key_frame and frames[0].time == 0
    # client decode off: the remux happens in the scan, as before
    with open(f"{root}/episode_0.mcap", "rb") as fh:
        doc2 = scan(fh, m, tmp_path / "eager")
    assert doc2["cameras"]["camera_wrist"]["mp4"] and "samples" not in doc2["cameras"]["camera_wrist"]


def _registered(client_for, tmp_path, **settings):
    from daemon.repo import protocol as P

    data = tmp_path / "inputs"
    make_abc(str(data / "abc"), n=20)
    c = client_for(base_path="/curation", local_data_root=data, **settings)
    rt = c.app.state.runtime
    pf = {"schema_version": "1.0", "format": {"kind": "mcap", "version": None, "supported": True, "detail": "mcap"},
          "validation": [], "dataset": None, "modules": [], "meta_fingerprint": "sha256:" + "c" * 64, "warnings": []}
    ds, _ = rt.repo.register_dataset(P.Dataset(
        id="", name="abc", source="local", uri=str(data / "abc"), preflight=pf, meta_fingerprint="sha256:" + "c" * 64,
        source_fingerprint={"objects": 0, "bytes": 0, "digest": "d"}, preflighted_at=1))
    probe = c.post(f"{API}/viz/mcap-probe", json={"input": {"dataset_id": ds.id}}).json()
    assert c.put(f"{API}/datasets/{ds.id}/mapping", json={"mapping": probe["draft"]}).status_code == 200
    return c, ds.id


def test_the_episode_offers_the_sample_pack_and_the_mp4_is_made_when_asked(client_for, tmp_path):
    import av

    c, ds = _registered(client_for, tmp_path)
    ep = c.get(f"{API}/datasets/{ds}/episodes/0/viz").json()
    assert_schema("VizEpisode", ep)
    cams = {x["key"]: x for x in ep["cameras"]}
    wrist = cams["camera_wrist"]
    assert wrist["access"] == "remux" and wrist["url"].endswith("/cameras/camera_wrist.mp4")
    assert wrist["samples_url"].endswith("/cameras/camera_wrist.frames") and wrist["index_url"].endswith("/cameras/camera_wrist.json")
    assert cams["camera_top"]["samples_url"].endswith("/cameras/camera_top.frames")
    idx = c.get(wrist["index_url"]).json()
    assert_schema("VizFrameIndex", idx)
    assert (idx["codec"], idx["count"], idx["key"][:3], idx["width"]) == ("h264", 20, [True, False, False], 64)
    assert idx["codec_string"].startswith("avc1.") and base64.b64decode(idx["config"]).startswith(b"\x00\x00\x00\x01")
    k = 10
    r = c.get(wrist["samples_url"], headers={"Range": f"bytes={idx['offset'][k]}-{idx['offset'][k] + idx['size'][k] - 1}"})
    assert r.status_code == 206 and len(r.content) == idx["size"][k] and AB.is_keyframe("h264", r.content)
    mp4 = c.get(wrist["url"])
    assert mp4.status_code == 200 and mp4.content[4:8] == b"ftyp"
    with av.open(io.BytesIO(mp4.content)) as inp:
        assert len(list(inp.decode(inp.streams.video[0]))) == 20
    assert c.get(wrist["url"], headers={"Range": "bytes=0-15"}).content == mp4.content[:16]
    # the cache dropped the sample pack (it evicts file by file): the episode is read again, not left broken
    from daemon.viz.service import viz_of

    packs = list(viz_of(c.app.state.runtime).disk.root.rglob("camera_wrist.annexb"))
    assert len(packs) == 1
    packs[0].unlink()
    again = c.get(wrist["index_url"])
    assert again.status_code == 200 and again.json()["count"] == 20 and packs[0].is_file()


def test_with_client_decode_off_every_camera_is_remuxed_up_front(client_for, tmp_path):
    c, ds = _registered(client_for, tmp_path, viz_client_decode=False)
    ep = c.get(f"{API}/datasets/{ds}/episodes/0/viz").json()
    assert_schema("VizEpisode", ep)
    assert all(x["samples_url"] is None and x["index_url"] is None for x in ep["cameras"])
    assert_error(c.get(f"{API}/datasets/{ds}/episodes/0/cameras/camera_wrist.json"), "not_found")
    assert c.get(f"{API}/datasets/{ds}/episodes/0/cameras/camera_wrist.mp4").status_code == 200
