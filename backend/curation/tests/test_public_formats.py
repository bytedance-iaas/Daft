"""The public cache bucket lists every format the platform reads (requester 2026-10-12): the format comes from the
manifest's file list alone; a readable directory under a mirrored repository is listed too."""
from __future__ import annotations

import json

import pytest

from curation import public_formats as pf
from curation.ingest import public_catalog as pc

INFO_V3 = json.dumps({"codebase_version": "v3.0", "total_episodes": 12}).encode()
INFO_V2 = json.dumps({"codebase_version": "v2.1", "total_episodes": 5}).encode()

MANIFEST = {
    "droid_100": ["dataset/droid_100/meta/info.json", "dataset/droid_100/videos/a.mp4",
                  "dataset/droid_100/.metadata.json"],
    "pusht_lance": ["dataset/pusht_lance/meta/info.json", "dataset/pusht_lance/frames.lance/data/0.lance"],
    "cups": ["dataset/cups/dataset_plan.pkl", "dataset/cups/demos/demo_1/raw_video.mp4",
             "dataset/cups/mcap/episode_000000.mcap", "dataset/cups/mcap/episode_000001.mcap",
             "dataset/cups/README.md"],
    "HABIT": ["dataset/HABIT/full/meta/info.json", "dataset/HABIT/full/videos/x.mp4",
              "dataset/HABIT/README.md"],
    "deep": ["dataset/deep/a/b/c/d/meta/info.json"],          # four levels down: not looked at
    "wiki": ["dataset/wiki/train.parquet", "dataset/wiki/README.md"],
}


class Store:
    """A bucket of the given objects; counts what is read."""

    def __init__(self, objects: dict[str, bytes], etag: str = "e1"):
        self.objects, self.etag, self.reads = objects, etag, []

    def get_bytes(self, bucket, key):
        self.reads.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    def head(self, bucket, key):
        if key == "dataset_files.json":
            return len(self.objects[key]), self.etag
        if key not in self.objects:
            raise FileNotFoundError(key)
        return len(self.objects[key]), "x"


@pytest.fixture(autouse=True)
def configured():
    pc.apply_config({"public_datasets": {"bucket": "ai-infra", "region": "cn-beijing"}})
    pf.reset()
    yield
    pc.reset()
    pf.reset()


def _store(**extra) -> Store:
    objects = {"dataset_files.json": json.dumps(MANIFEST).encode(),
               "dataset/droid_100/meta/info.json": INFO_V2,
               "dataset/droid_100/videos/a.mp4": b"\0" * 10,
               "dataset/droid_100/.metadata.json": b'{"id": "lerobot/droid_100"}',
               "dataset/pusht_lance/meta/info.json": INFO_V3,
               "dataset/HABIT/full/meta/info.json": INFO_V3,
               "dataset/HABIT/full/videos/x.mp4": b""}
    objects.update(extra)
    return Store(objects)


def test_every_readable_format_is_listed_subdirectories_included():
    got = {e["name"]: e for e in pf.catalog(store=_store())}
    assert {n: e["kind"] for n, e in got.items()} == {
        "droid_100": "lerobot", "pusht_lance": "lance", "cups": "umi_session", "cups/mcap": "mcap",
        "HABIT/full": "lerobot"}
    assert got["droid_100"]["id"] == "lerobot/droid_100"                       # the repository's HF id
    assert (got["droid_100"]["version"], got["droid_100"]["episodes"]) == ("v2.1", 5)
    assert (got["pusht_lance"]["version"], got["pusht_lance"]["episodes"]) == ("v3.0", 12)
    assert got["cups/mcap"]["episodes"] == 2 and got["cups"]["episodes"] is None
    assert got["cups"]["url"] == "tos://ai-infra/dataset/cups"
    assert got["cups/mcap"]["url"] == "tos://ai-infra/dataset/cups/mcap"
    assert got["HABIT/full"]["warning"] == "视频文件为空(镜像不完整)"
    assert "warning" not in got["pusht_lance"] and "warning" not in got["cups"]


def test_the_format_comes_from_the_file_list_alone():
    st = _store()
    pf.catalog(store=st)
    # no request for the directories it cannot read, nor to classify one
    assert not any(k.startswith(("dataset/wiki/", "dataset/deep/", "dataset/cups/")) for k in st.reads)
    assert pf.kind_of({"meta/info.json", "data/x.parquet"}) == "lerobot"
    assert pf.kind_of({"meta/info.json", "frames.lance/data/0.lance"}) == "lance"
    assert pf.kind_of({"dataset_plan.pkl", "demos/demo_1/raw_video.mp4"}) == "umi_session"
    assert pf.kind_of({"dataset_plan.pkl"}) is None                            # a plan without its demos
    assert pf.kind_of({"episode_0.mcap"}) == "mcap"
    assert pf.kind_of({"sub/episode_0.mcap"}) is None                         # the subdirectory is the dataset
    assert pf.kind_of({"train.parquet"}) is None


def test_kept_by_the_manifests_etag():
    st = _store()
    first = pf.catalog(store=st, now=0)
    st.reads.clear()
    assert pf.catalog(store=st, now=10) == first and st.reads == []            # within RECHECK_S
    assert pf.catalog(store=st, now=100) == first and st.reads == []           # ETag unchanged
    st.etag = "e2"
    pf.catalog(store=st, now=200)
    assert "dataset_files.json" in st.reads                                    # changed: read again


def test_not_configured_is_empty_and_an_unreadable_manifest_says_so():
    pc.reset()
    pc.apply_config({})
    assert pf.catalog(store=_store()) == []
    pc.apply_config({"public_datasets": {"bucket": "ai-infra", "region": "cn-beijing"}})
    with pytest.raises(pc.PublicCatalogError, match="读不到公共数据集清单"):
        pf.catalog(store=Store({}), force=True)
