"""LeRobot v2 export: full, incremental (drop / relabel / add), fallbacks, FSX discipline."""
from __future__ import annotations

import builtins
import json
import os
import stat

import numpy as np
import pandas as pd
import pytest

from curation.contracts import schemas
from curation.export.incremental import (ExportInputError, SourceChangedError, export_dataset)
from curation.export.lerobot_writer import export_lerobot_v2
from curation.export.manifest import (DETAIL_NAME, JOURNAL_NAME, MANIFEST_NAME, digest_json,
                                      export_fingerprint)

from .helpers import (V2_CAPTIONS, V2_TASK, final_list, quiet, read_jsonl, sha256, snapshot,
                      untouched, v2_entries)

DS = "lerobot_curated"
DELIVERED = [0, 1, 3, 4, 6]
CAMS = ("observation.images.exterior", "observation.images.wrist")


def export(src, out, indices, relabel=None, **kw):
    kw.setdefault("log", quiet)
    return export_dataset(src, final_list("passed", v2_entries(indices, relabel)), str(out), **kw)


def vid(k: int, cam: str) -> str:
    return f"videos/chunk-000/{cam}/episode_{k:06d}.mp4"


def data(k: int) -> str:
    return f"data/chunk-000/episode_{k:06d}.parquet"


def check_result(outcome, **expect):
    r = outcome.result
    assert schemas.errors("cli/export.schema.json", r) == []
    man = json.loads(open(outcome.manifest_path, encoding="utf-8").read())
    assert schemas.errors("cli/export-manifest.schema.json", man) == []
    assert man["fingerprint"] == r["fingerprint"]
    for k, v in expect.items():
        assert r[k] == v, (k, r[k], v)
    return man


def test_full_export_is_v1s_export(v2_source, tmp_path):
    mine = export(v2_source, tmp_path / "mine", DELIVERED)
    check_result(mine, incremental=False, episodes=5, videos_copied=10, videos_reencoded=0,
                 diff={"keep": 0, "relabel": 0, "renumber": 0, "add": 5, "drop": 0})
    v1_out = tmp_path / "v1"
    export_lerobot_v2(v2_source, DELIVERED, str(v1_out), task_overrides=V2_CAPTIONS)
    v1_files = snapshot(str(v1_out))
    ours = snapshot(str(tmp_path / "mine" / DS))
    # every file v1 writes is here, byte for byte; we add the stats v1 could not copy and a sidecar
    assert {rel: s[3] for rel, s in v1_files.items()} == \
        {rel: ours[rel][3] for rel in v1_files}
    assert set(ours) - set(v1_files) == {"meta/episodes_stats.jsonl", "meta/curation_episodes.jsonl"}
    sidecar = read_jsonl(str(tmp_path / "mine" / DS / "meta" / "curation_episodes.jsonl"))
    assert [(r["episode_index"], r["source_episode_index"], r["instruction_source"])
            for r in sidecar] == [(0, 0, "原始标注"), (1, 1, "原始标注"), (2, 3, "原始标注"),
                                  (3, 4, "自产caption"), (4, 6, "自产caption")]


