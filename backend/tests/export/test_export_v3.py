"""LeRobot v3 export: shared files, only the video files a drop hits are re-encoded."""
from __future__ import annotations

import builtins
import json
import os
from collections import defaultdict

import numpy as np
import pandas as pd
import pytest

from curation.adapters.decode import decode_window
from curation.export.lerobot_writer import export_lerobot_v3

from .helpers import V3_PARAMS as PARAMS
from .helpers import export_v3 as export3
from .helpers import read_jsonl, snapshot, untouched
from .v3_fixture import LENGTHS, TASKS

DS = "lerobot_curated"
CAMS = ("observation.images.exterior", "observation.images.wrist")


def video_files(state) -> dict[tuple[str, str], list[int]]:
    """(camera, delivered path) -> source episodes in it."""
    out = defaultdict(list)
    for e in state.episodes:
        for cam, rel in e.videos.items():
            out[(cam, rel)].append(e.episode_index)
    return dict(out)


def data_files(state) -> dict[str, list[int]]:
    out = defaultdict(list)
    for e in state.episodes:
        out[e.parquet].append(e.episode_index)
    return dict(out)


def source_window(src: str, ep: int, cam: str) -> list[np.ndarray]:
    meta = pd.read_parquet(os.path.join(src, "meta", "episodes", "chunk-000", "file-000.parquet"))
    row = meta.set_index("episode_index").loc[ep]
    path = os.path.join(src, "videos", cam, f"chunk-000/file-{int(row[f'videos/{cam}/file_index']):03d}.mp4")
    frames, _ = decode_window(path, float(row[f"videos/{cam}/from_timestamp"]),
                              float(row[f"videos/{cam}/to_timestamp"]))
    return frames


def assert_frames_match_source(src: str, out, state) -> None:
    """Every delivered episode shows its own source frames on every camera."""
    for e in state.episodes:
        for cam in CAMS:
            a, b = e.windows[cam]
            got, _ = decode_window(str(out / DS / e.videos[cam]), a, b)
            want = source_window(src, e.episode_index, cam)
            assert len(got) == len(want) == e.length, (e.episode_index, cam)
            diff = np.mean([np.abs(g.astype(np.int16) - w).mean() for g, w in zip(got, want, strict=True)])
            assert diff < 4.0, (e.episode_index, cam, diff)      # re-encoded, not re-cut


