"""export_run: what the future ``curation export --run-dir`` calls (D24, D35, D9)."""
from __future__ import annotations

import json
import os

import pytest

from curation.contracts import schemas
from curation.export.incremental import (ExportInputError, SourceChangedError, export_run,
                                         resolve_revision_dir)

from .helpers import V2_TASK, entry, make_revision, quiet, read_jsonl, v2_entries

REVIEW_1 = {"episode_index": 1, "current_list": "passed",
            "review": [{"source_module": "task_success", "kind": "task_verdict",
                        "reason": "证据不足，弃权"}]}
HELD_2 = {"episode_index": 2, "reasons": [{"module": "task_success", "kind": "execution_error",
                                           "text": "model call failed after retries"}]}


def delivered(run_dir) -> list[int]:
    return [r["source_episode_index"] for r in
            read_jsonl(os.path.join(run_dir, "export", "lerobot_curated", "meta",
                                    "curation_episodes.jsonl"))]


def test_pending_review_is_delivered_and_held_is_not(v2_source, tmp_path):
    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1, 3, 4, 6]), held=[HELD_2], review=[REVIEW_1])
    open(os.path.join(run, "_COMPLETE"), "w").close()
    logs = []
    o = export_run(run, v2_source, log=lambda lvl, m: logs.append(m))
    assert schemas.errors("cli/export.schema.json", o.result) == []
    assert o.result["manifest"] == "export/manifest.json"
    assert delivered(run) == [0, 1, 3, 4, 6]                  # 1 waits for a human, still delivered
    assert 2 not in delivered(run)                             # 2 waits for a retry: not delivered
    assert not os.path.exists(os.path.join(run, "_COMPLETE"))  # verify writes it again
    assert any("waiting for a human decision" in m for m in logs)
    man = json.load(open(os.path.join(run, "export", "manifest.json"), encoding="utf-8"))
    assert [e["episode_index"] for e in man["episodes"]] == [0, 1, 3, 4, 6]


def test_adjudication_then_reexport_is_incremental(v2_source, tmp_path):
    """Episode 3 judged failed and episode 1 relabelled: the next revision drops one
    middle episode and relabels one; nothing else is exported again."""
    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1, 3, 4, 6]), held=[HELD_2], review=[REVIEW_1])
    export_run(run, v2_source, log=quiet)
    make_revision(run, 2, v2_entries([0, 1, 4, 6], relabel={1: "put the block on the shelf"}),
                  held=[HELD_2])
    o = export_run(run, v2_source, log=quiet)
    assert o.result["incremental"] is True
    assert o.result["diff"] == {"keep": 1, "relabel": 1, "renumber": 2, "add": 0, "drop": 1}
    assert o.result["videos_copied"] == 0 and o.result["videos_reencoded"] == 0
    assert delivered(run) == [0, 1, 4, 6]
    # the held episode comes back after a successful retry
    make_revision(run, 3, v2_entries([0, 1, 2, 4, 6], relabel={1: "put the block on the shelf"}))
    o = export_run(run, v2_source, log=quiet)
    assert o.result["diff"]["add"] == 1 and o.result["videos_copied"] == 2
    assert delivered(run) == [0, 1, 2, 4, 6]


def test_only_committed_revisions_are_exported(v2_source, tmp_path):
    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1]))
    make_revision(run, 2, v2_entries([0, 1, 3]), commit=False)       # still being written
    assert resolve_revision_dir(run).endswith("r0001")
    export_run(run, v2_source, log=quiet)
    assert delivered(run) == [0, 1]
    with pytest.raises(ExportInputError, match="not committed"):
        export_run(run, v2_source, revision=2, log=quiet)
    with pytest.raises(ExportInputError, match="no committed"):
        export_run(str(tmp_path / "empty"), v2_source, log=quiet)


def test_an_episode_both_passed_and_held_is_refused(v2_source, tmp_path):
    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1, 3]), held=[{"episode_index": 3}])
    with pytest.raises(ExportInputError, match="both passed and held"):
        export_run(run, v2_source, log=quiet)
    assert not os.path.exists(os.path.join(run, "export"))


def test_source_manifest_of_the_run_is_enforced(v2_source, tmp_path):
    run = str(tmp_path / "run")
    make_revision(run, 1, [entry(0, V2_TASK, "原始标注")])
    st = os.stat(os.path.join(v2_source, "meta", "info.json"))
    manifest = {"schema_version": "1.0", "input": v2_source,
                "objects": [{"key": "meta/info.json", "size": st.st_size + 1,
                             "mtime_ns": st.st_mtime_ns}],
                "summary": {"count": 1, "bytes": st.st_size + 1, "digest": "sha256:" + "1" * 64}}
    with open(os.path.join(run, "source_manifest.json"), "w") as f:
        json.dump(manifest, f)
    with pytest.raises(SourceChangedError):
        export_run(run, v2_source, log=quiet)
