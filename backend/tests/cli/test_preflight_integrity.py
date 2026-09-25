"""Preflight's three integrity warnings (D52, design doc 14 §1, F7.1).

Empty or tiny files (the listing), mcap recordings cut off (no end marker) and mcap
summary sections that fail their CRC: warnings only - module availability stays what it
is on the clean dataset, and no sample is read.
"""
from __future__ import annotations

import os
import shutil

import pytest

from curation.cli import containers

pytest.importorskip("mcap", reason="mcap is needed for the mcap format")
pytest.importorskip("mcap_ros2", reason="mcap-ros2-support writes the fixture")


@pytest.fixture(scope="module")
def mini_mcap(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_mcap

    return make_mini_mcap(str(tmp_path_factory.mktemp("mcap") / "mini_mcap"))


@pytest.fixture
def mcap_copy(mini_mcap, tmp_path) -> str:
    dst = tmp_path / "mcap"
    shutil.copytree(mini_mcap, dst)
    return str(dst)


def _availability(doc: dict) -> dict:
    return {m["id"]: m["availability"] for m in doc["modules"]}


def _new_warnings(doc: dict) -> list[str]:
    keys = ("empty or too small", "cut off while recording", "fails its CRC")
    return [w for w in doc["warnings"] if any(k in w for k in keys)]


# ---------------------------------------------------------------- LeRobot


def test_clean_lerobot_has_no_integrity_warning(cli, dataset):
    assert _new_warnings(cli("preflight", "--input", dataset).doc) == []


def test_empty_and_tiny_videos_are_warned_about(cli, dataset):
    clean = cli("preflight", "--input", dataset).doc
    wrist = os.path.join(dataset, "videos", "chunk-000", "observation.images.wrist",
                         "episode_000002.mp4")
    ext = os.path.join(dataset, "videos", "chunk-000", "observation.images.exterior",
                       "episode_000005.mp4")
    open(wrist, "wb").close()                                   # 0 bytes
    with open(ext, "r+b") as fh:                                # no room for a moov
        fh.truncate(300)
    doc = cli("preflight", "--input", dataset).doc
    [w] = _new_warnings(doc)
    assert w.startswith("2 files are empty or too small to be valid (2 episodes: 2, 5;")
    assert "episode_000002.mp4 0 B" in w and "episode_000005.mp4 300 B" in w
    assert _availability(doc) == _availability(clean)          # D52: warnings only


def test_an_empty_parquet_is_warned_about(cli, dataset):
    path = os.path.join(dataset, "data", "chunk-000", "episode_000003.parquet")
    open(path, "wb").close()
    [w] = _new_warnings(cli("preflight", "--input", dataset).doc)
    assert "(1 episode: 3;" in w and "episode_000003.parquet 0 B" in w


# ---------------------------------------------------------------- mcap


def test_clean_mcap_has_no_integrity_warning(cli, mini_mcap):
    doc = cli("preflight", "--input", mini_mcap).doc
    assert _new_warnings(doc) == []
    with open(os.path.join(mini_mcap, "episode_0.mcap"), "rb") as fh:
        footer = containers.read_footer(fh)
    assert footer.ends_with_magic and footer.summary_crc and footer.summary_crc_ok is True


def test_a_recording_cut_off_is_named(cli, mcap_copy):
    clean = cli("preflight", "--input", mcap_copy).doc
    path = os.path.join(mcap_copy, "episode_4.mcap")
    size = os.path.getsize(path)
    with open(path, "r+b") as fh:
        fh.truncate(size // 2)
    doc = cli("preflight", "--input", mcap_copy).doc
    assert _new_warnings(doc) == [
        "1 episode (4) was cut off while recording (no mcap end marker); the checks read "
        "what is there"]
    # the old "no summary section" warning does not repeat it
    assert not any("no mcap summary section" in w for w in doc["warnings"])
    assert _availability(doc) == _availability(clean)


def test_a_summary_crc_failure_is_named(cli, mcap_copy):
    path = os.path.join(mcap_copy, "episode_6.mcap")
    with open(path, "rb") as fh:
        footer = containers.read_footer(fh)
    with open(path, "r+b") as fh:                    # a byte of the summary section, flipped
        fh.seek(footer.summary_start + 20)
        b = fh.read(1)
        fh.seek(footer.summary_start + 20)
        fh.write(bytes([b[0] ^ 0x5A]))
    doc = cli("preflight", "--input", mcap_copy).doc
    assert [w for w in _new_warnings(doc) if "CRC" in w] == [
        "1 episode (6) has a summary section that fails its CRC; the topics and counts read "
        "from it may be wrong"]


def test_an_empty_mcap_file_is_named(cli, mcap_copy):
    open(os.path.join(mcap_copy, "episode_2.mcap"), "wb").close()
    doc = cli("preflight", "--input", mcap_copy).doc
    assert "1 file is empty or too small to be valid (2)" in _new_warnings(doc)
    assert not any("cut off" in w for w in doc["warnings"])


def test_footer_of_a_file_without_crcs(tmp_path):
    from mcap.writer import Writer

    path = tmp_path / "plain.mcap"
    with open(path, "wb") as fh:
        w = Writer(fh, enable_crcs=False)
        w.start()
        sid = w.register_schema("s", "raw", b"")
        ch = w.register_channel("/x", "raw", sid)
        w.add_message(ch, 1, b"abc", 1)
        w.finish()
    with open(path, "rb") as fh:
        footer = containers.read_footer(fh)
    assert footer.ends_with_magic and footer.summary_crc == 0 and footer.summary_crc_ok is None
