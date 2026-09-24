"""The EEF module on mcap datasets (F5.13, design doc 12).

tools/parity's mini mcap dataset holds the same pictures as the mini LeRobot one - one JPEG message
per time step on ``/observation.images.<camera>`` - so a trajectory.json whose views name the episode's
``.mcap`` file and that topic must give what the LeRobot trajectory gives on the LeRobot dataset: the
same frames in the same order, the same sub-item readings and the same outcomes. On a (fake) TOS the
module reads the episode files from the funnel's source cache, one download each.
"""
from __future__ import annotations

import copy
import json
import os
import shutil

import numpy as np
import pytest

pytest.importorskip("mcap", reason="mcap is needed for the mcap format")
pytest.importorskip("mcap_ros2", reason="mcap-ros2-support decodes the fixture's cdr")

from .pipeline import results, run  # noqa: E402
from .test_eef_check import CAM, EEF, VLM, _files, _truth, fake_vlm  # noqa: E402

TOPIC = "/observation.images.exterior"
EPISODES = "0-3"


@pytest.fixture(scope="session")
def mini_mcap(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_mcap

    return make_mini_mcap(str(tmp_path_factory.mktemp("eef-mcap") / "mini_mcap"))


def _as_mcap(traj: str, out_dir) -> str:
    """The same trajectory.json with every view reading the episode's mcap topic; seeds copied along."""
    doc = json.loads(open(traj, encoding="utf-8").read())
    for entry in doc["samples"]:
        for view in entry["sample"]["views"]:
            m = view["media"]
            m.update(uri=f"episode_{entry['episode_index']}.mcap", topic=TOPIC, clip_start_s=0.0, clip_end_s=None)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copytree(os.path.join(os.path.dirname(traj), "observations_seed"), os.path.join(out_dir, "observations_seed"))
    path = os.path.join(out_dir, "trajectory.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def _centre(bgr: np.ndarray) -> np.ndarray:
    """Centroid of the red block (the fixture draws it pure red on grey)."""
    b, g, r = (bgr[..., i].astype(int) for i in range(3))
    ys, xs = np.nonzero((r > 150) & (g < 90) & (b < 90))
    return np.array([xs.mean(), ys.mean()])


def test_topic_frames_are_numbered_like_the_video(mini_dataset, mini_mcap, tmp_path):
    from curation.extensions.eef_consistency import load
    from curation.extensions.eef_consistency import mcap_media as MM
    from curation.extensions.eef_consistency import observations as O

    lr = _files(tmp_path / "lr", seed_every=15)
    mc = _as_mcap(lr, tmp_path / "mc")
    a = load.load_bundle(lr, lerobot_root=mini_dataset, episodes=[1])
    b = load.load_bundle(mc, lerobot_root=mini_mcap, episodes=[1])
    assert a.ok and b.ok, (a.errors[:2], b.errors[:2])
    video = list(O.view_frames(a.samples[1], CAM, mini_dataset))
    topic = list(O.view_frames(b.samples[1], CAM, mini_mcap))
    assert [f.index for f in topic] == [f.index for f in video] == list(range(len(_truth(1))))
    assert topic[0].bgr().shape == video[0].bgr().shape
    offs = [np.abs(_centre(x.bgr()) - _centre(y.bgr())).max() for x, y in zip(topic, video)]
    assert max(offs) < 1.5                                  # the same picture at every index
    MM.drop(O.media_path(b.samples[1], CAM, mini_mcap))
    assert not MM._videos


def test_what_a_topic_view_must_be(mini_mcap, tmp_path):
    from curation.extensions.eef_consistency import load
    from curation.extensions.eef_consistency import observations as O
    from curation.extensions.eef_consistency import video as V

    mc = _as_mcap(_files(tmp_path / "lr"), tmp_path / "mc")
    doc = json.loads(open(mc, encoding="utf-8").read())
    for change, where in (({"uri": "videos/episode_000000.mp4"}, "uri"), ({"clip_end_s": 5.0}, "clip_end_s"),
                          ({"kind": "image"}, "kind"), ({"topic": "observation.images.exterior"}, "topic")):
        bad = copy.deepcopy(doc)
        bad["samples"][0]["sample"]["views"][0]["media"].update(change)
        path = tmp_path / f"bad-{where}.json"
        path.write_text(json.dumps(bad))
        r = load.load_bundle(str(path), lerobot_root=mini_mcap, episodes=[0])
        assert not r.ok, where
    doc["samples"][0]["sample"]["views"][0]["media"]["topic"] = "/observation.images.side"
    path = tmp_path / "no-topic.json"
    path.write_text(json.dumps(doc))
    r = load.load_bundle(str(path), lerobot_root=mini_mcap, episodes=[0])
    assert r.ok                                              # the file exists; the topic is read later
    with pytest.raises(V.DecodeError, match="observation.images.side is not in the file"):
        next(O.view_frames(r.samples[0], CAM, mini_mcap))


def _eef(cli_input: str, traj: str, rd: str, *extra) -> dict:
    res = run("check", "--modules", EEF, "--input", cli_input, "--run-dir", rd, "--episodes", EPISODES,
              "--param", f"{EEF}.trajectory_json={traj}", *VLM, *extra)
    assert res.rc == 0, res.doc
    return res.doc["modules"][EEF]


def _readings(rd: str) -> dict:
    out = {}
    for ep, r in results(rd, EEF).items():
        d = r["details"]
        out[ep] = (r["verdict"], d["decision"]["outcome"],
                   {k: v["status"] for k, v in d["cameras"][CAM]["subitems"].items()},
                   (d.get("state_motion") or {}).get("status"))
    return out


def test_preflight_and_check_on_mcap_read_as_on_lerobot(mini_dataset, mini_mcap, tmp_path):
    from curation.extensions.eef_consistency import mcap_media as MM

    lr = _files(tmp_path / "lr", seed_every=1)
    mc = _as_mcap(lr, tmp_path / "mc")
    doc = run("preflight", "--input", mini_mcap, "--modules", EEF, "--vlm-backend", "ark",
              "--param", f"{EEF}.trajectory_json={mc}").doc
    (entry,) = doc["modules"]
    assert entry["availability"] == "available", entry
    assert entry["subitems"]["position_2d"]["availability"] == "available"
    with fake_vlm(tmp_path / "tape"):
        on_lerobot = _eef(mini_dataset, lr, str(tmp_path / "run-lr"))
        on_mcap = _eef(mini_mcap, mc, str(tmp_path / "run-mc"))
    assert on_mcap["episodes"]["error"] == on_lerobot["episodes"]["error"] == 0
    got, want = _readings(str(tmp_path / "run-mc")), _readings(str(tmp_path / "run-lr"))
    assert sorted(got) == [0, 1, 2, 3] and got == want
    assert {ep: r[2]["position_2d"] for ep, r in got.items()}[2] == "suspect"     # the 9 px offset is seen
    assert not MM._videos                                    # every episode's topic videos were dropped


def test_lance_is_still_refused(tmp_path):
    pytest.importorskip("lance", reason="pylance is needed for the lance format")
    from parity.fixtures import make_mini_lance

    root = make_mini_lance(str(tmp_path / "lance"))
    (entry,) = run("preflight", "--input", root, "--modules", EEF, "--vlm-backend", "ark",
                   "--param", f"{EEF}.trajectory_json={_files(tmp_path / 'lr')}").doc["modules"]
    assert (entry["availability"], entry["reason_code"]) == ("unsupported", "format_unsupported_by_module")
    assert "LeRobot and mcap" in entry["reason"]


# ---------------------------------------------------------------- on a fake TOS


@pytest.fixture
def tos(cloud, monkeypatch, tmp_path):
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    os.makedirs(tmp_path / "tmp")
    import tempfile

    tempfile.tempdir = None
    yield cloud
    tempfile.tempdir = None


def test_on_tos_the_episode_files_come_from_the_source_cache(cli, tos, mini_mcap, tmp_path, monkeypatch):
    tos.upload_dir(mini_mcap, "src", "ds/mcap")
    monkeypatch.setenv("CURATION_SOURCE_CACHE", str(tmp_path / "cache"))
    lr = _files(tmp_path / "lr", seed_every=1)
    mc = _as_mcap(lr, tmp_path / "mc")
    (entry,) = cli("preflight", "--input", "tos://src/ds/mcap", "--modules", EEF, "--vlm-backend", "ark",
                   "--param", f"{EEF}.trajectory_json={mc}").doc["modules"]
    assert entry["availability"] == "available", entry
    with fake_vlm(tmp_path / "tape"):
        remote = _eef("tos://src/ds/mcap", mc, str(tmp_path / "run-tos"))
        local = _eef(mini_mcap, mc, str(tmp_path / "run-local"))
    assert remote["episodes"]["error"] == 0
    assert _readings(str(tmp_path / "run-tos")) == _readings(str(tmp_path / "run-local"))
    whole = sorted(c[2] for c in tos.calls if c[0] == "get" and c[3] is None and c[2].startswith("ds/mcap/"))
    assert whole == [f"ds/mcap/episode_{i}.mcap" for i in range(4)]        # each episode's file, once
    assert not [c for c in tos.calls if c[0] in ("put", "delete") and c[1] == "src"]
