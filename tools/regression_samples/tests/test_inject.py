"""The injectors added after the 2026-10-01 audit: mcap structure faults (FILE-3), LeRobot v3 index faults (FILE-10), the
time-offset mappings (AV-5) and the dataset-level metadata faults (FILE-8)."""
from __future__ import annotations

import io
import json
import os
import struct
import sys

import pandas as pd
import pytest

from regression_samples import inject as INJ
from regression_samples import inject_mcap as M
from regression_samples import inject_v3 as V3

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(REPO, "backend"))


# ---------------------------------------------------------------- mcap (FILE-3)

def small_mcap(path, n=40):
    from mcap.writer import Writer
    with open(path, "wb") as fh:
        w = Writer(fh, chunk_size=256)
        w.start(profile="", library="test")
        sid = w.register_schema(name="raw", encoding="jsonschema", data=b"{}")
        cid = w.register_channel(topic="/robot0/vio/eef_pose", message_encoding="json", schema_id=sid)
        for i in range(n):
            w.add_message(channel_id=cid, log_time=i * 33_000_000, publish_time=i * 33_000_000, data=json.dumps({"i": i}).encode())
        w.finish()
    return path


def messages(path):
    from mcap.reader import make_reader
    with open(path, "rb") as fh:
        return [m.log_time for _, _, m in make_reader(fh).iter_messages()]


def chunk_crc_error(path):
    from mcap.stream_reader import CRCValidationError, StreamReader, get_chunk_data_stream   # where the platform imports it from
    from mcap.records import Chunk
    try:
        with open(path, "rb") as fh:
            for rec in StreamReader(fh, emit_chunks=True, validate_crcs=True).records:
                if isinstance(rec, Chunk) and rec.uncompressed_crc:
                    get_chunk_data_stream(rec, validate_crc=True)
    except CRCValidationError:
        return True
    return False


def footer_of(path):
    from curation.cli import containers
    with open(path, "rb") as fh:
        return containers.read_footer(fh)


@pytest.fixture()
def mcap_set(tmp_path):
    bases = [small_mcap(str(tmp_path / f"base_{i}.mcap")) for i in range(7)]
    out = tmp_path / "out"
    M.main(["--out", str(out), "--bases", ",".join(bases), "--controls", "2"])
    return out, json.loads((out / "injection.json").read_text())


def test_the_legal_variants_read_back_whole(mcap_set):
    out, inj = mcap_set
    by = {r["fault"]: r for r in inj["episodes"]}
    ns, zc = str(out / f"episode_{by['no_summary']['episode_index']}.mcap"), str(out / f"episode_{by['zero_crcs']['episode_index']}.mcap")
    assert messages(ns) == messages(str(out / "episode_5.mcap"))                # every message is still there
    assert footer_of(ns).summary_start == 0 and open(ns, "rb").read().endswith(M.MAGIC)
    assert not chunk_crc_error(zc) and footer_of(zc).summary_crc_ok is not False
    assert by["no_summary"]["item"] is None and by["zero_crcs"]["item"] is None


def test_the_broken_variants_are_caught_where_the_spec_says(mcap_set):
    out, inj = mcap_set
    by = {r["fault"]: r for r in inj["episodes"]}
    cut = (out / f"episode_{by['cut_off']['episode_index']}.mcap").read_bytes()
    assert not cut.endswith(M.MAGIC) and len(cut) < by["cut_off"]["params"]["of_bytes"]
    assert chunk_crc_error(str(out / f"episode_{by['chunk_crc']['episode_index']}.mcap"))
    assert footer_of(str(out / f"episode_{by['summary_crc']['episode_index']}.mcap")).summary_crc_ok is False
    assert {r["item"] for r in inj["episodes"] if r["fault"] in ("cut_off", "chunk_crc", "summary_crc")} == {"FILE-3"}
    assert [r["fault"] for r in inj["episodes"][-2:]] == [None, None]      # the controls
    assert "| episode_0.mcap |" in (out / "README.md").read_text()


# ---------------------------------------------------------------- LeRobot v3 (FILE-10)

