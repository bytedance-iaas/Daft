"""A ``tos://`` source (a local directory behind a fake TOS client, no network).

Checks the parts that differ from a local source: identities are ETags (as in
``source_manifest.json`` for TOS), videos are downloaded to local scratch and then
copied whole, and the delivered bytes equal the local export's.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from types import SimpleNamespace

import pytest

from curation.export.incremental import SourceChangedError, export_dataset
from curation.ingest import dsfs

from .helpers import export_v3, final_list, quiet, snapshot, v2_entries


class _Bucket:
    """A directory posing as bucket ``bkt`` (key = relative path)."""

    def __init__(self, root: str):
        self.root = root
        self.downloads: list[str] = []

    def _keys(self, prefix: str):
        out = []
        for cur, _d, names in os.walk(self.root):
            for n in names:
                k = os.path.relpath(os.path.join(cur, n), self.root).replace(os.sep, "/")
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def _obj(self, k):
        data = open(os.path.join(self.root, k), "rb").read()
        return SimpleNamespace(key=k, size=len(data), etag=f'"{hashlib.md5(data).hexdigest()}"')

    def list_objects_type2(self, bucket, prefix="", delimiter=None, continuation_token=None,
                           max_keys=1000):
        return SimpleNamespace(contents=[self._obj(k) for k in self._keys(prefix)],
                               common_prefixes=[], is_truncated=False)

    def get_object(self, bucket, key):
        data = open(os.path.join(self.root, key), "rb").read()
        return SimpleNamespace(read=lambda: data)

    def get_object_to_file(self, bucket, key, local_path):
        self.downloads.append(os.path.abspath(local_path))
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        shutil.copyfile(os.path.join(self.root, key), local_path)


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    from curation import tos_store
    root = tmp_path / "bkt"
    root.mkdir()
    client = _Bucket(str(root))
    store = tos_store.TosStore("ep", "cn-beijing", client=client)
    monkeypatch.setattr(dsfs, "_store", lambda bucket=None: store)
    # PyAV reads remote videos through a presigned URL; here it gets the local file
    monkeypatch.setattr(dsfs, "media_source",
                        lambda p: os.path.join(str(root), p[len("tos://bkt/"):])
                        if dsfs.is_remote(p) else p)
    dsfs.forget()
    yield root, client
    dsfs.forget()


def _etag_manifest(client: _Bucket, prefix: str) -> dict:
    objs = [{"key": k[len(prefix) + 1:], "size": o.size, "etag": o.etag}
            for k in client._keys(prefix + "/") for o in [client._obj(k)]]
    return {"schema_version": "1.0", "input": f"tos://bkt/{prefix}", "objects": objs,
            "summary": {"count": len(objs), "bytes": sum(o["size"] for o in objs),
                        "digest": "sha256:" + "2" * 64}}


def test_v2_from_tos_matches_the_local_export(v2_source, bucket, tmp_path):
    root, client = bucket
    shutil.copytree(v2_source, root / "datasets" / "mini")
    url = "tos://bkt/datasets/mini"
    out = tmp_path / "out"
    sm = _etag_manifest(client, "datasets/mini")
    o = export_dataset(url, final_list("passed", v2_entries([0, 1, 3, 4, 6])), str(out),
                       source_manifest=sm, log=quiet)
    assert o.result["videos_copied"] == 10
    assert client.downloads and not [p for p in client.downloads if p.startswith(str(out))]
    local = export_dataset(v2_source, final_list("passed", v2_entries([0, 1, 3, 4, 6])),
                           str(tmp_path / "local"), log=quiet)
    ours = {r: s[3] for r, s in snapshot(str(out / "lerobot_curated")).items()}
    assert ours == {r: s[3] for r, s in snapshot(str(tmp_path / "local" / "lerobot_curated")).items()}
    assert o.result["fingerprint"] != local.result["fingerprint"]    # a different source digest
    # drop the middle episode: renames only, nothing downloaded
    client.downloads.clear()
    o = export_dataset(url, final_list("passed", v2_entries([0, 1, 4, 6])), str(out),
                       source_manifest=sm, log=quiet)
    assert o.result["incremental"] and o.result["videos_copied"] == 0 and not client.downloads
    # the snapshot's ETags are enforced
    bad = json.loads(json.dumps(sm))
    next(x for x in bad["objects"] if x["key"].endswith("episode_000004.mp4"))["etag"] = '"0"'
    with pytest.raises(SourceChangedError, match="episode_000004.mp4"):
        export_dataset(url, final_list("passed", v2_entries([0, 1, 4, 6])), str(out),
                       source_manifest=bad, log=quiet)


def test_v3_from_tos_matches_the_local_export(v3_source, bucket, tmp_path):
    root, _client = bucket
    shutil.copytree(v3_source, root / "v3")
    o = export_v3("tos://bkt/v3", tmp_path / "remote", [0, 1, 3, 4, 5])
    local = export_v3(v3_source, tmp_path / "local", [0, 1, 3, 4, 5])
    assert o.result["videos_reencoded"] == local.result["videos_reencoded"] > 0
    assert {r: s[3] for r, s in snapshot(str(tmp_path / "remote" / "lerobot_curated")).items()} == \
        {r: s[3] for r, s in snapshot(str(tmp_path / "local" / "lerobot_curated")).items()}
