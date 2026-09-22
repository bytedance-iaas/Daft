"""C2 1.1 fields of the export: each episode's ``task``, the top-level ``files``,
``videos_renamed`` and ``full_reason`` (docs/contracts/SUMMARY.md, section 5)."""
from __future__ import annotations

import hashlib
import json
import os

from curation.contracts import schemas
from curation.export.incremental import export_run
from curation.export.manifest import check_manifest

from .helpers import V2_TASK, make_revision, quiet, v2_entries


def _manifest(run: str) -> dict:
    with open(os.path.join(run, "export", "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_manifest_lists_each_task_and_every_file(v2_source, tmp_path):
    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1, 4], relabel={1: "put the block on the shelf"}))
    o = export_run(run, v2_source, incremental=False, log=quiet)
    man = _manifest(run)
    assert schemas.errors("cli/export-manifest.schema.json", man) == []
    assert check_manifest(man) == []
    tasks = {e["episode_index"]: e["task"] for e in man["episodes"]}
    assert tasks[0] == {"text": V2_TASK, "source": "原始标注"}
    assert tasks[1] == {"text": "put the block on the shelf", "source": "人工改标"}
    assert tasks[4]["source"] == "自产caption" and tasks[4]["text"]
    root = os.path.join(run, "export", "lerobot_curated")
    on_disk = {os.path.relpath(os.path.join(d, n), root).replace(os.sep, "/")
               for d, _, names in os.walk(root) for n in names}
    assert set(man["files"]) == on_disk
    for rel, rec in man["files"].items():
        data = open(os.path.join(root, rel), "rb").read()
        assert rec == {"size": len(data),
                       "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}, rel
    assert o.result["full_reason"] is None and o.result["videos_renamed"] == 0
    assert schemas.errors("cli/export.schema.json", o.result) == []


def test_full_reason_and_renamed_videos(v2_source, tmp_path):
    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1, 3, 4]))
    o = export_run(run, v2_source, incremental=True, log=quiet)
    assert o.result["incremental"] is False and o.result["full_reason"]   # nothing to build on
    make_revision(run, 2, v2_entries([0, 3, 4]))                          # drop a middle one
    o = export_run(run, v2_source, incremental=True, log=quiet)
    assert o.result["incremental"] is True and o.result["full_reason"] is None
    assert o.result["videos_renamed"] == 4 and o.result["videos_copied"] == 0
    assert schemas.errors("cli/export.schema.json", o.result) == []
    o = export_run(run, v2_source, incremental=False, log=quiet)
    assert o.result["full_reason"] is None                                # not asked for


def test_old_manifests_without_the_new_fields_still_load():
    man = {"schema_version": "1.0", "source_format": "lerobot_v2",
           "fingerprint": "sha256:" + "0" * 64, "meta_files": [],
           "episodes": [{"episode_index": 0, "new_index": 0,
                         "content_key": "sha256:" + "1" * 64, "task_key": "sha256:" + "2" * 64,
                         "artifacts": {"parquet": "data/x.parquet", "videos": {}}}]}
    assert check_manifest(man) == []
    bad = dict(man, files={"a": {"size": -1, "sha256": "x"}})
    assert check_manifest(bad)
