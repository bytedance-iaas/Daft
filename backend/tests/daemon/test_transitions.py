"""State changes keep the database, the audit trail and SSE in step (daemon.transitions)."""
from __future__ import annotations

from daemon.events import EventHub
from daemon.repo import protocol as P
from daemon.transitions import change_subtask_state, change_task_state, record_revision

from .conftest import T0, seed_task


def test_task_transition_writes_audit_and_sse(repo):
    hub = EventHub(9)
    t = seed_task(repo)
    assert not change_task_state(repo, hub, t.id, {"running"}, "pausing", at=T0)   # not running
    assert repo.list_events(resource=t.id).items == [] and hub.buffered(t.id) == []

    assert change_task_state(repo, hub, t.id, {"queued"}, "running", at=T0 + 1, actor="alice")
    assert change_task_state(repo, hub, t.id, {"running"}, "pausing", at=T0 + 2,
                             pause_reason="user", actor="alice")
    assert change_task_state(repo, hub, t.id, {"pausing"}, "paused", at=T0 + 3)
    assert change_task_state(repo, hub, t.id, {"paused"}, "queued", at=T0 + 4, actor="alice")
    events = list(reversed(repo.list_events(resource=t.id).items))
    assert [(e.detail["from"], e.detail["to"]) for e in events] == [
        ("queued", "running"), ("running", "pausing"), ("pausing", "paused"), ("paused", "queued")]
    assert events[2].detail["pause_reason"] == "user"                  # kept through pausing
    assert events[3].detail["prev_pause_reason"] == "user"            # the timeline needs this
    assert events[0].actor == "alice" and events[2].actor == "system"
    sse = [(e.event, e.data["state"], e.data["pause_reason"]) for e in hub.buffered(t.id)]
    assert sse == [("state", "running", None), ("state", "pausing", "user"),
                   ("state", "paused", "user"), ("state", "queued", None)]


def test_terminal_state_publishes_done_with_failed_modules(repo):
    hub = EventHub(1)
    t = seed_task(repo, selected=("timestamp_check", "dedup"))
    repo.update_module_state(t.id, "dedup", {"pending"}, "failed")
    change_task_state(repo, hub, t.id, {"queued"}, "running", at=T0)
    change_task_state(repo, hub, t.id, {"running"}, "completed_with_errors", at=T0)
    last = hub.buffered(t.id)[-1]
    assert (last.event, last.data) == ("done", {"state": "completed_with_errors",
                                                "failed_modules": ["dedup"]})
    change_task_state(repo, None, t.id, {"completed_with_errors"}, "succeeded", at=T0)   # no hub


def test_subtask_transitions_remember_their_pause_reason(repo):
    hub = EventHub(1)
    t = seed_task(repo)
    change_task_state(repo, hub, t.id, {"queued"}, "running", at=T0)
    change_task_state(repo, hub, t.id, {"running"}, "succeeded", at=T0)
    sub = repo.create_subtask(P.Subtask(id="", task_id=t.id, kind="reexport", scope={},
                                        state="queued"))
    assert change_subtask_state(repo, hub, sub.id, {"queued"}, "running", at=T0)
    assert change_subtask_state(repo, hub, sub.id, {"running"}, "pausing", at=T0,
                                pause_reason="user")
    assert change_subtask_state(repo, hub, sub.id, {"pausing"}, "paused", at=T0)
    assert not change_subtask_state(repo, hub, sub.id, {"running"}, "stopping", at=T0)
    last = repo.list_events(resource=t.id).items[0]
    assert (last.action, last.detail["to"], last.detail["pause_reason"]) == \
        ("subtask.state", "paused", "user")
    assert change_subtask_state(repo, hub, sub.id, {"paused"}, "queued", at=T0)
    assert repo.list_events(resource=t.id).items[0].detail["prev_pause_reason"] == "user"
    states = [e.data for e in hub.buffered(t.id) if e.data.get("subtask_id") == sub.id]
    assert [s["state"] for s in states] == ["running", "pausing", "paused", "queued"]


def test_record_revision(repo):
    t = seed_task(repo)
    record_revision(repo, t.id, 2, at=T0, subtask_id="sub_1")
    ev = repo.list_events(resource=t.id).items[0]
    assert (ev.action, ev.detail) == ("task.revision", {"revision": 2, "subtask_id": "sub_1"})