def assert_frame_tables_are_consistent(src: str, out, state) -> None:
    info = json.loads((out / DS / "meta" / "info.json").read_text())
    files = sorted({e.parquet for e in state.episodes})
    frames = pd.concat([pd.read_parquet(out / DS / f) for f in files], ignore_index=True)
    assert frames["index"].tolist() == list(range(len(frames))) == list(range(info["total_frames"]))
    meta = pd.read_parquet(out / DS / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    assert meta["episode_index"].tolist() == list(range(len(state.episodes)))
    src_frames = pd.concat([pd.read_parquet(os.path.join(src, "data", "chunk-000", f"file-00{k}.parquet"))
                            for k in (0, 1)], ignore_index=True)
    tasks = pd.read_parquet(out / DS / "meta" / "tasks.parquet")
    for e, (_, row) in zip(state.episodes, meta.iterrows(), strict=True):
        mine = frames.iloc[row["dataset_from_index"]:row["dataset_to_index"]]
        theirs = src_frames[src_frames["episode_index"] == e.episode_index]
        assert (mine["episode_index"] == e.new_index).all()
        assert np.array_equal(np.stack(mine["action"].to_numpy()), np.stack(theirs["action"].to_numpy()))
        assert tasks.index[int(mine["task_index"].iloc[0])] == e.tasks[0] == list(row["tasks"])[0]


def test_full_export_is_v1s_export(v3_source, tmp_path):
    o = export3(v3_source, tmp_path / "mine", range(6))
    assert o.result["diff"]["add"] == 6 and o.result["videos_copied"] == 0
    v1_out = tmp_path / "v1"
    export_lerobot_v3(v3_source, list(range(6)), str(v1_out), video_file_mb=PARAMS["video_file_mb"],
                      data_file_mb=PARAMS["data_file_mb"])
    v1_files = snapshot(str(v1_out))
    ours = snapshot(str(tmp_path / "mine" / DS))
    assert {r: s[3] for r, s in v1_files.items()} == {r: ours[r][3] for r in v1_files}
    assert set(ours) - set(v1_files) == {"meta/curation_episodes.jsonl"}
    assert len({rel for cam, rel in video_files(o.state) if cam == CAMS[0]}) >= 2
    assert o.result["videos_reencoded"] == len(video_files(o.state))
    assert_frames_match_source(v3_source, tmp_path / "mine", o.state)
    assert_frame_tables_are_consistent(v3_source, tmp_path / "mine", o.state)


@pytest.mark.parametrize("dropped", [1, 2, 4])
def test_drop_a_middle_episode_reencodes_only_its_files(v3_source, tmp_path, dropped):
    out = tmp_path / "out"
    first = export3(v3_source, out, range(6))
    before = snapshot(str(out / DS))
    keep = [i for i in range(6) if i != dropped]
    o = export3(v3_source, out, keep)
    after = snapshot(str(out / DS))
    hit = {rel for (cam, rel), eps in video_files(first.state).items() if dropped in eps}
    spared = {rel for (cam, rel), eps in video_files(first.state).items() if dropped not in eps}
    assert spared, "the layout must have video files the drop does not hit"
    assert all(untouched(before, after, rel) for rel in spared)
    assert all(rel not in after or not untouched(before, after, rel) for rel in hit)
    assert o.result["videos_reencoded"] == sum(
        1 for (cam, rel), eps in video_files(first.state).items() if dropped in eps and len(eps) > 1)
    assert o.result["diff"] == {"keep": dropped, "relabel": 0, "renumber": 5 - dropped,
                                "add": 0, "drop": 1}
    # frame tables before the dropped episode's file are untouched
    first_data = data_files(first.state)
    for rel, eps in first_data.items():
        if max(eps) < dropped:
            assert untouched(before, after, rel)
    assert_frames_match_source(v3_source, out, o.state)
    assert_frame_tables_are_consistent(v3_source, out, o.state)
    assert set(after) == {e.parquet for e in o.state.episodes} | \
        {rel for e in o.state.episodes for rel in e.videos.values()} | \
        {f for f in after if f.startswith("meta/")}


def test_relabel_touches_no_video(v3_source, tmp_path, monkeypatch):
    out = tmp_path / "out"
    export3(v3_source, out, range(6))
    before = snapshot(str(out / DS))
    import av

    def no_video_io(*a, **k):
        raise AssertionError("a relabel must not decode or encode a video")
    monkeypatch.setattr(av, "open", no_video_io)
    monkeypatch.setattr("curation.adapters.decode.decode_window", no_video_io)
    # episode 3 is the only user of its text: the new text takes over its number
    o = export3(v3_source, out, range(6), relabel={3: "wipe the table"})
    assert o.result["diff"] == {"keep": 5, "relabel": 1, "renumber": 0, "add": 0, "drop": 0}
    assert o.result["videos_reencoded"] == 0 and o.result["videos_copied"] == 0
    after = snapshot(str(out / DS))
    assert all(untouched(before, after, rel) for rel in before if not rel.startswith("meta/"))
    tasks = pd.read_parquet(out / DS / "meta" / "tasks.parquet")
    assert list(tasks.index) == [TASKS[0], TASKS[1], "wipe the table"]
    # episode 0 shares its text: the new one is appended, only episode 0's frame table changes
    o = export3(v3_source, out, range(6), relabel={3: "wipe the table", 0: "grab the block"})
    assert o.result["diff"]["relabel"] == 1 and o.result["videos_reencoded"] == 0
    again = snapshot(str(out / DS))
    changed = {rel for rel in before if not rel.startswith("meta/") and not untouched(after, again, rel)}
    assert changed == {o.state.episodes[0].parquet}
    meta = pd.read_parquet(out / DS / "meta" / "episodes" / "chunk-000" / "file-000.parquet")
    assert list(meta.loc[0, "tasks"]) == ["grab the block"]
    assert read_jsonl(str(out / DS / "meta" / "curation_episodes.jsonl"))[0]["instruction_source"] == "人工改标"


def test_adding_an_episode_encodes_only_new_files(v3_source, tmp_path):
    out = tmp_path / "out"
    first = export3(v3_source, out, [0, 1, 3, 4, 5])
    before = snapshot(str(out / DS))
    o = export3(v3_source, out, range(6))
    after = snapshot(str(out / DS))
    assert o.result["diff"] == {"keep": 2, "relabel": 0, "renumber": 3, "add": 1, "drop": 0}
    assert all(untouched(before, after, rel) for (_c, rel) in video_files(first.state))
    new_files = {rel for (_c, rel) in video_files(o.state)} - {rel for (_c, rel) in video_files(first.state)}
    assert len(new_files) == len(CAMS) == o.result["videos_reencoded"]
    assert all(video_files(o.state)[(c, rel)] == [2] for (c, rel) in video_files(o.state) if rel in new_files)
    assert_frames_match_source(v3_source, out, o.state)
    assert_frame_tables_are_consistent(v3_source, out, o.state)


def test_reordered_list_lays_frame_tables_out_afresh(v3_source, tmp_path):
    out = tmp_path / "out"
    export3(v3_source, out, range(6))
    msgs = []
    o = export3(v3_source, out, [3, 4, 5, 0, 1, 2], log=lambda lvl, m: msgs.append(m))
    assert o.result["incremental"] is True and o.result["videos_reencoded"] == 0
    assert any("afresh" in m for m in msgs)
    assert_frames_match_source(v3_source, out, o.state)
    assert_frame_tables_are_consistent(v3_source, out, o.state)


def test_camera_health_sidecar_is_carried_and_renumbered(v3_source, tmp_path):
    out = tmp_path / "out"
    health = {"dataset": {"note": "x"},
              "episodes": {f"ep{i:06d}": {"verdict": "ok", "flagged_cameras": [],
                                           "consensus_lag_s": 0.01 * i, "per_camera": {}}
                           for i in (2, 4)}}
    export3(v3_source, out, range(6), camera_health=health)
    export3(v3_source, out, [0, 1, 3, 4, 5])
    doc = json.loads((out / DS / "meta" / "curation_camera_health.json").read_text())
    assert [(r["episode_index"], r["source_episode_id"]) for r in doc["episodes"]] == [(3, "ep000004")]
    assert doc["dataset"] == {"note": "x"}


def test_encoders_and_parquet_writers_never_write_into_the_delivery(v3_source, tmp_path, monkeypatch):
    out = tmp_path / "out"
    root = str(out)
    writes: list[str] = []
    import av
    import pyarrow.parquet as pq
    real_av_open, real_open, real_writer = av.open, builtins.open, pq.ParquetWriter

    def spy_av_open(file, mode="r", *a, **k):
        if "w" in str(mode):
            writes.append(os.path.abspath(os.fspath(file)))
        return real_av_open(file, mode, *a, **k)

    class SpyWriter(real_writer):
        def __init__(self, where, *a, **k):
            writes.append(os.path.abspath(os.fspath(where)))
            super().__init__(where, *a, **k)
    opened: list[str] = []

    def spy_open(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wax+"):
            opened.append(os.path.abspath(os.fspath(file)))
        return real_open(file, mode, *a, **k)
    monkeypatch.setattr(av, "open", spy_av_open)
    monkeypatch.setattr(pq, "ParquetWriter", SpyWriter)
    monkeypatch.setattr(builtins, "open", spy_open)
    export3(v3_source, out, range(6))
    export3(v3_source, out, [0, 1, 3, 4, 5])
    export3(v3_source, out, range(6), relabel={0: "x"})
    assert writes and not [p for p in writes if p.startswith(root + os.sep)], writes
    inside = [p for p in opened if p.startswith(root + os.sep)]
    assert inside and all(os.path.basename(p).startswith(".curation-pub-") for p in inside)


def test_parallel_export_writes_the_same_bytes(v3_source, tmp_path):
    seq, par = tmp_path / "seq", tmp_path / "par"
    for indices in (range(6), [0, 1, 3, 4, 5], range(6)):
        export3(v3_source, seq, indices)
        o = export3(v3_source, par, indices, concurrency=3)
        assert {r: s[3] for r, s in snapshot(str(par / DS)).items()} == \
            {r: s[3] for r, s in snapshot(str(seq / DS)).items()}
    assert o.result["diff"]["add"] == 1


def test_frame_counts_match_rows(v3_source, tmp_path):
    o = export3(v3_source, tmp_path / "out", [5, 4, 3, 2, 1, 0][::-1])
    for e in o.state.episodes:
        for cam in CAMS:
            a, b = e.windows[cam]
            assert round((b - a) * 15) == LENGTHS[e.episode_index]
