"""The worker pool with stand-in runs: slots, order, pause / stop, shutdown, left-alone tasks."""
from __future__ import annotations

import threading
import time

import pytest

from daemon.orchestr import scheduler as S
from daemon.orchestr.runbase import Interrupt, TaskFailure
from daemon.orchestr.workdir import WorkDir, write_json_atomic
from daemon.transitions import change_task_state

from .conftest import seed_task


class StandIn:
    """What :class:`daemon.orchestr.scheduler.Job` needs from a run."""

    kind = "main"
    log_calls: list = []

    def __init__(self, orch, task, sub, behaviour):
        self.task_id, self.sub_id, self.behaviour = task.id, sub.id if sub else None, behaviour
        self.stages, self._intent = {}, None
        self.wake = threading.Event()

        class J:
            def set(self, **kw):
                pass
        self.journal = J()

    def request(self, intent):
        self._intent = intent
        self.wake.set()

    def execute(self):
        return self.behaviour(self)

    def close(self):
        pass

    def log(self, *a, **kw):
        StandIn.log_calls.append(a)

    def progress(self, *a, **kw):
        pass


@pytest.fixture
def pool(client_for, monkeypatch):
    from daemon.util import now_ms

    events, lock = [], threading.Lock()

    def build(behaviour, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        monkeypatch.setattr(S, "run_for", lambda orch, task, sub: StandIn(orch, task, sub,
                                                                          behaviour))
        c = client_for(clock_fn=now_ms)
        return c, c.app.state.runtime

    def record(what, task_id):
        with lock:
            events.append((what, task_id, time.monotonic()))

    build.events, build.record = events, record
    return build


def _prepared(rt, n=1, **kw):
    ids = []
    for _ in range(n):
        t = seed_task(rt.repo, state="created", **kw)
        wd = WorkDir(rt.settings.work_dir, t.id)
        wd.ensure()
        write_json_atomic(wd.start_mark, {"run_id": "r"})
        assert change_task_state(rt.repo, rt.hub, t.id, {"created"}, "queued", at=rt.clock())
        rt.orchestrator.scheduler.enqueue(t.id)
        ids.append(t.id)
    return ids


def _wait_state(rt, task_id, states, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = rt.repo.get_task(task_id)
        if t.state in states:
            return t
        time.sleep(0.02)
    raise AssertionError(f"{task_id} stayed {rt.repo.get_task(task_id).state}")


def test_one_slot_first_in_first_out(pool):
    def behaviour(run):
        pool.record("start", run.task_id)
        time.sleep(0.15)
        pool.record("end", run.task_id)
        return "succeeded"

    c, rt = pool(behaviour, CURATOR_MAX_RUNNING_TASKS=1)
    ids = _prepared(rt, 3)
    for i in ids:
        _wait_state(rt, i, {"succeeded"})
    starts = [e[1] for e in pool.events if e[0] == "start"]
    assert starts == ids
    open_ = 0
    for what, _, _ in sorted(pool.events, key=lambda e: e[2]):
        open_ += 1 if what == "start" else -1
        assert open_ <= 1


def test_three_run_at_once_by_default_and_the_fourth_waits(pool):
    """P1 (D54): three slots unless CURATOR_MAX_RUNNING_TASKS says otherwise."""
    gate = threading.Barrier(3, timeout=5)
    release = threading.Event()

    def behaviour(run):
        pool.record("start", run.task_id)
        if len([e for e in pool.events if e[0] == "start"]) <= 3:
            gate.wait()                               # the first three are in here together
            release.wait(10)
        return "succeeded"

    c, rt = pool(behaviour)
    assert rt.orchestrator.cfg.max_running == 3
    ids = _prepared(rt, 4)
    for i in ids[:3]:
        _wait_state(rt, i, {"running"})
    time.sleep(0.3)
    assert rt.repo.get_task(ids[3]).state == "queued"
    release.set()
    for i in ids:
        _wait_state(rt, i, {"succeeded"})
    assert [e[1] for e in pool.events if e[0] == "start"][3] == ids[3]


def test_two_slots_run_two_at_once(pool):
    gate = threading.Barrier(2, timeout=5)

    def behaviour(run):
        gate.wait()                                   # both must be in here at the same time
        return "succeeded"

    c, rt = pool(behaviour, CURATOR_MAX_RUNNING_TASKS=2)
    for i in _prepared(rt, 2):
        _wait_state(rt, i, {"succeeded"})


def test_pause_resume_and_stop_reach_the_run(pool):
    runs = {"n": 0}

    def behaviour(run):
        runs["n"] += 1
        if runs["n"] == 1:
            run.wake.wait(10)
            raise Interrupt(run._intent)
        return "completed_with_errors"

    c, rt = pool(behaviour)
    [task_id] = _prepared(rt)
    _wait_state(rt, task_id, {"running"})
    r = c.post(f"/api/v1/tasks/{task_id}/actions/pause", headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    t = _wait_state(rt, task_id, {"paused"})
    assert t.pause_reason == "user"
    c.post(f"/api/v1/tasks/{task_id}/actions/resume", headers={"Content-Type": "application/json"})
    assert _wait_state(rt, task_id, {"completed_with_errors"}).state == "completed_with_errors"

    runs["n"] = 0
    [other] = _prepared(rt)
    _wait_state(rt, other, {"running"})
    c.post(f"/api/v1/tasks/{other}/actions/stop", headers={"Content-Type": "application/json"})
    assert _wait_state(rt, other, {"stopped"}).state_reason == "用户停止"


def test_a_failure_fails_the_task_with_its_reason(pool):
    def behaviour(run):
        raise TaskFailure("source_changed", "源数据变了", stage="numeric")

    c, rt = pool(behaviour)
    [task_id] = _prepared(rt)
    t = _wait_state(rt, task_id, {"failed"})
    assert t.state_reason == "源数据变了"


def test_a_bug_in_a_run_does_not_leave_the_task_running(pool):
    def behaviour(run):
        raise ZeroDivisionError("oops")

    c, rt = pool(behaviour)
    [task_id] = _prepared(rt)
    t = _wait_state(rt, task_id, {"failed"})
    assert "编排出错" in t.state_reason


def test_shutdown_pauses_by_the_system(pool):
    def behaviour(run):
        run.wake.wait(10)
        raise Interrupt(run._intent)

    c, rt = pool(behaviour)
    [task_id] = _prepared(rt)
    _wait_state(rt, task_id, {"running"})
    rt.orchestrator.system_pause_all()
    t = _wait_state(rt, task_id, {"paused"})
    assert t.pause_reason == "system" and t.state_reason == "Daemon 停机"
    [late] = _prepared(rt)
    time.sleep(0.3)
    assert rt.repo.get_task(late).state == "queued"        # nothing new starts


def test_a_queued_task_the_start_procedure_did_not_prepare_is_left_alone(pool):
    def behaviour(run):
        raise AssertionError("must not run")

    c, rt = pool(behaviour)
    t = seed_task(rt.repo, state="queued")
    rt.orchestrator.scheduler.enqueue(t.id)
    time.sleep(0.5)
    assert rt.repo.get_task(t.id).state == "queued"