def test_drop_a_middle_episode(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    before = snapshot(str(out / DS))
    o = export(v2_source, out, [0, 1, 4, 6])
    check_result(o, incremental=True, episodes=4, videos_copied=0, videos_reencoded=0,
                 diff={"keep": 2, "relabel": 0, "renumber": 2, "add": 0, "drop": 1})
    after = snapshot(str(out / DS))
    for k in (0, 1):                                   # not a byte touched
        assert untouched(before, after, data(k))
        for cam in CAMS:
            assert untouched(before, after, vid(k, cam))
    for old, new, src_ep in ((3, 2, 4), (4, 3, 6)):    # renamed, not copied
        for cam in CAMS:
            assert after[vid(new, cam)][0] == before[vid(old, cam)][0]
            assert after[vid(new, cam)][3] == sha256(os.path.join(
                v2_source, "videos", "chunk-000", cam, f"episode_{src_ep:06d}.mp4"))
    assert not any("episode_000004" in rel for rel in after)
    assert sorted(o.renamed) == sorted((vid(old, c), vid(new, c)) for old, new in ((3, 2), (4, 3))
                                       for c in CAMS)
    assert o.deleted == [data(4)]                      # the rest was renamed over or rewritten
    # frame tables renumbered: episode_index and a contiguous global index
    frames = pd.concat([pd.read_parquet(out / DS / data(k)) for k in range(4)], ignore_index=True)
    assert frames["index"].tolist() == list(range(len(frames)))
    assert sorted(frames["episode_index"].unique()) == [0, 1, 2, 3]
    # the result is exactly what a fresh export of the new list delivers
    fresh = export(v2_source, tmp_path / "fresh", [0, 1, 4, 6])
    assert {r: s[3] for r, s in snapshot(str(tmp_path / "fresh" / DS)).items()} == \
        {r: s[3] for r, s in after.items()}
    assert open(fresh.manifest_path).read() == open(o.manifest_path).read()


def test_relabel_touches_no_video(v2_source, tmp_path, monkeypatch):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    before = snapshot(str(out / DS))
    import av

    def no_video_io(*a, **k):
        raise AssertionError("a relabel must not decode, encode or copy a video")
    monkeypatch.setattr(av, "open", no_video_io)
    monkeypatch.setattr("curation.adapters.decode.decode_window", no_video_io)
    o = export(v2_source, out, DELIVERED, relabel={1: "put the red block back on the shelf"})
    check_result(o, incremental=True, videos_copied=0, videos_reencoded=0,
                 diff={"keep": 4, "relabel": 1, "renumber": 0, "add": 0, "drop": 0})
    after = snapshot(str(out / DS))
    assert all(untouched(before, after, rel) for rel in before if rel.startswith("videos/"))
    assert [rel for rel in before if rel.startswith("data/") and not untouched(before, after, rel)] \
        == [data(1)]
    tasks = read_jsonl(str(out / DS / "meta" / "tasks.jsonl"))
    new_idx = next(t["task_index"] for t in tasks if t["task"] == "put the red block back on the shelf")
    assert set(pd.read_parquet(out / DS / data(1))["task_index"]) == {new_idx}
    ep_rows = read_jsonl(str(out / DS / "meta" / "episodes.jsonl"))
    assert ep_rows[1]["tasks"] == ["put the red block back on the shelf"]
    assert read_jsonl(str(out / DS / "meta" / "curation_episodes.jsonl"))[1]["instruction_source"] == "人工改标"
    # the other episodes kept their task numbers (the table is kept stable)
    for k in (0, 2, 3, 4):
        assert untouched(before, after, data(k))


def test_adding_an_episode_back_copies_only_its_videos(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, [0, 1, 4, 6])
    o = export(v2_source, out, DELIVERED)
    check_result(o, incremental=True, videos_copied=2, videos_reencoded=0,
                 diff={"keep": 2, "relabel": 0, "renumber": 2, "add": 1, "drop": 0})
    assert len(o.renamed) == 4
    fresh = export(v2_source, tmp_path / "fresh", DELIVERED)
    assert {r: s[3] for r, s in snapshot(str(tmp_path / "fresh" / DS)).items()} == \
        {r: s[3] for r, s in snapshot(str(out / DS)).items()}
    assert fresh.result["fingerprint"] == o.result["fingerprint"]


def test_unchanged_export_touches_nothing(v2_source, tmp_path):
    out = tmp_path / "out"
    first = export(v2_source, out, DELIVERED)
    before = snapshot(str(out / DS))
    o = export(v2_source, out, DELIVERED)
    check_result(o, incremental=True, videos_copied=0,
                 diff={"keep": 5, "relabel": 0, "renumber": 0, "add": 0, "drop": 0})
    assert o.written == [] and o.renamed == [] and o.deleted == []
    assert snapshot(str(out / DS)) == before
    assert o.result["fingerprint"] == first.result["fingerprint"]


def test_interrupted_export_is_redone_in_full(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    (out / JOURNAL_NAME).write_text(json.dumps({"to": "sha256:" + "f" * 64}))
    (out / DS / "videos" / "chunk-000" / CAMS[0] / ".curation-pub-leftover").write_bytes(b"x")
    o = export(v2_source, out, [0, 1, 4, 6])
    check_result(o, incremental=False, videos_copied=8,
                 diff={"keep": 2, "relabel": 0, "renumber": 2, "add": 0, "drop": 1})
    assert any("interrupted" in r for r in o.rebuild_reasons)
    assert not (out / JOURNAL_NAME).exists()
    fresh = export(v2_source, tmp_path / "fresh", [0, 1, 4, 6])
    assert snapshot(str(tmp_path / "fresh" / DS)).keys() == snapshot(str(out / DS)).keys()
    assert fresh.result["fingerprint"] == o.result["fingerprint"]


@pytest.mark.parametrize("damage", ["delete", "truncate"])
def test_damaged_delivery_is_redone_in_full(v2_source, tmp_path, damage):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    victim = out / DS / vid(2, CAMS[1])
    if damage == "delete":
        victim.unlink()
    else:
        victim.write_bytes(victim.read_bytes()[:100])
    o = export(v2_source, out, DELIVERED)
    assert o.result["incremental"] is False
    assert any("missing or changed" in r for r in o.rebuild_reasons)
    assert sha256(str(victim)) == sha256(os.path.join(
        v2_source, "videos", "chunk-000", CAMS[1], "episode_000003.mp4"))


def test_full_export_on_request(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    o = export(v2_source, out, [0, 1, 4, 6], incremental=False)
    check_result(o, incremental=False, videos_copied=8,
                 diff={"keep": 2, "relabel": 0, "renumber": 2, "add": 0, "drop": 1})


def _source_manifest(root: str) -> dict:
    objects = []
    for cur, _d, names in os.walk(root):
        for n in sorted(names):
            p = os.path.join(cur, n)
            st = os.stat(p)
            objects.append({"key": os.path.relpath(p, root).replace(os.sep, "/"),
                            "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    objects.sort(key=lambda o: o["key"])
    doc = {"schema_version": "1.0", "input": root, "objects": objects,
           "summary": {"count": len(objects), "bytes": sum(o["size"] for o in objects),
                       "digest": digest_json(objects)}}
    assert schemas.errors("cli/source-manifest.schema.json", doc) == []
    return doc


def test_source_manifest_pins_the_source(v2_source, tmp_path):
    sm = _source_manifest(v2_source)
    o = export(v2_source, tmp_path / "ok", DELIVERED, source_manifest=sm)
    entries = v2_entries(DELIVERED)
    assert o.result["fingerprint"] == export_fingerprint(
        entries, source_format="lerobot_v2", source_digest=sm["summary"]["digest"])
    bad = json.loads(json.dumps(sm))
    victim = next(x for x in bad["objects"] if x["key"] == data(3))
    victim["size"] += 1
    with pytest.raises(SourceChangedError) as err:
        export(v2_source, tmp_path / "bad", DELIVERED, source_manifest=bad)
    assert err.value.exit_code == 6 and data(3) in str(err.value)
    extra = json.loads(json.dumps(sm))
    extra["objects"] = [x for x in extra["objects"] if x["key"] != "meta/tasks.jsonl"]
    with pytest.raises(SourceChangedError, match="not in source_manifest"):
        export(v2_source, tmp_path / "extra", DELIVERED, source_manifest=extra)


def test_computed_stats_follow_the_episodes(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    first = read_jsonl(str(out / DS / "meta" / "episodes_stats.jsonl"))
    export(v2_source, out, [0, 1, 4, 6])
    rows = read_jsonl(str(out / DS / "meta" / "episodes_stats.jsonl"))
    assert [r["episode_index"] for r in rows] == [0, 1, 2, 3]
    info = json.loads((out / DS / "meta" / "info.json").read_text())
    for r in rows:
        st = r["stats"]
        for cam in CAMS:
            assert np.asarray(st[cam]["mean"]).shape == (3, 1, 1)
        assert np.asarray(st["action"]["mean"]).shape == (7,)
        assert np.asarray(st["timestamp"]["min"]).shape == (1,)
        assert st["episode_index"]["min"] == [r["episode_index"]]
    # episode 4 moved from new index 3 to 2: its video stats are carried, its numbers redone
    assert rows[2]["stats"][CAMS[0]] == first[3]["stats"][CAMS[0]]
    assert rows[2]["stats"]["index"]["min"] != first[3]["stats"]["index"]["min"]
    assert info["total_frames"] == sum(r["stats"]["index"]["count"][0] for r in rows)


def test_source_stats_are_carried_like_v1(v2_source, tmp_path):
    import shutil
    src = tmp_path / "src"
    shutil.copytree(v2_source, src)
    with open(src / "meta" / "episodes_stats.jsonl", "w") as f:
        for i in range(8):
            f.write(json.dumps({"episode_index": i, "stats": {"action": {"marker": [i]}}}) + "\n")
    out = tmp_path / "out"
    export(str(src), out, DELIVERED)
    export(str(src), out, [0, 1, 4, 6])
    rows = read_jsonl(str(out / DS / "meta" / "episodes_stats.jsonl"))
    assert [(r["episode_index"], r["stats"]["action"]["marker"]) for r in rows] == \
        [(0, [0]), (1, [1]), (2, [4]), (3, [6])]


def test_delivered_files_follow_their_directory_permissions(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    for rel in (data(0), vid(0, CAMS[0]), "meta/info.json"):
        p = out / DS / rel
        want = stat.S_IMODE(os.stat(p.parent).st_mode) & 0o666
        assert stat.S_IMODE(os.stat(p).st_mode) == want
    assert stat.S_IMODE(os.stat(out / MANIFEST_NAME).st_mode) == \
        stat.S_IMODE(os.stat(out).st_mode) & 0o666


def test_only_whole_file_copies_land_in_the_delivery(v2_source, tmp_path, monkeypatch):
    """FSX refuses random writes: writers work in scratch, the delivery only sees
    sequential copies into temporary names that are then renamed into place."""
    out = tmp_path / "out"
    root = str(out)
    opened, parquet_writes = [], []
    real_open = builtins.open

    def spy_open(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wax+"):
            opened.append(os.path.abspath(os.fspath(file)))
        return real_open(file, mode, *a, **k)
    import pyarrow.parquet as pq
    real_write_table = pq.write_table

    def spy_write_table(table, where, *a, **k):
        parquet_writes.append(os.path.abspath(os.fspath(where)))
        return real_write_table(table, where, *a, **k)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(pq, "write_table", spy_write_table)
    export(v2_source, out, DELIVERED)
    export(v2_source, out, [0, 1, 4, 6])
    export(v2_source, out, [0, 1, 3, 4, 6], relabel={4: "wipe it"})
    inside = [p for p in opened if p.startswith(root + os.sep)]
    assert inside and all(os.path.basename(p).startswith(".curation-pub-") for p in inside), inside
    assert parquet_writes and not [p for p in parquet_writes if p.startswith(root + os.sep)]


def test_parallel_export_writes_the_same_bytes(v2_source, tmp_path):
    seq, par = tmp_path / "seq", tmp_path / "par"
    progress = []
    for indices in (DELIVERED, [0, 1, 4, 6], [0, 1, 3, 4, 6]):
        export(v2_source, seq, indices)
        o = export(v2_source, par, indices, concurrency=4,
                   progress=lambda done, total: progress.append((done, total)))
        assert {r: s[3] for r, s in snapshot(str(par / DS)).items()} == \
            {r: s[3] for r, s in snapshot(str(seq / DS)).items()}
    assert o.result["videos_copied"] == 2
    assert progress[-1][0] == progress[-1][1] > 0


def test_empty_passed_list_leaves_no_dataset(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    o = export(v2_source, out, [])
    man = check_result(o, episodes=0, diff={"keep": 0, "relabel": 0, "renumber": 0, "add": 0,
                                            "drop": 5})
    assert man["episodes"] == [] and not (out / DS).exists()
    assert (out / DETAIL_NAME).exists()


def test_bad_lists_are_refused(v2_source, tmp_path):
    with pytest.raises(ExportInputError, match="not in the source"):
        export(v2_source, tmp_path / "a", [0, 42])
    with pytest.raises(ExportInputError, match="twice"):
        export_dataset(v2_source, [{"episode_index": 1}, {"episode_index": 1}], str(tmp_path / "b"),
                       log=quiet)
    with pytest.raises(ExportInputError, match="passed list"):
        export_dataset(v2_source, final_list("held", []), str(tmp_path / "c"), log=quiet)


def test_task_text_is_written_where_loaders_read_it(v2_source, tmp_path):
    out = tmp_path / "out"
    export(v2_source, out, DELIVERED)
    tasks = [t["task"] for t in read_jsonl(str(out / DS / "meta" / "tasks.jsonl"))]
    assert tasks == [V2_TASK, V2_CAPTIONS[4], V2_CAPTIONS[6]]
    rows = read_jsonl(str(out / DS / "meta" / "episodes.jsonl"))
    assert [r["tasks"] for r in rows] == [[V2_TASK]] * 3 + [[V2_CAPTIONS[4]], [V2_CAPTIONS[6]]]
    ti = [int(pd.read_parquet(out / DS / data(k))["task_index"].iloc[0]) for k in range(5)]
    assert [tasks[i] for i in ti] == [r["tasks"][0] for r in rows]
