"""Manual walk-through of an incremental re-export (curation/export/INCREMENTAL.md).

    .venv/bin/python backend/tests/export/demo_reexport.py --work "$W7" --format v2
    .venv/bin/python backend/tests/export/demo_reexport.py --work "$W7" --format v3

Builds a fixture dataset under ``--work`` and a run directory with three result
revisions, exports each with ``export_run(..., incremental=True)`` and prints the
``curation export --json`` result plus which delivered files were left untouched,
renamed or written.

v2 (W0's fixture):  r1 passed 0 1 3 4 6 (1 waits for a human, 2 is held for a retry)
                    r2 drops the middle episode 3, r3 relabels episode 4.
v3 (v3_fixture):    r1 passed 0-5, r2 drops the middle episode 2, r3 relabels 3.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(os.path.dirname(HERE))
for p in (BACKEND, os.path.join(os.path.dirname(BACKEND), "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from curation.export.incremental import export_run  # noqa: E402
from tests.export.helpers import (V3_PARAMS, entry, make_revision, quiet,  # noqa: E402
                                  snapshot, v2_entries)


def _step(title: str, run: str, source: str, **kw) -> None:
    ds = os.path.join(run, "export", "lerobot_curated")
    before = snapshot(ds) if os.path.isdir(ds) else {}
    o = export_run(run, source, log=quiet, **kw)
    after = snapshot(ds)
    moved = {new for _old, new in o.renamed}
    untouched = sorted(r for r in after if before.get(r) == after[r])
    written = sorted(r for r in after if before.get(r) != after[r] and r not in moved)
    print(f"\n== {title}")
    print(json.dumps(o.result, ensure_ascii=False))
    print(f"   untouched {len(untouched)}, renamed {len(o.renamed)}, written {len(written)}, "
          f"deleted {len(o.deleted)}")
    for old, new in o.renamed:
        print(f"   renamed  {old} -> {new}  (inode {before[old][0]} -> {after[new][0]})")
    for r in written:
        print(f"   written  {r}")
    for r in o.deleted:
        print(f"   deleted  {r}")
    if o.rebuild_reasons:
        print(f"   full export because: {'; '.join(o.rebuild_reasons)}")


def demo_v2(work: str) -> str:
    from parity.fixtures import make_mini_lerobot

    src = make_mini_lerobot(os.path.join(work, "v2-source"))
    run = os.path.join(work, "v2-run")
    review = [{"episode_index": 1, "current_list": "passed",
               "review": [{"source_module": "task_success", "kind": "task_verdict",
                           "reason": "证据不足，弃权"}]}]
    make_revision(run, 1, v2_entries([0, 1, 3, 4, 6]), held=[{"episode_index": 2}], review=review)
    _step("r1: first export (episode 1 waits for a human -> delivered; 2 is held -> not)", run, src)
    make_revision(run, 2, v2_entries([0, 1, 4, 6]), held=[{"episode_index": 2}], review=review)
    _step("r2: episode 3 rejected -> the middle episode drops out", run, src)
    make_revision(run, 3, v2_entries([0, 1, 4, 6], relabel={4: "wipe the table clean"}),
                  held=[{"episode_index": 2}])
    _step("r3: episode 4 relabelled -> no video is touched", run, src)
    return run


def demo_v3(work: str) -> str:
    from tests.export.v3_fixture import EPISODE_TASK, TASKS, make_mini_lerobot_v3

    src = make_mini_lerobot_v3(os.path.join(work, "v3-source"))
    run = os.path.join(work, "v3-run")

    def passed(indices, relabel=None):
        return [entry(i, relabel[i], "人工改标") if relabel and i in relabel
                else entry(i, TASKS[EPISODE_TASK[i]], "原始标注") for i in indices]
    make_revision(run, 1, passed(range(6)))
    _step("r1: first export (small file-size thresholds: several files per camera)", run, src,
          params=V3_PARAMS)
    make_revision(run, 2, passed([0, 1, 3, 4, 5]))
    _step("r2: the middle episode 2 drops out -> only the video files it was in are re-encoded",
          run, src, params=V3_PARAMS)
    make_revision(run, 3, passed([0, 1, 3, 4, 5], relabel={3: "wipe the table"}))
    _step("r3: episode 3 relabelled -> no video is touched", run, src, params=V3_PARAMS)
    return run


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--work", required=True, help="an empty directory outside the repo")
    p.add_argument("--format", choices=("v2", "v3"), default="v2")
    args = p.parse_args(argv)
    os.makedirs(args.work, exist_ok=True)
    run = demo_v2(args.work) if args.format == "v2" else demo_v3(args.work)
    ds = os.path.join(run, "export", "lerobot_curated")
    venv = "curator-lerobot-v21" if args.format == "v2" else "curator-lerobot-v30"
    print(f"\nofficial loader check:\n  ~/.cache/{venv}/bin/python "
          f"{os.path.join(HERE, 'lerobot_check.py')} {ds}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
