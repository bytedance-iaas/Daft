"""Startup reconciliation (design doc 01 §3.2; D26): nothing looks busy with nobody running it."""
from __future__ import annotations

from daemon.events import EventHub
from daemon.reconcile import SYSTEM_PAUSE_REASON, reconcile
from daemon.repo import protocol as P
from daemon.transitions import change_subtask_state, change_task_state

from .conftest import T0, seed_task


def _to(repo, task_id, *steps):
    """Drive a task with raw CAS steps, e.g. ("running",), ("pausing", "user")."""
    for step in steps:
        to, pause = (step if isinstance(step, tuple) else (step, None))
        cur = repo.get_task(task_id).state
        assert repo.update_task_state(task_id, {cur}, to, pause_reason=pause, at=T0), (cur, to)


def _sub(repo, task_id, *states):
    s = repo.create_subtask(P.Subtask(id="", task_id=task_id, kind="retry", scope={},
                                      state="queued"))
    for to in states:
        cur = repo.get_subtask(s.id).state
        assert repo.update_subtask_state(s.id, {cur}, to, at=T0)
    return s


def test_reconciliation_table(repo, clock):
    running = seed_task(repo, "running")
    _to(repo, running.id, "running")
    pausing_sys = seed_task(repo, "pausing system")
    _to(repo, pausing_sys.id, "running", ("pausing", "system"))
    pausing_none = seed_task(repo, "pausing without reason")
    _to(repo, pausing_none.id, "running", "pausing")
    pausing_user = seed_task(repo, "pausing user")
    _to(repo, pausing_user.id, "running", ("pausing", "user"))
    paused_sys = seed_task(repo, "paused by graceful shutdown")
    _to(repo, paused_sys.id, "running", ("pausing", "system"), "paused")
    paused_user = seed_task(repo, "paused by user")
    _to(repo, paused_user.id, "running", ("pausing", "user"), "paused")
    stopping = seed_task(repo, "stopping")
    _to(repo, stopping.id, "running", "stopping")
    repo.update_task_fields(stopping.id, if_updated_at=None, note="n")
    queued = seed_task(repo, "queued")
    created = seed_task(repo, "created", state="created")
    done = seed_task(repo, "done")
    _to(repo, done.id, "running", "succeeded")

    hub = EventHub(1)
    counts = reconcile(repo, hub, clock)

    def state(t):
        got = repo.get_task(t.id)
        return got.state, got.pause_reason

    assert state(running) == ("queued", None)
    assert state(pausing_sys) == ("queued", None)
    assert state(pausing_none) == ("queued", None)
    assert state(paused_sys) == ("queued", None)
    assert state(pausing_user) == ("paused", "user")
    assert state(paused_user) == ("paused", "user")
    assert state(stopping) == ("stopped", None)
    assert state(queued) == ("queued", None)
    assert state(created) == ("created", None)
    assert state(done) == ("succeeded", None)
    assert counts == {"task:running->pausing": 1, "task:pausing->paused": 4,
                      "task:paused->queued": 4, "task:stopping->stopped": 1}

    trail = [(e.detail["from"], e.detail["to"], e.detail["pause_reason"], e.detail["by"])
             for e in reversed(repo.list_events(resource=running.id).items)]
    assert trail == [("running", "pausing", "system", "reconcile"),
                     ("pausing", "paused", "system", "reconcile"),
                     ("paused", "queued", None, "reconcile")]
    summary = repo.list_events(resource="daemon").items[0]
    assert summary.action == "daemon.reconcile" and summary.detail["counts"] == counts
    states = [(e.data["state"], e.data["pause_reason"]) for e in hub.buffered(running.id)]
    assert states == [("pausing", "system"), ("paused", "system"), ("queued", None)]
    assert hub.buffered(stopping.id)[-1].event == "done"
    assert repo.get_task(running.id).state_reason is None
    assert reconcile(repo, hub, clock) == {}                              # idempotent


