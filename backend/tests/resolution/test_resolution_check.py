"""Resolution check: synthetic encoded videos for the I/O path, pure judge() for every branch."""
from __future__ import annotations

import numpy as np
import pytest

from curation.extensions.resolution import (VideoProbe, declared_from_lerobot, judge,
                                            probe_video, resolution_check)


def _write_video(path, width=320, height=240, fps=20, n_frames=30):
    """Encode a small mpeg4 clip (mpeg4 ships with every ffmpeg build av bundles)."""
    import av

    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        rng = np.random.default_rng(0)
        for _ in range(n_frames):
            img = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("res") / "cam.mp4"
    _write_video(path, width=320, height=240, fps=20)
    return str(path)


# ---- end to end on real encoded files ----

def test_consistent_video_passes(clip):
    r = resolution_check(clip, {"width": 320, "height": 240, "fps": 20})
    assert r.passed is True and r.score == 1.0
    assert r.detail["mismatches"] == []
    assert r.detail["measured"]["width"] == 320 and r.detail["measured"]["height"] == 240
    assert abs(r.detail["measured"]["fps"] - 20) / 20 <= 0.005


def test_declared_resolution_mismatch_fails(clip):
    r = resolution_check(clip, {"width": 640, "height": 480, "fps": 20})
    assert r.passed is False and r.score == 0.0
    kinds = [m["kind"] for m in r.detail["mismatches"]]
    assert kinds == ["declared_resolution"]
    assert r.detail["mismatches"][0]["actual"] == [320, 240]


def test_declared_fps_mismatch_fails(clip):
    r = resolution_check(clip, {"width": 320, "height": 240, "fps": 30})
    assert r.passed is False
    assert [m["kind"] for m in r.detail["mismatches"]] == ["declared_fps"]


def test_no_declared_spec_is_unknown_not_pass(clip):
    r = resolution_check(clip, None)
    assert r.passed is None and r.score is None
    assert r.detail["reason"] == "no_declared_spec"
    # measured values still reported for the dataset-level aggregation
    assert r.detail["measured"]["width"] == 320


def test_missing_file_is_unknown(tmp_path):
    r = resolution_check(str(tmp_path / "nope.mp4"), {"width": 320, "height": 240, "fps": 20})
    assert r.passed is None and r.detail["reason"] == "file_missing"


def test_garbage_file_is_unknown(tmp_path):
    p = tmp_path / "junk.mp4"
    p.write_bytes(b"\x00" * 4096)
    r = resolution_check(str(p), {"width": 320, "height": 240, "fps": 20})
    assert r.passed is None and r.detail["reason"].startswith(("decode_failed", "no_video_stream",
                                                              "no_frames"))


def test_probe_reports_reality(clip):
    probe = probe_video(clip)
    assert probe.ok and (probe.frame_width, probe.frame_height) == (320, 240)
    assert probe.container_width == 320
    assert abs(probe.measured_fps - 20) / 20 <= 0.005


# ---- pure judge() branches no encoder can conveniently produce ----

def _probe(**kw):
    base = dict(ok=True, container_width=320, container_height=240, container_fps=20.0,
                frame_width=320, frame_height=240, measured_fps=20.0, n_frames=12)
    base.update(kw)
    return VideoProbe(**base)


def test_lying_container_header_fails_even_without_schema():
    r = judge(_probe(container_width=640, container_height=480), None)
    assert r.passed is False
    assert [m["kind"] for m in r.detail["mismatches"]] == ["container_vs_frames"]


def test_fps_within_tolerance_passes():
    # 29.97 measured against a declared 30: container timebase rounding, not a fallback
    r = judge(_probe(measured_fps=29.97, container_fps=29.97), {"width": 320, "height": 240, "fps": 30})
    assert r.passed is True


def test_declared_fps_unmeasurable_is_unknown():
    r = judge(_probe(measured_fps=None, container_fps=None), {"width": 320, "height": 240, "fps": 20})
    assert r.passed is None and r.detail["reason"] == "fps_unmeasurable"


def test_multiple_mismatches_all_reported():
    r = judge(_probe(frame_width=160, frame_height=120, measured_fps=10.0),
              {"width": 320, "height": 240, "fps": 20})
    kinds = {m["kind"] for m in r.detail["mismatches"]}
    assert kinds == {"container_vs_frames", "declared_resolution", "declared_fps"}


# ---- LeRobot schema extraction ----

def test_declared_from_lerobot_reads_shape_and_feature_fps():
    info = {"fps": 30, "features": {"observation.images.cam": {
        "dtype": "video", "shape": [240, 320, 3], "info": {"video.fps": 20.0}}}}
    assert declared_from_lerobot(info, "observation.images.cam") == {
        "width": 320, "height": 240, "fps": 20.0}


def test_declared_from_lerobot_falls_back_to_dataset_fps_and_none_on_unknown_key():
    info = {"fps": 30, "features": {"observation.images.cam": {"dtype": "video",
                                                              "shape": [240, 320, 3]}}}
    assert declared_from_lerobot(info, "observation.images.cam")["fps"] == 30.0
    assert declared_from_lerobot(info, "observation.images.other") is None


def test_declared_from_lerobot_channel_first_via_names():
    info = {"fps": 20, "features": {"cam": {
        "dtype": "video", "shape": [3, 480, 640],
        "names": ["channels", "height", "width"]}}}
    assert declared_from_lerobot(info, "cam") == {"width": 640, "height": 480, "fps": 20.0}


def test_declared_from_lerobot_channel_first_heuristic_without_names():
    info = {"fps": 20, "features": {"cam": {"dtype": "video", "shape": [3, 480, 640]}}}
    assert declared_from_lerobot(info, "cam") == {"width": 640, "height": 480, "fps": 20.0}


def test_declared_from_lerobot_names_as_dict():
    info = {"fps": 20, "features": {"cam": {
        "dtype": "video", "shape": [480, 640, 3],
        "names": {"axes": ["height", "width", "channels"]}}}}
    assert declared_from_lerobot(info, "cam") == {"width": 640, "height": 480, "fps": 20.0}
