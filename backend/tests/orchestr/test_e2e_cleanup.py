"""A task whose work directory was cleaned 7 days after its end (slow).

A subtask that comes later restores from the delivery what it builds on, and so do
the result readers (W5b's backfill hook): the report opens as before.
"""
from __future__ import annotations

import os

import pytest

from daemon.util import now_ms

pytestmark = pytest.mark.slow

DAY = 86_400_000


def test_a_cleaned_task_comes_back_for_a_reexport_and_for_its_report(daemon):
    d = daemon()
    d.orch.janitor.stop()                                    # swept by hand, with a clock
    task = d.wait(d.create()["id"])
    assert task["state"] == "succeeded"
    task_id, rd, batch = task["id"], d.run_dir(task["id"]), d.delivery(task["run_id"])
    report = d.api("GET", f"/tasks/{task_id}/report")
    assert report.status_code == 200, report.text
    assert os.path.isdir(os.path.join(batch, "details", "evidence"))   # left there on a restore

    later = now_ms() + 8 * DAY
    assert d.orch.janitor.sweep(now=later) == [task_id]
    assert os.listdir(rd) == [".orchestr"]
    assert d.get(task_id)["state"] == "succeeded"            # the task itself is untouched

    r = d.api("POST", f"/tasks/{task_id}/reexport")         # days later: a subtask
    assert r.status_code == 202, r.text
    done = d.wait(task_id)
    assert done["state"] == "succeeded" and done["delivery_stale"] is False, done
    assert os.path.isfile(os.path.join(rd, "revisions", "r0001", "commit.json"))
    assert not os.path.exists(os.path.join(rd, "details", "evidence"))
    assert os.path.isfile(os.path.join(batch, "_COMPLETE"))
    assert any(files for _, _, files in os.walk(os.path.join(batch, "details", "evidence")))
    sub = d.api("GET", f"/tasks/{task_id}/subtasks").json()["items"][-1]
    lines = d.api("GET", f"/tasks/{task_id}/logs",
                  params={"limit": 200, "subtask": sub["id"]}).json()["items"]
    assert any("从交付目录取回" in line["msg"] for line in lines), lines

    assert d.orch.janitor.sweep(now=later + 8 * DAY) == [task_id]
    again = d.api("GET", f"/tasks/{task_id}/report")        # a reader restores it (W5b hook)
    assert again.status_code == 200, again.text
    assert again.json()["report"]["overview"] == report.json()["report"]["overview"]
    assert os.path.isfile(os.path.join(rd, "revisions", "r0001", "report.json"))
