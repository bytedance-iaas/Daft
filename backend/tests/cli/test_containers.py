"""mcap and lance datasets through the v2 commands (D44; v1's PR #155).

The synthetic datasets are the LeRobot fixture's eight episodes in the two formats
(``parity.fixtures.make_mini_mcap`` / ``make_mini_lance``), so the verdicts must be the
LeRobot chain's: ep 2 (timestamp jump) and ep 5 (fragment) fail the numeric gate, ep 7 is
ep 3's byte copy (dedup), eps 4 and 6 have no task text (autolabel). v1 itself on the same
data is checked bit for bit by ``tools/parity/tests/test_containers_parity.py``.

Local datasets run the whole Daemon order; datasets on a fake TOS check what is read
from the bucket (summaries and meta only at preflight, each object once into the source
cache, nothing written back), the cache's lifecycle and the source-change exit (6).
"""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("mcap", reason="mcap is needed for the mcap format")
pytest.importorskip("mcap_ros2", reason="mcap-ros2-support decodes the fixture's cdr")
pytest.importorskip("lance", reason="pylance is needed for the lance format")

from curation.contracts import schemas  # noqa: E402

from .conftest import ENV_VARS  # noqa: E402
from .fakes import FakeCloud  # noqa: E402
from .fakevlm_server import FakeVlmServer  # noqa: E402
from .pipeline import Chain, read_jsonl, results, run  # noqa: E402

EPISODES = "0-7"
PASSED, REJECT = [0, 1, 3, 4, 6], [2, 5, 7]
DATASET_DIR = {"mcap": "mcap_curated", "lance": "lance_episodes"}


