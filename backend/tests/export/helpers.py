"""Shared helpers of the export tests."""
from __future__ import annotations

import hashlib
import json
import os

#: The W0 fixture's episodes that a real run delivers: 2 (timestamp jump) and 5
#: (fragment) fail the timestamp gate, 7 is a byte copy of 3 (dedup); 4 and 6 have no
#: task text, so the model captioned them.
V2_CAPTIONS = {4: "wipe the table with the sponge", 6: "stack the green cup on the plate"}
V2_TASK = "pick up the red block and place it in the bin"


def quiet(_level: str, _msg: str) -> None:
    pass


def entry(i: int, text: str | None = None, source: str | None = None) -> dict:
    e: dict = {"episode_index": int(i)}
    if source is not None:
        e["task_text"] = {"text": text or "", "source": source}
    return e


def v2_entries(indices, relabel: dict | None = None) -> list[dict]:
    """passed.json entries for the W0 fixture as aggregate would write them."""
    out = []
    for i in indices:
        if relabel and i in relabel:
            out.append(entry(i, relabel[i], "人工改标"))
        elif i in V2_CAPTIONS:
            out.append(entry(i, V2_CAPTIONS[i], "自产caption"))
        else:
            out.append(entry(i, V2_TASK, "原始标注"))
    return out


def final_list(name: str, entries: list[dict], revision: int = 1) -> dict:
    return {"schema_version": "1.0", "list": name, "revision": revision,
            "count": len(entries), "episodes": entries}


#: v3 file-size thresholds that spread the six fixture episodes over several files
V3_PARAMS = {"video_file_mb": 0.01, "data_file_mb": 0.01}


def export_v3(src, out, indices, relabel=None, **kw):
    """Export the v3 fixture's ``indices`` (original task texts unless relabelled) and
    check the result and manifest against the contracts."""
    from curation.contracts import schemas
    from curation.export.incremental import export_dataset

    from .v3_fixture import EPISODE_TASK, TASKS

    entries = [entry(i, relabel[i], "人工改标") if relabel and i in relabel
               else entry(i, TASKS[EPISODE_TASK[i]], "原始标注") for i in indices]
    kw.setdefault("params", V3_PARAMS)
    kw.setdefault("log", quiet)
    o = export_dataset(src, final_list("passed", entries), str(out), **kw)
    assert schemas.errors("cli/export.schema.json", o.result) == []
    with open(o.manifest_path, encoding="utf-8") as f:
        assert schemas.errors("cli/export-manifest.schema.json", json.load(f)) == []
    return o


def make_revision(run_dir: str, revision: int, passed: list[dict], *,
                  held: list[dict] = (), review: list[dict] = (), commit: bool = True) -> str:
    rev = os.path.join(run_dir, "revisions", f"r{revision:04d}")
    os.makedirs(rev, exist_ok=True)
    docs = {"passed": passed, "held": list(held), "review": list(review),
            "reject": []}
    for name, eps in docs.items():
        with open(os.path.join(rev, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(final_list(name, eps, revision), f, ensure_ascii=False)
    if commit:
        with open(os.path.join(rev, "commit.json"), "w", encoding="utf-8") as f:
            json.dump({"schema_version": "1.0", "revision": revision, "created_at": 0,
                       "source_digest": "sha256:" + "0" * 64, "parts": {},
                       "adjudications_applied": [], "subtask_id": None, "files": {}}, f)
    return rev


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def snapshot(root: str) -> dict[str, tuple[int, int, int, str]]:
    """rel path -> (inode, mtime_ns, size, sha256) of every file under ``root``."""
    out = {}
    for cur, _dirs, names in os.walk(root):
        for name in names:
            p = os.path.join(cur, name)
            st = os.stat(p)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = (
                st.st_ino, st.st_mtime_ns, st.st_size, sha256(p))
    return out


def untouched(before: dict, after: dict, rel: str) -> bool:
    return rel in before and before[rel] == after.get(rel)


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
