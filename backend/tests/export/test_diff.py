"""Diff classes, the task table and the task text resolution (pure functions)."""
from __future__ import annotations

import pytest

from curation.export.diff import CLASSES, EpisodeKey, diff_episodes
from curation.export.incremental_base import Wanted, task_table, written_tasks
from curation.export.lerobot_writer import _task_table


def K(i, new, content="c", task="t", index_from=None, task_index=None):
    return EpisodeKey(i, new, f"sha256:{content}{i}", task, index_from, task_index)


def test_first_export_is_all_add():
    d = diff_episodes(None, [K(3, 0), K(7, 1)])
    assert d.counts() == {"keep": 0, "relabel": 0, "renumber": 0, "add": 2, "drop": 0}


def test_drop_a_middle_episode():
    old = [K(0, 0, index_from=0), K(1, 1, index_from=10), K(3, 2, index_from=20),
           K(4, 3, index_from=30), K(6, 4, index_from=40)]
    new = [K(0, 0, index_from=0), K(1, 1, index_from=10), K(4, 2, index_from=20),
           K(6, 3, index_from=30)]
    d = diff_episodes(old, new)
    assert d.classes == {0: "keep", 1: "keep", 4: "renumber", 6: "renumber"}
    assert d.dropped == [3]
    assert d.counts() == {"keep": 2, "relabel": 0, "renumber": 2, "add": 0, "drop": 1}
    assert sum(d.counts()[c] for c in CLASSES if c != "drop") == len(new)


def test_relabel_and_renumber_and_changed_source():
    old = [K(0, 0, task="a"), K(1, 1), K(2, 2, content="old")]
    new = [K(0, 0, task="b"), K(2, 1, content="new"), K(1, 2, task="z")]
    d = diff_episodes(old, new)
    assert d.classes == {0: "relabel", 2: "add", 1: "renumber"}   # renumber wins over relabel
    assert d.dropped == [2]                                         # changed source = drop + add


def test_frame_offset_and_task_index_count_when_known():
    old = [K(5, 0, index_from=0, task_index=0)]
    assert diff_episodes(old, [K(5, 0, index_from=7, task_index=0)]).classes[5] == "renumber"
    assert diff_episodes(old, [K(5, 0, index_from=0, task_index=2)]).classes[5] == "relabel"
    # a manifest-only old side (no offsets / task indices) is compared on what it has
    assert diff_episodes([K(5, 0)], [K(5, 0, index_from=7, task_index=2)]).classes[5] == "keep"


def test_duplicate_episode_is_refused():
    with pytest.raises(ValueError):
        diff_episodes(None, [K(1, 0), K(1, 1)])


# ── task table ───────────────────────────────────────────────────────────────

def test_first_export_task_table_is_v1s():
    tasks = [["b"], ["a"], ["b"], ["c", "a"]]
    table, idx = task_table(None, tasks)
    assert (table, idx) == _task_table(tasks)


def test_incremental_task_table_keeps_numbers():
    prev = ["pick", "place", "open", "push"]
    # "place" was relabelled to "wipe": the new text takes the freed slot, nothing moves
    table, idx = task_table(prev, [["pick"], ["push"], ["open"], ["wipe"]])
    assert table == ["pick", "wipe", "open", "push"]
    assert idx == {"pick": 0, "wipe": 1, "open": 2, "push": 3}
    # nothing changed -> identical table
    assert task_table(prev, [["pick"], ["place"], ["open"], ["push"]])[0] == prev
    # free slots without a new text: the last text fills them, trailing ones just go
    assert task_table(prev, [["pick"]])[0] == ["pick"]
    assert task_table(prev, [["push"], ["pick"]])[0] == ["pick", "push"]
    # more new texts than free slots: the rest is appended in first-appearance order
    assert task_table(["a", "b"], [["a"], ["c"], ["d"]])[0] == ["a", "c", "d"]
    assert task_table(["a", "b", "c"], [["c"], ["x"]])[0] == ["x", "c"]


# ── task text resolution (v1 task_overrides semantics) ───────────────────────

@pytest.mark.parametrize("wanted,src,expect", [
    (Wanted(1, "wipe", "自产caption"), [""], (["wipe"], "自产caption")),
    (Wanted(1, "wipe it", "人工改标"), ["pick"], (["wipe it"], "人工改标")),
    (Wanted(1, "pick", "原始标注"), ["pick", "place"], (["pick", "place"], "原始标注")),
    (Wanted(1, "", "自产caption"), [], ([""], "无")),            # empty caption: no override
    (Wanted(1, None, None), ["pick"], (["pick"], "原始标注")),   # no task_text in passed.json
    (Wanted(1, None, None), [], ([""], "无")),
    (Wanted(1, "", "无"), [], ([""], "无")),
])
def test_written_tasks(wanted, src, expect):
    assert written_tasks(wanted, src) == expect
