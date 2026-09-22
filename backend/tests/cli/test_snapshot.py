"""curation snapshot and --source-manifest (design doc 02 §3.3, D27): a change exits 6."""
from __future__ import annotations

import json
import os

import pytest

from curation.cli import source_manifest as sm
from curation.cli.errors import SourceChanged
from curation.cli.storage import LocalStorage
from curation.contracts import schemas


def _valid(doc: dict) -> dict:
    assert schemas.errors("cli/source-manifest.schema.json", doc) == []
    return doc


def _keys(doc: dict) -> list[str]:
    return [o["key"] for o in doc["objects"]]


META = ["meta/episodes.jsonl", "meta/info.json", "meta/tasks.jsonl"]


def _episode_keys(ep: int) -> list[str]:
    return [f"data/chunk-000/episode_{ep:06d}.parquet",
            f"videos/chunk-000/observation.images.exterior/episode_{ep:06d}.mp4",
            f"videos/chunk-000/observation.images.wrist/episode_{ep:06d}.mp4"]


#: the data files v1 reads to resolve the dataset semantics (its first 100 episodes)
SAMPLE = [f"data/chunk-000/episode_{ep:06d}.parquet" for ep in range(8)]


def _selected(*eps: int) -> list[str]:
    return sorted(set(META + SAMPLE + [k for ep in eps for k in _episode_keys(ep)]))


def test_snapshot_lists_meta_and_every_episode(cli, dataset, tmp_path):
    out = tmp_path / "run" / "source_manifest.json"
    res = cli("snapshot", "--input", dataset, "--out", str(out))
    assert res.rc == 0
    doc = _valid(res.doc)
    assert json.loads(out.read_text()) == doc             # the file is what was printed
    assert doc["input"] == os.path.abspath(dataset)
    assert sorted(_keys(doc)) == sorted(META + [k for ep in range(8) for k in _episode_keys(ep)])
    assert all("mtime_ns" in o and "etag" not in o for o in doc["objects"])
    sizes = {k: os.path.getsize(os.path.join(dataset, k)) for k in _keys(doc)}
    assert [o["size"] for o in doc["objects"]] == [sizes[k] for k in _keys(doc)]
    assert doc["summary"]["count"] == len(doc["objects"])
    assert doc["summary"]["bytes"] == sum(sizes.values())
    again = cli("snapshot", "--input", dataset, "--out", str(tmp_path / "again.json")).doc
    assert again["summary"]["digest"] == doc["summary"]["digest"]      # deterministic


def test_snapshot_of_selected_episodes(cli, dataset, tmp_path):
    doc = _valid(cli("snapshot", "--input", dataset, "--episodes", "0-2,7",
                     "--out", str(tmp_path / "m.json")).doc)
    assert sorted(_keys(doc)) == _selected(0, 1, 2, 7)
    listfile = tmp_path / "eps.txt"
    listfile.write_text("# survivors\n3\n\n5-6\n")
    doc = _valid(cli("snapshot", "--input", dataset, "--episodes", f"@{listfile}",
                     "--out", str(tmp_path / "m2.json")).doc)
    assert sorted(_keys(doc)) == _selected(3, 5, 6)


def test_the_semantics_sample_is_v1s():
    """snapshot records the data v1 resolves the semantics from; the CLI keeps its own
    copy of the sample size so it never imports the numeric reader."""
    from curation.cli import lerobot_meta
    from curation.ingest import lerobot_reader

    assert lerobot_meta.SEMANTICS_SAMPLE == lerobot_reader.SEMANTICS_VOTE_EPISODES


def test_episode_selection_errors(cli, dataset, tmp_path):
    res = cli("snapshot", "--input", dataset, "--episodes", "50-60", "--out",
              str(tmp_path / "m.json"))
    assert res.rc == 2 and "none of the requested episodes exists" in res.doc["error"]["message"]
    assert not (tmp_path / "m.json").exists()
    res = cli("snapshot", "--input", dataset, "--episodes", "6-9", "--out",
              str(tmp_path / "m.json"))
    assert res.rc == 0
    assert any(e["kind"] == "log" and e["level"] == "warn" and "2 of the 4 requested" in e["msg"]
               for e in res.events)
    for bad in ("5-3", "-1", "x", "0-2000000"):
        assert cli("snapshot", "--input", dataset, "--episodes", bad,
                   "--out", str(tmp_path / "m.json")).rc == 2
    assert cli("snapshot", "--input", dataset, "--episodes", f"@{tmp_path}/none.txt",
               "--out", str(tmp_path / "m.json")).rc == 2
    assert cli("snapshot", "--input", dataset, "--out", "tos://bkt/x.json").rc == 2


def test_files_missing_from_the_dataset_are_left_out_with_a_warning(cli, dataset, tmp_path):
    """v1's rule (D40): a LeRobot v2 episode without its parquet or a camera's video is
    left out - listed in skipped_episodes with what it lacks, none of its objects kept."""
    os.remove(os.path.join(dataset, _episode_keys(4)[2]))
    res = cli("snapshot", "--input", dataset, "--out", str(tmp_path / "m.json"))
    assert res.rc == 0
    doc = _valid(res.doc)
    assert doc["skipped_episodes"] == [{"episode_index": 4, "missing": [_episode_keys(4)[2]]}]
    assert not set(_episode_keys(4)) & set(_keys(doc))   # never read: nothing to pin
    assert any("left out like v1 does" in e.get("msg", "") for e in res.events)
    complete = _valid(cli("snapshot", "--input", dataset, "--episodes", "0-3",
                          "--out", str(tmp_path / "m2.json")).doc)
    assert "skipped_episodes" not in complete


