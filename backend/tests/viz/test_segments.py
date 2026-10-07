"""Episode slices and faststart copies (design doc 21 §4, D69, F15.3): the cut, transcodes from a slice
read by range, and CURATOR_VIZ_SEGMENT (slices and moov-at-end copies served by the Daemon)."""
from __future__ import annotations

import io
import os
import shutil
import time

import numpy as np
import pytest

from curation.streams.rangefile import RangeFile
from curation.viz.segment import cut, keyframe_before, moov_at_end

from .conftest import assert_error, assert_schema
from .fixtures import FPS, LENGTHS, W
from .test_fixes import _column, _frames, _register, _wait

API = "/curation/api/v1"


def _times(path_or_bytes):
    import av

    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    with av.open(src) as inp:
        vs = inp.streams.video[0]
        frames = list(inp.decode(vs))
        return [float(f.pts * vs.time_base) for f in frames], frames


def _reader(path):
    fh = open(path, "rb")

    def read(start, n):
        fh.seek(start)
        return fh.read(n)

    return read, os.path.getsize(path)


# ---------------------------------------------------------------- the cut

def test_each_episode_cut_from_the_shared_file_holds_its_frames(data_root, tmp_path):
    src = str(data_root / "lerobot_v3/videos/observation.images.top/chunk-000/file-000.mp4")
    read, size = _reader(src)
    for ep, n in enumerate(LENGTHS):
        frm, to = sum(LENGTHS[:ep]) / FPS, sum(LENGTHS[:ep + 1]) / FPS
        k = keyframe_before(RangeFile(read, size), frm)
        seg = cut(RangeFile(read, size), str(tmp_path / f"ep{ep}.mp4"), frm, to)
        assert seg.start_s == pytest.approx(k) and seg.start_s <= frm + 1e-6 and not seg.b_frames
        times, frames = _times(str(tmp_path / f"ep{ep}.mp4"))
        inside = [i for i, t in enumerate(times) if frm - seg.start_s - 1e-6 <= t < to - seg.start_s - 1e-6]
        assert len(inside) == n                                    # every frame of the episode, at its time
        first = sum(LENGTHS[:ep])
        assert [_column(frames[i]) for i in inside] == [(3 * (first + j)) % W for j in range(n)]
        assert not moov_at_end(*_reader(str(tmp_path / f"ep{ep}.mp4")))     # moov in front


def test_a_whole_file_copy_puts_its_moov_in_front(data_root, tmp_path):
    src = str(data_root / "lerobot_v2/videos/chunk-000/observation.images.front/episode_000000.mp4")
    read, size = _reader(src)
    assert moov_at_end(read, size)                                  # PyAV writes it last, as LeRobot v2 does
    seg = cut(src, str(tmp_path / "w.mp4"), None, None)
    assert seg.start_s == 0.0 and seg.end_s is None and seg.packets == LENGTHS[0]
    r2, s2 = _reader(str(tmp_path / "w.mp4"))
    assert not moov_at_end(r2, s2)
    assert len(_times(str(tmp_path / "w.mp4"))[1]) == LENGTHS[0]


def _big_shared(tmp_path) -> tuple[str, list[int]]:
    """Three episodes of 120 noisy 320x240 frames in one H.264 file (a few MB: reads can be measured)."""
    import av

    lengths = [120, 120, 120]
    path = str(tmp_path / "big.mp4")
    rng = np.random.default_rng(0)
    with av.open(path, "w") as out:
        s = out.add_stream("h264", rate=FPS)
        s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
        s.options = {"bf": "0", "g": "10", "crf": "10"}
        for i in range(sum(lengths)):
            img = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
            for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(p)
        for p in s.encode():
            out.mux(p)
    return path, lengths


def test_a_slice_reads_the_moov_and_its_episode_only(tmp_path):
    path, lengths = _big_shared(tmp_path)
    read, size = _reader(path)
    rf = RangeFile(read, size)
    frm, to = lengths[0] / FPS, (lengths[0] + lengths[1]) / FPS
    seg = cut(rf, str(tmp_path / "mid.mp4"), frm, to)
    assert seg.packets >= lengths[1]
    assert rf.stats.bytes < 0.5 * size, (rf.stats.bytes, size)     # about a third of the file, not all of it


def test_a_slice_is_read_in_large_blocks(tmp_path):
    # libav reads 32 KiB at a time in order: the Daemon's ranged view fetches a MiB at a time, so a cut
    # costs its episode's bytes plus at most a block at either end and the file's head (and the moov,
    # which PyAV leaves in the tail), a GET per MiB (design doc 21 §4.6)
    from curation.viz.segment import CUT_BLOCK

    path, lengths = _big_shared(tmp_path)
    read, size = _reader(path)
    frm, to = lengths[0] / FPS, (lengths[0] + lengths[1]) / FPS
    small, big = RangeFile(read, size), RangeFile(read, size, block=CUT_BLOCK)
    seg = cut(small, str(tmp_path / "a.mp4"), frm, to)
    assert cut(big, str(tmp_path / "b.mp4"), frm, to) == seg
    assert big.stats.bytes <= seg.bytes + 3 * CUT_BLOCK + (1 << 16), (big.stats.bytes, seg.bytes)
    assert big.stats.gets <= seg.bytes // CUT_BLOCK + 4 < small.stats.gets / 5, (big.stats.gets, small.stats.gets)


# ---------------------------------------------------------------- transcodes from a slice