@pytest.fixture(scope="session")
def mini_mcap(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_mcap

    return make_mini_mcap(str(tmp_path_factory.mktemp("mcap") / "mini_mcap"))


@pytest.fixture(scope="session")
def mini_lance(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_lance

    return make_mini_lance(str(tmp_path_factory.mktemp("lance") / "mini_lance"))


def _datasets(mini_mcap, mini_lance) -> dict:
    return {"mcap": mini_mcap, "lance": mini_lance}


def _list(run_dir: str, name: str, rev: int = 1) -> list[int]:
    with open(os.path.join(run_dir, "revisions", f"r{rev:04d}", f"{name}.json")) as fh:
        return sorted(int(e["episode_index"]) for e in json.load(fh)["episodes"])


# ---------------------------------------------------------------- preflight


@pytest.mark.parametrize("fmt", ["mcap", "lance"])
def test_preflight_reads_the_format(cli, mini_mcap, mini_lance, fmt):
    doc = cli("preflight", "--input", _datasets(mini_mcap, mini_lance)[fmt],
              "--vlm-backend", "ark").doc
    assert not schemas.errors("cli/preflight.schema.json", doc)
    assert doc["format"]["kind"] == fmt and doc["format"]["supported"] is True
    assert doc["format"]["version"] == ("v3" if fmt == "lance" else None)
    ds = doc["dataset"]
    assert ds["episode_count"] == 8 and ds["robot_type"] == "franka"
    assert sorted(ds["cameras"]) == ["exterior", "wrist"]
    assert ds["labels"] == {"with_task": 6, "without_task": 2}
    assert ds["fps"] == (None if fmt == "mcap" else 15.0)      # mcap: log_time, no fps
    assert ds["total_frames"] == 516
    by = {m["id"]: m for m in doc["modules"]}
    for m in ("timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
              "video_action_sync", "task_success", "dedup", "skill_profile"):
        assert by[m]["availability"] == "available", by[m]
    eef = by["eef_video_consistency"]
    if fmt == "mcap":         # F5.13: it reads mcap image topics; the dataset preflight asks for the file
        assert (eef["availability"], eef["reason_code"]) == ("needs_input", "trajectory_missing")
    else:
        assert eef["availability"] == "unsupported"
        assert eef["reason_code"] == "format_unsupported_by_module"
        assert eef["reason_args"] == {"format": fmt}


def test_preflight_mcap_robot_type_needs_input_without_metadata(cli, mini_mcap, tmp_path):
    """No robot_type metadata record: kinematic_limits asks, like a LeRobot dataset
    without robot_type (the fixture's files are rewritten without the record)."""
    from parity import fixtures

    src = tmp_path / "no_robot"
    src.mkdir()
    for ep in (0, 1):
        fixtures._write_episode_mcap(str(src / f"episode_{ep}.mcap"), ep, robot_type=None)
    doc = cli("preflight", "--input", str(src), "--vlm-backend", "ark").doc
    km = next(m for m in doc["modules"] if m["id"] == "kinematic_limits")
    assert km["availability"] == "needs_input" and km["reason_code"] == "robot_type_unknown"
    assert "mcap metadata records" in km["reason"]
    doc = cli("preflight", "--input", str(src), "--embodiment-id", "franka").doc
    assert next(m for m in doc["modules"]
                if m["id"] == "kinematic_limits")["availability"] == "available"


def test_preflight_mcap_topic_mapping(cli, mini_mcap):
    """The site's ingest.mcap_mapping decides the action topic (v1's rule)."""
    doc = cli("preflight", "--input", mini_mcap,
              "--set", "ingest.mcap_mapping={action: /nope}").doc
    assert doc["format"]["supported"] is False
    assert "/nope" in doc["validation"][0] and "/action" in doc["validation"][0]
    assert {m["reason_code"] for m in doc["modules"]} == {"metadata_invalid"}
    doc = cli("preflight", "--input", mini_mcap,
              "--set", "ingest.mcap_mapping={action: /observation.state}").doc
    assert doc["format"]["supported"] is True
    assert "ingest.mcap_mapping" in doc["format"]["detail"]


@pytest.mark.parametrize("fmt", ["mcap", "lance"])
def test_preflight_format_switched_off(cli, mini_mcap, mini_lance, fmt):
    doc = cli("preflight", "--input", _datasets(mini_mcap, mini_lance)[fmt],
              "--set", f"ingest.{fmt}_enabled=false").doc
    assert not schemas.errors("cli/preflight.schema.json", doc)
    assert doc["format"]["kind"] == fmt and doc["format"]["supported"] is False
    assert f"ingest.{fmt}_enabled" in doc["format"]["detail"]
    assert {m["reason_code"] for m in doc["modules"]} == {"format_disabled"}
    res = cli("snapshot", "--input", _datasets(mini_mcap, mini_lance)[fmt],
              "--set", f"ingest.{fmt}_enabled=false", "--out", "/dev/null")
    assert res.rc == 2 and f"ingest.{fmt}_enabled" in res.doc["error"]["message"]


def test_preflight_lance_without_the_stamp_is_invalid(cli, tmp_path):
    """Three tables without storage_format "lance": not lerobot-lance-convert >= 0.3.0."""
    from parity.fixtures import make_mini_lance

    root = make_mini_lance(str(tmp_path / "old"))
    info_path = os.path.join(root, "meta", "info.json")
    with open(info_path) as fh:
        info = json.load(fh)
    info.pop("storage_format")
    with open(info_path, "w") as fh:
        json.dump(info, fh)
    doc = cli("preflight", "--input", root).doc
    assert doc["format"]["kind"] == "lance" and doc["format"]["supported"] is False
    assert "storage_format" in doc["validation"][0]
    assert {m["reason_code"] for m in doc["modules"]} == {"metadata_invalid"}


def test_preflight_lance_meta_from_the_mirror(cli, tmp_path):
    from parity.fixtures import make_mini_lance

    root = make_mini_lance(str(tmp_path / "tables_only"), meta_dir=False)
    assert not os.path.exists(os.path.join(root, "meta"))
    doc = cli("preflight", "--input", root).doc
    assert doc["format"]["supported"] is True and doc["dataset"]["episode_count"] == 8
    assert any("meta.lance" in w for w in doc["warnings"])


def test_snapshot_records_what_stands_for_the_metadata(cli, mini_mcap, mini_lance, tmp_path):
    doc = cli("snapshot", "--input", mini_mcap, "--episodes", "1",
              "--out", str(tmp_path / "m.json")).doc
    # every episode file, whatever the selection: the numbering depends on all of them
    assert [o["key"] for o in doc["objects"]] == [f"episode_{i}.mcap" for i in range(8)]
    doc = cli("snapshot", "--input", mini_lance, "--out", str(tmp_path / "l.json")).doc
    keys = [o["key"] for o in doc["objects"]]
    assert "meta/info.json" in keys
    assert {k.split("/")[0] for k in keys} == {"meta", "frames.lance", "videos.lance",
                                               "meta.lance"}


# ---------------------------------------------------------------- local chains


@pytest.fixture(scope="module", params=["mcap", "lance"])
def chain(request, tmp_path_factory, mini_mcap, mini_lance):
    fmt = request.param
    tmp = tmp_path_factory.mktemp(f"chain-{fmt}")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        mp.setenv("TMPDIR", str(tmp / "tmp"))        # the readers' muxed / extracted videos
        os.makedirs(tmp / "tmp")
        import tempfile

        tempfile.tempdir = None
        try:
            with FakeVlmServer() as vlm:
                c = Chain(_datasets(mini_mcap, mini_lance)[fmt], str(tmp / "run"), vlm.url,
                          extra_source=["--selection", EPISODES])
                c.front()
                c.funnel()
                c.post()
                c.deliver(str(tmp / "delivery"))
        finally:
            tempfile.tempdir = None
        c.fmt, c.delivery, c.tmp = fmt, str(tmp / "delivery"), str(tmp / "tmp")
        yield c


def test_chain_verdicts_are_the_lerobot_fixtures(chain):
    assert _list(chain.rd, "passed") == PASSED
    assert _list(chain.rd, "reject") == REJECT
    assert _list(chain.rd, "held") == []
    ts = results(chain.rd, "timestamp_check")
    assert sorted(e for e, r in ts.items() if r["verdict"] == "fail") == [2, 5]
    assert results(chain.rd, "dedup")[7]["verdict"] == "fail"
    captions = {line["episode_index"] for line in
                read_jsonl(os.path.join(chain.rd, "autolabel", "captions.jsonl"))}
    assert captions == {4, 6}
    assert all(r["verdict"] != "error" for m in ("timestamp_check", "kinematic_limits",
                                                  "motion_quality", "visual_quality",
                                                  "video_action_sync", "task_success")
               for r in results(chain.rd, m).values())


def test_chain_semantics_come_from_the_selection(chain):
    """Every stage attaches the semantics v1 resolves on the task's selection, however
    few survivors it reads (the frame stage reads 6 of 8)."""
    rec = results(chain.rd, "motion_quality")[0]
    assert rec["verdict"] in ("pass", "fail", "scored", "abstain")
    assert chain.steps["frame"].doc["modules"]["visual_quality"]["episodes"]["total"] == 6


def test_chain_delivers_the_format(chain, mini_mcap):
    exp = chain.steps["export"].doc
    assert exp["format"] == chain.fmt and exp["incremental"] is False
    assert exp["dataset_dir"] == f"export/{DATASET_DIR[chain.fmt]}"
    root = os.path.join(chain.delivery, "export", DATASET_DIR[chain.fmt])
    with open(os.path.join(root, "index.json")) as fh:
        index = json.load(fh)
    with open(os.path.join(chain.delivery, "export", "manifest.json")) as fh:
        manifest = json.load(fh)
    assert not schemas.errors("cli/export-manifest.schema.json", manifest)
    assert manifest["dataset_dir"] == DATASET_DIR[chain.fmt]
    assert [e["episode_index"] for e in manifest["episodes"]] == PASSED
    if chain.fmt == "mcap":
        assert sorted(os.listdir(root)) == sorted([f"episode_{i}.mcap" for i in PASSED]
                                                  + ["index.json"])
        for i in PASSED:                          # byte for byte the source's
            with open(os.path.join(root, f"episode_{i}.mcap"), "rb") as a, \
                    open(os.path.join(mini_mcap, f"episode_{i}.mcap"), "rb") as b:
                assert a.read() == b.read()
        by = {r["episode_id"]: r for r in index["episodes"]}
        assert by["ep000004"]["instruction_source"] == "自产caption"
        assert by["ep000004"]["relabeled"] is True and by["ep000000"]["relabeled"] is False
    else:
        import pandas as pd

        assert "原格式交付本版本未做" in index["说明"] and "note" in exp
        df = pd.read_parquet(os.path.join(root, "episodes_parquet"))
        assert sorted(df["episode_id"]) == [f"ep{i:06d}" for i in PASSED]
        src = dict(zip(df["episode_id"], df["instruction_source"]))
        assert src["ep000004"] == "自产caption" and src["ep000000"] == "原始标注"
        for video in df["video"]:                 # pointers into the delivery
            for v in video.values():
                assert v["path"].startswith(chain.delivery) and os.path.isfile(v["path"])
    verify = chain.steps["verify"].doc
    assert verify["failed"] == [] and verify["complete_marker"] is True


def test_chain_leaves_no_temporary_videos(chain):
    assert os.listdir(chain.tmp) == []


def test_a_second_export_writes_nothing_new(chain, tmp_path):
    """Always a full export for these formats; unchanged bytes are not uploaded again and
    --incremental says why it did not build on the previous export."""
    res = run("export", "--run-dir", chain.rd, "--input", chain.ds, "--output", chain.delivery,
              "--incremental")
    assert res.rc == 0 and res.doc["incremental"] is False
    assert "LeRobot" in res.doc["full_reason"]
    assert res.doc["diff"] == {"keep": 5, "relabel": 0, "renumber": 0, "add": 0, "drop": 0}
    import re

    logs = " ".join(e.get("msg", "") for e in res.events)
    up, gone = map(int, re.search(r"(\d+) file\(s\) uploaded, (\d+) deleted", logs).groups())
    # mcap: at most index.json (its generated_at, when the second ticked), never a .mcap;
    # lance: daft names its parquet part anew each time (one in, one out), no video again
    assert (up, gone) in ([(0, 0), (1, 0)] if chain.fmt == "mcap" else [(1, 1)])


def test_chain_report_notes_the_container(chain):
    with open(os.path.join(chain.rd, "revisions", "r0001", "report.json")) as fh:
        report = json.load(fh)
    container = report["integrity"]["container"]
    assert container["format"] == chain.fmt
    assert container["delivery"]
    with open(os.path.join(chain.rd, "revisions", "r0001", "report.md"), encoding="utf-8") as fh:
        md = fh.read()
    if chain.fmt == "lance":
        assert "原格式交付本版本未做" in md
    else:
        assert "mcap_curated" in md


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


def _gets(cloud: FakeCloud, prefix: str) -> list[tuple]:
    return [c for c in cloud.calls if c[0] == "get" and c[2].startswith(prefix)]


@pytest.mark.parametrize("fmt", ["mcap", "lance"])
def test_tos_preflight_reads_metadata_only(cli, tos, mini_mcap, mini_lance, fmt):
    tos.upload_dir(_datasets(mini_mcap, mini_lance)[fmt], "src", f"ds/{fmt}")
    doc = cli("preflight", "--input", f"tos://src/ds/{fmt}", "--vlm-backend", "ark").doc
    assert doc["format"]["supported"] is True and doc["dataset"]["episode_count"] == 8
    gets = _gets(tos, f"ds/{fmt}/")
    if fmt == "mcap":         # summaries: ranged reads (a small file in one), no full GET
        assert gets and all(c[3] is not None for c in gets)
        assert len(gets) == 8 and {c[2] for c in gets} == {f"ds/mcap/episode_{i}.mcap"
                                                           for i in range(8)}
    else:
        assert {c[2].split("/", 2)[2].split("/")[0] for c in gets} == {"meta"}


@pytest.mark.parametrize("fmt", ["mcap", "lance"])
def test_tos_source_cache(cli, tos, mini_mcap, mini_lance, fmt, tmp_path, monkeypatch):
    """Each object is copied once into the task's cache and read from there by every later
    command; the source bucket is only read."""
    tos.upload_dir(_datasets(mini_mcap, mini_lance)[fmt], "src", f"ds/{fmt}")
    uri = f"tos://src/ds/{fmt}"
    cache = tmp_path / "cache"
    monkeypatch.setenv("CURATION_SOURCE_CACHE", str(cache))
    run_dir = tmp_path / "run"
    sm = str(run_dir / "source_manifest.json")
    assert cli("snapshot", "--input", uri, "--out", sm).rc == 0
    common = ["--input", uri, "--run-dir", str(run_dir), "--source-manifest", sm,
              "--selection", EPISODES]
    num = cli("check", "--modules", "timestamp_check,kinematic_limits,motion_quality", *common,
              "--episodes", EPISODES, "--survivors-out", str(run_dir / "numeric.txt"))
    assert num.rc == 0, num.doc
    assert num.doc["modules"]["timestamp_check"]["episodes"]["fail"] == 2
    def data_gets():                             # whole objects that are episode data
        return [c[2] for c in _gets(tos, f"ds/{fmt}/")
                if c[3] is None and not c[2].startswith(f"ds/{fmt}/meta/")]

    objects = [k for k in tos.buckets["src"] if k.startswith(f"ds/{fmt}/")
               and not k.startswith(f"ds/{fmt}/meta/")]
    assert sorted(data_gets()) == sorted(objects)          # each once (lance: the tables)
    frame = cli("check", "--modules", "visual_quality,video_action_sync", *common,
                "--episodes", f"@{run_dir / 'numeric.txt'}")
    assert frame.rc == 0, frame.doc
    assert frame.doc["modules"]["visual_quality"]["episodes"]["error"] == 0
    assert sorted(data_gets()) == sorted(objects)          # the cache served the second one
    assert not [c for c in tos.calls if c[0] in ("put", "delete") and c[1] == "src"]
    assert os.listdir(cache) and os.listdir(tmp_path / "tmp") == []


def test_tos_cache_of_one_command_is_removed(cli, tos, mini_mcap, tmp_path):
    tos.upload_dir(mini_mcap, "src", "ds/mcap")
    res = cli("check", "--modules", "timestamp_check", "--input", "tos://src/ds/mcap",
              "--run-dir", str(tmp_path / "run"), "--episodes", "0-1")
    assert res.rc == 0
    assert os.listdir(tmp_path / "tmp") == []    # no CURATION_SOURCE_CACHE: a temporary one


def test_tos_changed_object_exits_6(cli, tos, mini_mcap, tmp_path):
    tos.upload_dir(mini_mcap, "src", "ds/mcap")
    sm = str(tmp_path / "sm.json")
    assert cli("snapshot", "--input", "tos://src/ds/mcap", "--out", sm).rc == 0
    tos.buckets["src"]["ds/mcap/episode_3.mcap"] += b"\0"
    res = cli("check", "--modules", "timestamp_check", "--input", "tos://src/ds/mcap",
              "--run-dir", str(tmp_path / "run"), "--source-manifest", sm, "--episodes", "3")
    assert res.rc == 6 and res.doc["error"]["details"]["key"] == "episode_3.mcap"
    tos.buckets["src"]["ds/mcap/episode_9.mcap"] = tos.buckets["src"]["ds/mcap/episode_0.mcap"]
    del tos.buckets["src"]["ds/mcap/episode_3.mcap"]
    tos.buckets["src"]["ds/mcap/episode_3.mcap"] = tos.buckets["src"]["ds/mcap/episode_0.mcap"]
    res = cli("check", "--modules", "timestamp_check", "--input", "tos://src/ds/mcap",
              "--run-dir", str(tmp_path / "run"), "--source-manifest", sm, "--episodes", "1")
    assert res.rc == 6                            # a new episode file: the dataset changed
    assert res.doc["error"]["details"]["change"] == "added"


def test_tos_export_mcap(cli, tos, mini_mcap, tmp_path, monkeypatch):
    """Export of a remote mcap dataset: the kept files come through the cache, byte for byte."""
    from .pipeline import Chain

    tos.upload_dir(mini_mcap, "src", "ds/mcap")
    monkeypatch.setenv("CURATION_SOURCE_CACHE", str(tmp_path / "cache"))
    with FakeVlmServer() as vlm:
        c = Chain("tos://src/ds/mcap", str(tmp_path / "run"), vlm.url,
                  extra_source=["--selection", EPISODES])
        c.front()
        c.funnel()
        c.post()
        c.deliver(str(tmp_path / "delivery"))
    assert _list(c.rd, "passed") == PASSED
    root = tmp_path / "delivery" / "export" / "mcap_curated"
    for i in PASSED:
        assert (root / f"episode_{i}.mcap").read_bytes() == \
            tos.buckets["src"][f"ds/mcap/episode_{i}.mcap"]
    assert c.steps["verify"].doc["complete_marker"] is True


# ---------------------------------------------------------------- the helpers


def test_mcap_numbering_is_v1s(tmp_path):
    from curation.cli.containers import mcap_episodes

    names = {"episode_3.mcap": 1, "episode_10.mcap": 1, "calib.mcap": 1, "x/episode_0.mcap": 1}
    assert mcap_episodes(names) == {3: "episode_3.mcap", 10: "episode_10.mcap"}
    assert mcap_episodes({"b.mcap": 1, "a.mcap": 1}) == {0: "a.mcap", 1: "b.mcap"}
    assert mcap_episodes({"README.md": 1}) == {}


def test_range_file_reads_across_blocks():
    from curation.cli.containers import RangeFile

    data = bytes(range(256)) * 50
    calls = []

    def read_range(start, n):
        calls.append((start, n))
        return data[start:start + n]

    f = RangeFile(read_range, len(data), readahead=1000, tail=500)
    f.seek(-10, os.SEEK_END)
    assert f.read(10) == data[-10:]               # the whole tail in one read
    f.seek(-400, os.SEEK_END)
    assert f.read(20) == data[-400:-380]          # cached
    f.seek(995)
    assert f.read(20) == data[995:1015]           # one read from 995 on
    f.seek(1000)
    assert f.read(5) == data[1000:1005]           # cached
    assert calls == [(len(data) - 500, 500), (995, 1000)]
    small = RangeFile(read_range, 300, tail=500)
    small.seek(0)
    assert small.read(8) == data[:8] and calls[-1] == (0, 300)   # a small file: one read


def test_cache_refuses_an_object_that_changed_while_read(tmp_path):
    from curation.cli.containers import SourceCache
    from curation.cli.errors import SourceChanged
    from curation.cli.storage import ObjectInfo

    class Store:
        remote, uri = True, "tos://b/ds"

        def download(self, key, path):
            with open(path, "wb") as fh:
                fh.write(b"new bytes")
            return ObjectInfo(key, 9, etag='"other"')

    listing = {"episode_0.mcap": ObjectInfo("episode_0.mcap", 9, etag='"old"')}
    cache = SourceCache(Store(), listing, "mcap", root=str(tmp_path))
    with pytest.raises(SourceChanged):
        cache.fetch(["episode_0.mcap"])
    assert os.path.getsize(os.path.join(cache.data, "episode_0.mcap")) == 0   # the stand-in
