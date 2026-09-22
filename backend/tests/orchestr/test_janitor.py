"""Work directories: cleaned 7 days after the end, restored from the delivery on demand (00 §4.2)."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from daemon.orchestr import delivery as D
from daemon.orchestr.workdir import WorkDir
from daemon.repo import protocol as P
from daemon.util import now_ms

from .conftest import seed_task

DAY = 86_400_000
RUN_ID = "20260901-120000"

#: the run directory of a finished task, and what of it the delivery has
LOCAL = {
    "plan.json": "{}", "source_manifest.json": "{}", "run.json": "{}",
    "revisions/r0001/commit.json": '{"revision": 1}', "revisions/r0001/report.json": "{}",
    "checks/task_success/parts/0001.jsonl": "{}\n", "logs/numeric.jsonl": "{}\n",
    "details/evidence/task_success/ep000001_0.jpg": "jpg", "details/vlm_latency.csv": "a,b\n",
    "checks/video_action_sync/curves/ep000001.json": "{}", "export/manifest.json": "{}",
}
DELIVERED_ONLY = {"_COMPLETE": "", "export/lerobot_curated/meta/info.json": "{}",
                  "export/lerobot_curated/data/chunk-000/episode_000000.parquet": "p"}


@pytest.fixture
def world(daemon):
    d = daemon()
    d.orch.janitor.stop()                       # the tests sweep by hand
    return d


def _finished(d, *, ended_at: int, files=True, state="succeeded", run_id=RUN_ID) -> P.Task:
    rt = d.rt
    cred = rt.repo.get_credential_by_name("out-key")
    t = seed_task(rt.repo, state="created", delivery="tos://deliveries/mini",
                  output_cred_id=cred.id)
    rt.repo.freeze_task_inputs(t.id, run_id=run_id, preflight={}, source_fingerprint={},
                               vlm_snapshot=None)
    for frm, to in (("created", "queued"), ("queued", "running"), ("running", state)):
        assert rt.repo.update_task_state(t.id, {frm}, to, at=ended_at)
    assert rt.repo.switch_result_rev(t.id, 0, 1)
    wd = WorkDir(rt.settings.work_dir, t.id)
    wd.ensure()
    (wd.start_mark).write_text("{}")
    (wd.journal("main")).write_text(json.dumps({"stages": {"numeric": {"done": True}}}))
    if files:
        batch = pathlib.Path(d.delivery(run_id))
        for rel, text in {**LOCAL, **DELIVERED_ONLY}.items():
            path = batch / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for rel, text in LOCAL.items():
            path = wd.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        D.mark_synced(wd.sync_state, run_id, wd.root, list(LOCAL))
    scratch = pathlib.Path(rt.settings.scratch_dir) / t.id / "curation-export-x"
    scratch.mkdir(parents=True, exist_ok=True)
    return rt.repo.get_task(t.id)


def _local(d, task_id) -> set[str]:
    root = pathlib.Path(d.run_dir(task_id))
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
            and not p.relative_to(root).as_posix().startswith(".orchestr/")}


def test_a_finished_task_is_cleaned_after_the_retention_and_restored_on_demand(world):
    d = world
    now = now_ms()
    task = _finished(d, ended_at=now - 10 * DAY)
    young = _finished(d, ended_at=now - 6 * DAY)
    wd = WorkDir(d.rt.settings.work_dir, task.id)
    # written after the last publish: goes up with the final sync before the cleaning
    (wd.root / "human-decisions").mkdir()
    (wd.root / "human-decisions" / "task_verdicts.csv").write_text("episode_id,verdict\n")

    assert d.orch.janitor.sweep(now=now) == [task.id]
    assert _local(d, task.id) == set()                          # only .orchestr/ is left
    mark = json.loads(wd.cleaned_mark.read_text())
    assert mark["purged"] is False and mark["bytes"] > 0
    assert wd.started() and wd.journal("main").is_file()       # the task can go on later
    assert not (pathlib.Path(d.rt.settings.scratch_dir) / task.id).exists()
    batch = pathlib.Path(d.delivery(RUN_ID))
    assert (batch / "human-decisions" / "task_verdicts.csv").is_file()
    assert (batch / "details" / "evidence" / "task_success" / "ep000001_0.jpg").is_file()
    assert _local(d, young.id) == set(LOCAL)                    # 6 days: not yet
    assert d.orch.janitor.sweep(now=now) == []                  # nothing left to do

    # a reader (W5b's hook) brings back what is not big
    restorer = d.orch.restorer
    assert restorer.needed(task)
    assert restorer.hook(task, "revisions/r0001/commit.json") is True
    skipped = {"details/evidence/task_success/ep000001_0.jpg",
               "checks/video_action_sync/curves/ep000001.json"}
    assert _local(d, task.id) == (set(LOCAL) - skipped) | {"human-decisions/task_verdicts.csv"}
    assert not wd.cleaned_mark.exists() and wd.restored_mark.is_file()
    assert not restorer.needed(task)
    with d.orch.restorer.open(task) as delivery:                # nothing to upload again
        assert D.sync_run_dir(delivery, RUN_ID, wd.root, wd.sync_state) == {"uploaded": 0}
    # the retention counts from the restore now
    assert d.orch.janitor.sweep(now=now) == []
    assert d.orch.janitor.sweep(now=now_ms() + 8 * DAY) == sorted([task.id, young.id])


def test_what_keeps_a_directory(world):
    d = world
    now = now_ms()
    busy = _finished(d, ended_at=now - 10 * DAY, files=False)
    sub = d.rt.repo.create_subtask(P.Subtask(id="", task_id=busy.id, kind="reexport", scope={},
                                             state="queued"))
    running = seed_task(d.rt.repo, state="created")
    for frm, to in (("created", "queued"), ("queued", "running")):
        assert d.rt.repo.update_task_state(running.id, {frm}, to, at=now - 20 * DAY)
    WorkDir(d.rt.settings.work_dir, running.id).ensure()
    (pathlib.Path(d.run_dir(running.id)) / "plan.json").write_text("{}")
    (pathlib.Path(d.run_dir(busy.id)) / "plan.json").write_text("{}")
    assert d.orch.janitor.sweep(now=now) == []                  # a subtask / a running task
    # a subtask that ended counts as the last activity
    assert d.rt.repo.update_subtask_state(sub.id, {"queued"}, "running", at=now - 3 * DAY)
    assert d.rt.repo.update_subtask_state(sub.id, {"running"}, "succeeded", at=now - 2 * DAY)
    assert d.orch.janitor.sweep(now=now) == []
    assert d.orch.janitor.sweep(now=now + 6 * DAY) == [busy.id]


def test_what_the_delivery_does_not_hold_is_never_removed(world, monkeypatch):
    d = world
    now = now_ms()
    task = _finished(d, ended_at=now - 10 * DAY, state="failed")
    (pathlib.Path(d.run_dir(task.id)) / "logs" / "system.jsonl").write_text("{}\n")

    def broken(t):
        raise D.DeliveryError("交付目录写不进去", "AccessDenied")

    working = d.orch.restorer.open
    monkeypatch.setattr(d.orch.restorer, "open", broken)
    assert d.orch.janitor.sweep(now=now) == []                  # the last upload failed
    assert d.orch.janitor.sweep(now=now + 365 * DAY) == []      # however long it has been
    assert _local(d, task.id) == set(LOCAL) | {"logs/system.jsonl"}
    no_batch = _finished(d, ended_at=now - 10 * DAY, files=False, run_id="20260901-140000")
    d.rt.repo.freeze_task_inputs(no_batch.id, run_id="", preflight={}, source_fingerprint={},
                                 vlm_snapshot=None)                 # (as if it never had one)
    (pathlib.Path(d.run_dir(no_batch.id)) / "plan.json").write_text("{}")
    monkeypatch.setattr(d.orch.restorer, "open", working)
    assert d.orch.janitor.sweep(now=now) == [task.id]           # uploaded now, then cleaned
    assert (pathlib.Path(d.run_dir(no_batch.id)) / "plan.json").is_file()
    # and a reader does not hammer an unreachable delivery
    calls = []
    monkeypatch.setattr(d.orch.restorer, "open", lambda t: calls.append(t) or broken(t))
    assert d.orch.restorer.hook(task, "revisions/r0001/commit.json") is False
    assert d.orch.restorer.hook(task, "revisions/r0001/commit.json") is False
    assert len(calls) == 1


def test_a_purged_batch_is_not_uploaded_again(world):
    d = world
    now = now_ms()
    task = _finished(d, ended_at=now - 10 * DAY)
    batch = pathlib.Path(d.delivery(RUN_ID))
    import shutil

    shutil.rmtree(batch)
    wd = WorkDir(d.rt.settings.work_dir, task.id)
    D.forget_sync(wd.sync_state)
    wd.purged_mark.write_text("{}")
    assert d.orch.janitor.sweep(now=now) == [task.id]
    assert not batch.exists()
    assert d.orch.restorer.hook(task, "revisions/r0001/commit.json") is False


def test_a_subtask_on_a_cleaned_task_restores_first_and_says_what_is_missing(world):
    from daemon.orchestr.runbase import Run, TaskFailure

    d = world
    task = _finished(d, ended_at=now_ms() - 10 * DAY)
    assert d.orch.janitor.sweep() == [task.id]
    run = Run(d.orch, task)
    run.ensure_local()                                      # back from the delivery
    assert (pathlib.Path(d.run_dir(task.id)) / "revisions" / "r0001" / "commit.json").is_file()
    run.ensure_local()                                      # nothing to do the second time

    gone = _finished(d, ended_at=now_ms() - 10 * DAY, files=False,   # nothing was delivered
                     run_id="20260901-130000")
    WorkDir(d.rt.settings.work_dir, gone.id).cleaned_mark.write_text("{}")
    with pytest.raises(TaskFailure) as err:
        Run(d.orch, gone).ensure_local()
    assert err.value.code == "no_result" and "r0001" in err.value.reason_zh


def test_directories_without_a_task_go_when_old(world):
    d = world
    root = pathlib.Path(d.rt.settings.work_dir)
    old, fresh = root / ("task_" + "0" * 26), root / ("task_" + "1" * 26)
    other = root / "not-a-task"
    for p in (old, fresh, other):
        (p / ".orchestr").mkdir(parents=True)
        (p / "plan.json").write_text("{}")
    ancient = (now_ms() - 30 * DAY) / 1000
    for p in (old, old / ".orchestr", old / "plan.json", other, other / "plan.json"):
        os.utime(p, (ancient, ancient))
    assert d.orch.janitor.sweep() == [old.name]
    assert not old.exists() and fresh.exists() and other.exists()


def test_the_retention_setting(monkeypatch):
    from daemon.orchestr.config import OrchestratorConfig

    assert OrchestratorConfig.from_env({}).work_retention_s == 7 * 86400
    cfg = OrchestratorConfig.from_env({"CURATOR_WORK_RETENTION_DAYS": "0.5"})
    assert cfg.work_retention_s == 43200
    assert OrchestratorConfig.from_env({"CURATOR_WORK_RETENTION_DAYS": "0"}).work_retention_s == 0