def small_v3(root, lengths=(5, 6, 7)):
    os.makedirs(os.path.join(root, "meta/episodes/chunk-000"))
    os.makedirs(os.path.join(root, "data/chunk-000"))
    info = {"codebase_version": "v3.0", "fps": 10, "total_episodes": len(lengths), "total_frames": sum(lengths),
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": {"observation.images.front": {"dtype": "video", "shape": [8, 8, 3]}, "action": {"dtype": "float32", "shape": [1]}}}
    json.dump(info, open(os.path.join(root, "meta/info.json"), "w"))
    rows, eps, start = [], [], 0
    for e, n in enumerate(lengths):
        for f in range(n):
            rows.append({"action": [float(f)], "timestamp": f / 10, "frame_index": f, "episode_index": e, "index": start + f, "task_index": 0})
        eps.append({"episode_index": e, "data/chunk_index": 0, "data/file_index": 0, "dataset_from_index": start, "dataset_to_index": start + n,
                    "videos/observation.images.front/chunk_index": 0, "videos/observation.images.front/file_index": 0,
                    "videos/observation.images.front/from_timestamp": start / 10, "videos/observation.images.front/to_timestamp": (start + n) / 10,
                    "tasks": ["move"], "length": n})
        start += n
    pd.DataFrame(rows).to_parquet(os.path.join(root, "data/chunk-000/file-000.parquet"), index=False)
    pd.DataFrame(eps).to_parquet(os.path.join(root, "meta/episodes/chunk-000/file-000.parquet"), index=False)
    pd.DataFrame({"task_index": [0]}, index=pd.Index(["move"], name="task")).to_parquet(os.path.join(root, "meta/tasks.parquet"))
    return root


def test_each_v3_fault_breaks_one_reference_of_one_episode(tmp_path):
    base = small_v3(str(tmp_path / "base"))
    out = str(tmp_path / "out")
    V3.main(["--base", base, "--out", out, "--plan", "offset:1:2,video_range:2:0.5,dangling_task:0,frame_index:2"])
    eps = pd.read_parquet(os.path.join(out, "meta/episodes/chunk-000/file-000.parquet")).set_index("episode_index")
    before = pd.read_parquet(os.path.join(base, "meta/episodes/chunk-000/file-000.parquet")).set_index("episode_index")
    assert (eps.at[1, "dataset_from_index"], eps.at[1, "dataset_to_index"]) == (7, 13)       # was 5..11
    assert eps.at[2, "videos/observation.images.front/to_timestamp"] == pytest.approx(before.at[2, "videos/observation.images.front/to_timestamp"] + 0.5)
    assert eps.at[0, "dataset_from_index"] == before.at[0, "dataset_from_index"]            # untouched
    d = pd.read_parquet(os.path.join(out, "data/chunk-000/file-000.parquet"))
    assert set(d[d["episode_index"] == 0]["task_index"]) == {7} and set(d[d["episode_index"] == 1]["task_index"]) == {0}
    fi = list(d[d["episode_index"] == 2]["frame_index"])
    assert len(fi) == 7 and len(set(fi)) == 6                                                # one repeat, one skip
    inj = json.load(open(os.path.join(out, "injection.json")))
    assert [r["item"] for r in inj["episodes"]] == ["FILE-10", "FILE-10", "FILE-10"]
    assert next(r for r in inj["episodes"] if r["fault"] == "offset")["neighbours"] == [0, 2]
    base_d = pd.read_parquet(os.path.join(base, "data/chunk-000/file-000.parquet"))
    assert set(base_d["task_index"]) == {0}                                                    # the base is not modified


# ---------------------------------------------------------------- AV-5 mappings, FILE-8 metadata

def test_drift_falls_behind_steadily_and_reset_jumps_once():
    m = INJ.drift_map(31, 6)
    assert m[0] == 0 and m[-1] == 24 and all(b - a in (0, 1) for a, b in zip(m, m[1:]))
    r = INJ.reset_map(10, 5, 3)
    assert r == [0, 1, 2, 3, 4, 2, 3, 4, 5, 6]


def test_dataset_metadata_faults_contradict_the_data():
    info = {"fps": 15, "total_episodes": 3, "total_frames": 300}
    rec = INJ.dataset_meta_fps(info, None)
    assert info["fps"] == 30 and rec["item"] == "FILE-8" and rec["params"]["actual_fps"] == 15
    info = {"fps": 15, "total_episodes": 3, "total_frames": 300}
    rec = INJ.dataset_meta_totals(info, None)
    assert (info["total_episodes"], info["total_frames"]) == (4, 337) and rec["params"]["actual"] == {"total_episodes": 3, "total_frames": 300}