def test_system_pause_reason_is_recorded_while_paused(repo, clock):
    t = seed_task(repo)
    _to(repo, t.id, "running")
    seen = []

    class Spy(EventHub):
        def publish_state(self, task_id, state, **kw):
            seen.append((state, repo.get_task(task_id).state_reason))
            return super().publish_state(task_id, state, **kw)

    reconcile(repo, Spy(1), clock)
    assert seen[:2] == [("pausing", SYSTEM_PAUSE_REASON), ("paused", SYSTEM_PAUSE_REASON)]


def test_subtasks_follow_the_same_table(repo, clock):
    hub = EventHub(1)
    parents = []
    for name in ("running", "pausing-system", "pausing-user", "paused-user", "stopping", "queued",
                 "paused-unknown"):
        t = seed_task(repo, name)
        _to(repo, t.id, "running", "completed_with_errors")
        parents.append(t)
    subs = {
        "running": _sub(repo, parents[0].id, "running"),
        "pausing-system": _sub(repo, parents[1].id, "running"),
        "pausing-user": _sub(repo, parents[2].id, "running"),
        "paused-user": _sub(repo, parents[3].id, "running"),
        "stopping": _sub(repo, parents[4].id, "running", "stopping"),
        "queued": _sub(repo, parents[5].id),
        "paused-unknown": _sub(repo, parents[6].id, "running", "pausing", "paused"),
    }
    change_subtask_state(repo, hub, subs["pausing-system"].id, {"running"}, "pausing", at=T0,
                         pause_reason="system")
    change_subtask_state(repo, hub, subs["pausing-user"].id, {"running"}, "pausing", at=T0,
                         pause_reason="user")
    change_subtask_state(repo, hub, subs["paused-user"].id, {"running"}, "pausing", at=T0,
                         pause_reason="user")
    change_subtask_state(repo, hub, subs["paused-user"].id, {"pausing"}, "paused", at=T0)

    counts = reconcile(repo, hub, clock)
    got = {name: repo.get_subtask(s.id).state for name, s in subs.items()}
    assert got == {"running": "queued", "pausing-system": "queued", "pausing-user": "paused",
                   "paused-user": "paused", "stopping": "stopped", "queued": "queued",
                   "paused-unknown": "queued"}
    assert counts["subtask:paused->queued"] == 3
    for t in parents:
        assert repo.get_task(t.id).state == "completed_with_errors"        # parents never move
    user_paused = [e for e in hub.buffered(parents[3].id) if e.data.get("subtask_id")]
    assert user_paused[-1].data["pause_reason"] == "user"


def test_app_start_reconciles_before_serving(make_app):
    from fastapi.testclient import TestClient

    app = make_app()
    rt = app.state.runtime
    t = seed_task(rt.repo)
    change_task_state(rt.repo, None, t.id, {"queued"}, "running", at=T0)
    assert not rt.reconciled.is_set()
    order = []
    rt.on_ready.append(lambda runtime: order.append(runtime.repo.get_task(t.id).state))
    with TestClient(app) as c:
        assert rt.reconciled.is_set() and order == ["queued"]
        assert c.get("/readyz").json()["checks"]["reconciled"] is True
    assert rt.stopping.is_set()


def test_a_failed_reconciliation_keeps_the_pod_unready(make_app, monkeypatch):
    from fastapi.testclient import TestClient

    import daemon.app as app_mod

    calls = []

    def flaky(repo, hub, clock):
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app_mod, "reconcile", flaky)
    monkeypatch.setattr(app_mod, "RECONCILE_RETRY_S", 0.05)
    app = make_app()
    with TestClient(app) as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["checks"]["reconciled"] is False
        assert c.get("/healthz").status_code == 200
        monkeypatch.setattr(app_mod, "reconcile", lambda *a: {})
        rt = app.state.runtime
        assert rt.reconciled.wait(5)
        assert c.get("/readyz").status_code == 200
    assert calls
