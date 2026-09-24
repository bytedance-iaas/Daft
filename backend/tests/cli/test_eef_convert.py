"""tools/eef_convert.py: a trajectory.json between a LeRobot dataset and its mcap twin (F5.13).

tools/parity's mini LeRobot and mini mcap datasets hold the same frames: the converted file is the
one the mcap tests write by hand, the check finds nothing on it and catches a wrong topic or frame
count, and converting back gives the LeRobot views again - on the DEMO dataset2 (LeRobot v3, two
cameras in concatenated videos) too.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mcap", reason="mcap is needed for the mcap format")

import eef_convert as X  # noqa: E402  (tools/ is on sys.path, tests/conftest.py)

from ..eef import demo_data  # noqa: E402
from .test_eef_check import _files  # noqa: E402
from .test_eef_mcap import _as_mcap  # noqa: E402


@pytest.fixture(scope="module")
def mini_mcap(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_mcap

    return make_mini_mcap(str(tmp_path_factory.mktemp("convert") / "mini_mcap"))


def _media(doc: dict) -> dict:
    return {(e["episode_index"], v["camera_id"]): v["media"] for e in doc["samples"] for v in e["sample"]["views"]}


def _run(*argv) -> tuple[int, dict]:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = X.main(list(argv))
    return rc, json.loads(buf.getvalue())


def test_to_mcap_and_back_on_the_mini_twins(mini_dataset, mini_mcap, tmp_path):
    lr = _files(tmp_path / "lr")
    rc, report = _run("to-mcap", "--trajectory", lr, "--out", str(tmp_path / "mcap.json"), "--check", mini_mcap)
    assert rc == 0 and report["problems"] == [] and report["views"] == 7
    got = json.loads((tmp_path / "mcap.json").read_text())
    want = json.loads(open(_as_mcap(lr, tmp_path / "by-hand")).read())
    assert _media(got) == _media(want)
    assert [e["frames"] for e in got["samples"]] == [e["frames"] for e in want["samples"]]   # frames untouched
    rc, report = _run("to-lerobot", "--trajectory", str(tmp_path / "mcap.json"), "--out", str(tmp_path / "back.json"),
                      "--dataset", mini_dataset)
    assert rc == 0 and report["problems"] == []
    assert _media(json.loads((tmp_path / "back.json").read_text())) == _media(json.loads(open(lr).read()))


def test_the_check_says_what_does_not_fit(mini_mcap, tmp_path):
    lr = _files(tmp_path / "lr")
    rc, report = _run("to-mcap", "--trajectory", lr, "--out", str(tmp_path / "a.json"), "--check", mini_mcap,
                      "--map", "exterior=/observation.images.side")
    assert rc == 1 and len(report["problems"]) == 7
    assert "no such topic (the file has /action" in report["problems"][0]
    doc = json.loads(open(lr).read())
    doc["samples"][2]["sample"]["views"][0]["media"]["frame_count"] += 5
    (tmp_path / "off.json").write_text(json.dumps(doc))
    rc, report = _run("to-mcap", "--trajectory", str(tmp_path / "off.json"), "--out", str(tmp_path / "b.json"),
                      "--check", mini_mcap)
    assert rc == 1 and any("episode 2 camera exterior" in p and "image messages, frame_count says" in p
                           for p in report["problems"])
    with pytest.raises(SystemExit):
        X.main(["to-lerobot", "--trajectory", lr, "--out", str(tmp_path / "c.json")])        # --dataset is required
    assert X.main(["to-lerobot", "--trajectory", lr, "--out", str(tmp_path / "c.json"),
                   "--dataset", mini_mcap]) == 2                                            # not topic views


def test_dataset2_round_trip_keeps_the_v3_clips(tmp_path):
    root = demo_data.require("dataset2")
    src = str(root / "trajectory.json")
    rc, report = _run("to-mcap", "--trajectory", src, "--out", str(tmp_path / "mcap.json"))
    assert rc == 0, report
    mc = json.loads((tmp_path / "mcap.json").read_text())
    assert {m["topic"] for m in _media(mc).values()} == {"/observation.images.exterior_1_left",
                                                          "/observation.images.exterior_2_left"}
    assert {m["uri"] for m in _media(mc).values()} == {f"episode_{i}.mcap" for i in range(7)}
    rc, report = _run("to-lerobot", "--trajectory", str(tmp_path / "mcap.json"), "--out", str(tmp_path / "back.json"),
                      "--dataset", str(demo_data.lerobot_root("dataset2")))
    assert rc == 0 and report["problems"] == [], report
    back, orig = _media(json.loads((tmp_path / "back.json").read_text())), _media(json.loads(open(src).read()))
    assert back.keys() == orig.keys()
    for k in orig:
        assert back[k]["uri"] == orig[k]["uri"]
        assert back[k]["clip_start_s"] == pytest.approx(orig[k]["clip_start_s"], abs=1e-6)
        assert back[k]["clip_end_s"] == pytest.approx(orig[k]["clip_end_s"], abs=1e-6)