def test_not_a_lerobot_dataset(cli, tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "episode_0.rrd").write_bytes(b"x")
    res = cli("snapshot", "--input", str(tmp_path / "in"), "--out", str(tmp_path / "m.json"))
    assert res.rc == 2 and "not a LeRobot dataset" in res.doc["error"]["message"]


def test_snapshot_on_tos_records_etags(cli, cloud, mini_dataset, tmp_path, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "datasets/mini")
    monkeypatch.setenv("CURATION_INPUT_TOS_ACCESS_KEY", "in-ak")
    monkeypatch.setenv("CURATION_INPUT_TOS_SECRET_KEY", "in-sk")
    doc = _valid(cli("snapshot", "--input", "tos://src-bucket/datasets/mini/", "--episodes",
                     "1", "--out", str(tmp_path / "m.json")).doc)
    assert doc["input"] == "tos://src-bucket/datasets/mini"
    assert sorted(_keys(doc)) == _selected(1)
    info = next(o for o in doc["objects"] if o["key"] == "meta/info.json")
    data = cloud.buckets["src-bucket"]["datasets/mini/meta/info.json"]
    assert info == {"key": "meta/info.json", "size": len(data), "etag": cloud.etag(data)}
    # listing only: nothing but meta files was downloaded
    gets = {c[2] for c in cloud.calls if c[0] == "get"}
    assert gets <= {f"datasets/mini/{k}" for k in META}


# ---------------------------------------------------------------- --source-manifest


@pytest.fixture
def pinned(cli, dataset, tmp_path):
    path = tmp_path / "run" / "source_manifest.json"
    assert cli("snapshot", "--input", dataset, "--out", str(path)).rc == 0
    return str(path)


def test_unchanged_source_passes(cli, dataset, pinned):
    assert cli("preflight", "--input", dataset, "--source-manifest", pinned).rc == 0


@pytest.mark.parametrize("change", ["append", "touch", "delete", "add"])
def test_changed_metadata_exits_6(cli, dataset, pinned, change):
    target = os.path.join(dataset, "meta", "episodes.jsonl")
    if change == "append":
        with open(target, "a") as fh:
            fh.write('{"episode_index": 8, "tasks": [], "length": 1}\n')
    elif change == "touch":
        st = os.stat(target)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    elif change == "delete":
        os.remove(os.path.join(dataset, "meta", "tasks.jsonl"))
    else:
        with open(os.path.join(dataset, "meta", "stats.json"), "w") as fh:
            fh.write("{}")
    res = cli("preflight", "--input", dataset, "--source-manifest", pinned)
    assert res.rc == 6
    err = res.doc["error"]
    assert err["code"] == "source_changed"
    expected_key = {"append": "meta/episodes.jsonl", "touch": "meta/episodes.jsonl",
                    "delete": "meta/tasks.jsonl", "add": "meta/stats.json"}[change]
    assert err["details"]["key"] == expected_key
    assert err["details"]["change"] == {"append": "size", "touch": "mtime", "delete": "missing",
                                        "add": "added"}[change]


def test_rewritten_tos_object_exits_6(cli, cloud, mini_dataset, tmp_path, monkeypatch):
    cloud.upload_dir(mini_dataset, "src-bucket", "mini")
    monkeypatch.setenv("TOS_ACCESS_KEY", "ak")
    monkeypatch.setenv("TOS_SECRET_KEY", "sk")
    path = str(tmp_path / "m.json")
    assert cli("snapshot", "--input", "tos://src-bucket/mini", "--out", path).rc == 0
    assert cli("preflight", "--input", "tos://src-bucket/mini", "--source-manifest", path).rc == 0
    key = "mini/meta/info.json"
    before = cloud.buckets["src-bucket"][key]
    after = before.replace(b'"franka"', b'"frankb"')   # same size, new content
    assert len(after) == len(before) and after != before
    cloud.buckets["src-bucket"][key] = after
    res = cli("preflight", "--input", "tos://src-bucket/mini", "--source-manifest", path)
    assert res.rc == 6
    assert res.doc["error"]["details"]["key"] == "meta/info.json"
    assert res.doc["error"]["details"]["change"] == "etag"


def test_preflight_checks_only_what_it_reads(cli, dataset, pinned):
    """Preflight reads meta/ only; a changed video is caught by the commands reading it."""
    video = os.path.join(dataset, _episode_keys(0)[1])
    with open(video, "ab") as fh:
        fh.write(b"\0")
    assert cli("preflight", "--input", dataset, "--source-manifest", pinned).rc == 0
    manifest = sm.SourceManifest.load(pinned)
    listing = LocalStorage(dataset).list()
    manifest.verify(listing, keys=[_episode_keys(1)[0]])           # untouched episode: fine
    with pytest.raises(SourceChanged) as info:
        manifest.verify(listing)                                     # whole manifest
    assert info.value.details["key"] == _episode_keys(0)[1]
    with pytest.raises(SourceChanged) as info:
        manifest.verify(listing, keys=["data/chunk-000/episode_000099.parquet"])
    assert info.value.details["change"] == "not_in_manifest"


def test_manifest_of_another_input_or_a_broken_one_is_a_usage_error(cli, dataset, pinned,
                                                                    tmp_path, mini_dataset):
    res = cli("preflight", "--input", mini_dataset, "--source-manifest", pinned)
    assert res.rc == 2 and "was taken for" in res.doc["error"]["message"]
    broken = tmp_path / "broken.json"
    broken.write_text('{"schema_version": "1.0", "input": "/x", "objects": [{"key": "a"}]}')
    res = cli("preflight", "--input", dataset, "--source-manifest", str(broken))
    assert res.rc == 2 and "not a source manifest" in res.doc["error"]["message"]
    res = cli("preflight", "--input", dataset, "--source-manifest", str(tmp_path / "nope.json"))
    assert res.rc == 2