def test_a_tos_transcode_cuts_the_episode_instead_of_downloading_the_file(client_for, data_root, tmp_path, monkeypatch):
    from daemon.viz.service import viz_of

    c = client_for(base_path="/curation", local_data_root=data_root)
    rt = c.app.state.runtime
    ds = _register(rt, data_root / "lerobot_v3", "v3", fp="sha256:" + "c" * 64)
    svc = viz_of(rt)
    src = svc.dataset_source(ds.id, rt.repo.get_dataset(ds.id, owner=None).owner_id)
    root = data_root / "lerobot_v3"
    svc.lerobot.meta(src)                                          # the metadata read while it is local
    src.source = "tos"                                             # then a TOS dataset: no whole-file download
    reads = []

    def opener(_src, rel):
        import contextlib

        @contextlib.contextmanager
        def open_(small=False):
            r, size = _reader(str(root / rel))
            rf = RangeFile(r, size)
            reads.append(rf)
            yield rf

        return open_

    monkeypatch.setattr(svc.segments, "opener", opener)
    monkeypatch.setattr(svc, "copy_lock", lambda *_: pytest.fail("the whole file was fetched"))
    job = svc.lerobot_transcode(src, 1, "top")
    deadline = time.monotonic() + 60
    while job.state == "pending" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert job.state == "done", job.message
    times, frames = _times(str(job.out))
    assert len(frames) == LENGTHS[1] and _column(frames[0]) == (3 * LENGTHS[0]) % W
    assert reads and all(isinstance(r, RangeFile) for r in reads)


# ---------------------------------------------------------------- CURATOR_VIZ_SEGMENT

@pytest.fixture
def seg_app(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root, viz_segment=True)
    rt = c.app.state.runtime
    c.ids = {"v2": _register(rt, data_root / "lerobot_v2", "v2", version="v2", fp="sha256:" + "d" * 64).id,
             "v3": _register(rt, data_root / "lerobot_v3", "v3", fp="sha256:" + "e" * 64).id,
             "lance": _register(rt, data_root / "lance_03", "lance", fp="sha256:" + "f" * 64).id}
    return c


def test_with_the_switch_a_v3_episode_plays_its_slice(seg_app):
    ds = seg_app.ids["v3"]
    ep = seg_app.get(f"{API}/datasets/{ds}/episodes/1/viz").json()
    assert_schema("VizEpisode", ep)
    cam = next(x for x in ep["cameras"] if x["key"] == "top")
    assert cam["access"] == "remux" and cam["url"].endswith("top.mp4?segment=1") and cam["transcode_url"]
    r = seg_app.get(cam["url"].replace("/curation/api/v1", API))
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    times, frames = _times(r.content)
    k = next(i for i, t in enumerate(times) if t >= cam["from_ts"] - 1e-6)
    assert _column(frames[k]) == (3 * LENGTHS[0]) % W               # from_ts is the episode's start in the slice
    assert cam["to_ts"] - cam["from_ts"] == pytest.approx(LENGTHS[1] / FPS)
    assert r.headers.get("etag") and "immutable" in r.headers["cache-control"]


def test_with_the_switch_a_moov_at_end_file_is_put_in_front(seg_app):
    ds = seg_app.ids["v2"]
    ep = seg_app.get(f"{API}/datasets/{ds}/episodes/0/viz").json()
    front = next(x for x in ep["cameras"] if x["key"] == "front")
    assert front["access"] == "remux" and front["from_ts"] is None and front["url"].endswith("?segment=1")
    data = seg_app.get(front["url"].replace("/curation/api/v1", API)).content
    assert not moov_at_end(lambda s, n: data[s:s + n], len(data))
    assert len(_times(data)[1]) == LENGTHS[0]
    wrist = next(x for x in ep["cameras"] if x["key"] == "wrist")
    assert wrist["access"] == "transcode"                          # transcodes are left as they are


def test_with_the_switch_a_lance_blob_episode_plays_its_slice(seg_app):
    ds = seg_app.ids["lance"]
    model = seg_app.get(f"{API}/datasets/{ds}/viz").json()
    key = model["cameras"][0]["key"]
    ep = seg_app.get(f"{API}/datasets/{ds}/episodes/2/viz").json()
    cam = next(x for x in ep["cameras"] if x["key"] == key)
    assert cam["access"] == "remux" and cam["url"].endswith("?segment=1")
    r = seg_app.get(cam["url"].replace("/curation/api/v1", API))
    assert r.status_code == 200
    times, frames = _times(r.content)
    k = next(i for i, t in enumerate(times) if t >= cam["from_ts"] - 1e-6)
    assert _column(frames[k]) == (3 * (LENGTHS[0] + LENGTHS[1])) % W


def test_without_the_switch_slices_are_not_served(client_for, data_root):
    c = client_for(base_path="/curation", local_data_root=data_root)
    ds = _register(c.app.state.runtime, data_root / "lerobot_v3", "v3", fp="sha256:" + "1" * 64)
    cam = next(x for x in c.get(f"{API}/datasets/{ds.id}/episodes/1/viz").json()["cameras"] if x["key"] == "top")
    assert cam["access"] == "local" and "segment" not in cam["url"]
    err = assert_error(c.get(f"{API}/datasets/{ds.id}/episodes/1/cameras/top.mp4?segment=1"), "not_found")
    assert err["error"]["details"]["reason"] == "segment_disabled"
