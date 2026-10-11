"""The Episode 流水线 rows after the run (C4 5.3.2): the current revision's verdicts, not the provisional ones."""
from __future__ import annotations

import dataclasses

from daemon.results import live

from .conftest import World

VERDICTS = {0: "keep", 1: "drop", 2: "drop", 3: "keep", 4: "keep", 5: "keep", 6: "held", 7: "keep", 8: "keep"}


def test_a_finished_task_reads_its_verdicts_from_the_current_revision(world: World):
    final = live._final(world.rt, world.task())
    assert {ep: v for ep, (v, _) in final.items()} == VERDICTS
    # the reasons are the lists' own words: a reject names its blocking findings, a pass has none
    assert final[1][1] and "；" not in final[1][1]
    assert final[0][1] is None


def test_rows_stay_provisional_while_anything_runs(world: World, monkeypatch):
    task = world.task()
    # a retry or an applied decision reruns episodes: their rows are live again
    monkeypatch.setattr(world.rt.repo, "active_subtask", lambda task_id: object())
    assert live._final(world.rt, task) is None
    monkeypatch.undo()
    assert live._final(world.rt, dataclasses.replace(task, state="running")) is None
    assert live._final(world.rt, dataclasses.replace(task, result_rev=0)) is None
