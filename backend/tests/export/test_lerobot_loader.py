"""Acceptance (design doc 11, section 5, W7): after dropping a middle episode and
re-exporting, the official LeRobot loader opens the delivery without a warning.

Skipped unless ``lerobot`` is importable -- it is not in the shared venv on purpose
(torch).  Run it in a separate venv, see ``curation/export/INCREMENTAL.md``:

* lerobot 0.3.x reads codebase v2.1 -> the v2 cases run, the v3 ones skip;
* lerobot >= 0.4 reads v3.0 only     -> the v3 cases run, the v2 ones skip.
"""
from __future__ import annotations

import os

import pytest

from .lerobot_check import lerobot_info

INFO = lerobot_info()
pytestmark = pytest.mark.skipif(INFO is None, reason="lerobot is not importable in this venv")

from curation.adapters.decode import decode_window  # noqa: E402
from curation.export.incremental import export_dataset, export_run  # noqa: E402

from .helpers import (V2_CAPTIONS, V2_TASK, entry, final_list, make_revision,  # noqa: E402
                      quiet, v2_entries)
from .lerobot_check import load_check  # noqa: E402
from .v3_fixture import EPISODE_TASK, TASKS  # noqa: E402

reads_v21 = pytest.mark.skipif(INFO is not None and not INFO[1].startswith("v2.1"),
                               reason="this lerobot does not read codebase v2.1")
reads_v3 = pytest.mark.skipif(INFO is not None and not INFO[1].startswith("v3"),
                              reason="this lerobot does not read codebase v3")


def _reference(state, src_window):
    """reference(ep, frame, cam) for load_check: the frame the source shows there."""
    cache = {}

    def ref(ep, k, cam):
        e = state.episodes[ep]
        key = (e.episode_index, cam)
        if key not in cache:
            path, a, b = src_window(e.episode_index, cam)
            cache[key] = decode_window(path, a, b)[0]
        return cache[key][k]
    return ref


def _check(root, state, src_window):
    report = load_check(os.path.join(root, "lerobot_curated"),
                        expected_tasks=[e.tasks[0] for e in state.episodes],
                        reference=_reference(state, src_window))
    assert report["ok"], report
    assert report["episodes"] == len(state.episodes)
    assert report["frames"] == sum(e.length for e in state.episodes)
    return report


@reads_v21
def test_v2_drop_middle_episode_loads_without_warnings(v2_source, tmp_path):
    def src_window(i, cam):
        return os.path.join(v2_source, "videos", "chunk-000", cam, f"episode_{i:06d}.mp4"), 0.0, 1e9

    run = str(tmp_path / "run")
    make_revision(run, 1, v2_entries([0, 1, 3, 4, 6]), held=[{"episode_index": 2}],
                  review=[{"episode_index": 1, "current_list": "passed",
                           "review": [{"source_module": "task_success", "kind": "task_verdict",
                                       "reason": "证据不足，弃权"}]}])
    first = export_run(run, v2_source, log=quiet)
    _check(os.path.join(run, "export"), first.state, src_window)

    make_revision(run, 2, v2_entries([0, 1, 4, 6]))                 # drop the middle one (3)
    o = export_run(run, v2_source, log=quiet)
    assert o.result["incremental"] and o.result["diff"]["drop"] == 1
    assert o.result["videos_copied"] == 0
    _check(os.path.join(run, "export"), o.state, src_window)

    make_revision(run, 3, v2_entries([0, 1, 4, 6], relabel={4: "wipe the table clean"}))
    o = export_run(run, v2_source, log=quiet)
    assert o.result["diff"]["relabel"] == 1 and o.result["videos_copied"] == 0
    assert [e.tasks[0] for e in o.state.episodes] == [V2_TASK, V2_TASK, "wipe the table clean",
                                                      V2_CAPTIONS[6]]
    _check(os.path.join(run, "export"), o.state, src_window)


@reads_v21
def test_v2_check_catches_a_broken_delivery(v2_source, tmp_path):
    """Negative control: episode 2 has a 0.6 s timestamp jump (the timestamp gate drops it,
    or it is held); delivered anyway, the official loader refuses the dataset."""
    o = export_dataset(v2_source, final_list("passed", v2_entries([0, 1, 2])), str(tmp_path / "x"),
                       log=quiet)
    report = load_check(str(tmp_path / "x" / "lerobot_curated"))
    assert not report["ok"] and report["problems"], report
    assert len(o.state.episodes) == 3


@reads_v3
def test_v3_drop_middle_episode_loads_without_warnings(v3_source, tmp_path):
    import pandas as pd

    meta = pd.read_parquet(os.path.join(v3_source, "meta", "episodes", "chunk-000",
                                        "file-000.parquet")).set_index("episode_index")

    def src_window(i, cam):
        row = meta.loc[i]
        path = os.path.join(v3_source, "videos", cam,
                            f"chunk-000/file-{int(row[f'videos/{cam}/file_index']):03d}.mp4")
        return path, float(row[f"videos/{cam}/from_timestamp"]), float(row[f"videos/{cam}/to_timestamp"])

    source_report = load_check(v3_source)                 # the fixture itself is a clean v3.0 dataset
    assert source_report["ok"], source_report

    def passed(indices, relabel=None):
        return final_list("passed", [entry(i, relabel[i], "人工改标") if relabel and i in relabel
                                     else entry(i, TASKS[EPISODE_TASK[i]], "原始标注") for i in indices])

    params = {"video_file_mb": 0.01, "data_file_mb": 0.01}
    out = str(tmp_path / "export")
    first = export_dataset(v3_source, passed(range(6)), out, params=params, log=quiet)
    _check(out, first.state, src_window)

    o = export_dataset(v3_source, passed([0, 1, 3, 4, 5]), out, params=params, log=quiet)
    assert o.result["incremental"] and o.result["diff"]["drop"] == 1
    assert 0 < o.result["videos_reencoded"] < len({rel for e in first.state.episodes
                                                   for rel in e.videos.values()})
    _check(out, o.state, src_window)

    o = export_dataset(v3_source, passed([0, 1, 3, 4, 5], relabel={3: "wipe the table"}), out,
                       params=params, log=quiet)
    assert o.result["diff"]["relabel"] == 1 and o.result["videos_reencoded"] == 0
    _check(out, o.state, src_window)

    o = export_dataset(v3_source, passed(range(6), relabel={3: "wipe the table"}), out,
                       params=params, log=quiet)
    assert o.result["diff"]["add"] == 1
    _check(out, o.state, src_window)
