"""curation task ... against a stub Daemon whose answers are validated against C4 (D22)."""
from __future__ import annotations

import base64
import json

import pytest

from curation.cli import task_client

from .fakes import StubDaemon, make_task


@pytest.fixture
def stub(monkeypatch):
    with StubDaemon() as daemon:
        daemon.tasks["task_01"] = make_task("task_01", "running", pending=3)
        monkeypatch.setenv("CURATOR_URL", daemon.url)
        monkeypatch.setenv("CURATOR_USER", daemon.user)
        monkeypatch.setenv("CURATOR_PASSWORD", daemon.password)
        yield daemon
        assert daemon.problems == [], "the stub answered outside the contract"


def _last(stub) -> dict:
    return stub.requests[-1]


TASK_BODY = {"name": "hf pusht", "input": {"source": "public", "uri": "pusht"},
             "output": {"uri": "tos://b/d", "credential": "prod-tos"},
             "preflight_id": "pf_2", "episodes": {"mode": "all"},
             "modules": ["timestamp_check", {"id": "video_action_sync",
                                             "params": {"sync_plots": "all"}}]}


def test_create_prints_the_response_with_links(cli, stub, tmp_path):
    path = tmp_path / "task.json"
    path.write_text(json.dumps(TASK_BODY, ensure_ascii=False))
    res = cli("task", "create", "--file", str(path), "--idempotency-key", "agent-run-0001")
    assert res.rc == 0
    assert res.doc["id"] == "task_new" and res.doc["state"] == "queued"
    assert {ln["rel"] for ln in res.doc["links"]} == {"task", "report"}
    req = _last(stub)
    assert req["method"] == "POST" and req["path"] == "/curation/api/v1/tasks"
    assert req["body"] == TASK_BODY
    assert req["headers"]["idempotency-key"] == "agent-run-0001"
    assert req["headers"]["content-type"] == "application/json"
    expected = base64.b64encode(f"{stub.user}:{stub.password}".encode()).decode()
    assert req["headers"]["authorization"] == f"Basic {expected}"


def test_create_and_wait(cli, stub, tmp_path):
    path = tmp_path / "task.json"
    path.write_text(json.dumps(TASK_BODY))
    stub.state_script["task_new"] = ["queued", "running", "completed_with_errors"]
    res = cli("task", "create", "--file", str(path), "--wait", "--poll-interval", "0.01")
    assert res.rc == 0
    assert res.doc["id"] == "task_new" and res.doc["state"] == "completed_with_errors"
    assert res.doc["links"]


def test_create_reads_stdin_and_rejects_bad_files(cli, stub, tmp_path, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(TASK_BODY)))
    assert cli("task", "create", "--file", "-").rc == 0
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    assert cli("task", "create", "--file", str(bad)).rc == 2
    bad.write_text("{nope")
    assert cli("task", "create", "--file", str(bad)).rc == 2
    assert cli("task", "create", "--file", str(tmp_path / "missing.json")).rc == 2


def test_list_forwards_the_filters(cli, stub):
    res = cli("task", "list", "--state", "running", "--page", "1", "--page-size", "10")
    assert res.rc == 0
    assert res.doc["total"] == 1 and res.doc["items"][0]["id"] == "task_01"
    assert _last(stub)["query"] == {"state": ["running"], "page": ["1"], "page_size": ["10"]}


def test_get(cli, stub):
    res = cli("task", "get", "task_01")
    assert res.rc == 0
    assert res.doc == stub.tasks["task_01"]            # verbatim
    rels = [ln["rel"] for ln in res.doc["links"]]
    assert rels == ["task", "report", "adjudication"]


def test_wait_polls_until_the_task_and_its_subtask_finish(cli, stub):
    sub = {"id": "sub_01", "task_id": "task_01", "kind": "retry", "scope": {},
           "state": "running", "created_at": 1}
    stub.state_script["task_01"] = [
        "queued", "running", {"state": "completed_with_errors", "active_subtask": sub},
        "succeeded"]
    res = cli("task", "wait", "task_01", "--poll-interval", "0.01")
    assert res.rc == 0
    assert res.doc["state"] == "succeeded" and res.doc["active_subtask"] is None
    polls = [r for r in stub.requests if r["path"].endswith("/tasks/task_01")]
    assert len(polls) == 4
    states = [e["msg"] for e in res.events if e["kind"] == "log"]
    assert "task task_01: running" in states
    assert any(e["kind"] == "progress" and e["stage"] == "task:vlm" for e in res.events)


def test_wait_timeout_returns_the_task_as_it_is(cli, stub):
    res = cli("task", "wait", "task_01", "--poll-interval", "0.01", "--timeout", "0.05")
    assert res.rc == 0 and res.doc["state"] == "running"
    assert any(e["kind"] == "log" and e["level"] == "warn" and "stopped waiting" in e["msg"]
               for e in res.events)


def test_actions(cli, stub):
    stub.tasks["task_02"] = make_task("task_02", "created")
    res = cli("task", "start", "task_02", "--idempotency-key", "start-task-02")
    assert res.rc == 0 and res.doc["state"] == "queued"
    assert _last(stub)["path"].endswith("/tasks/task_02/actions/start")
    assert _last(stub)["headers"]["idempotency-key"] == "start-task-02"
    res = cli("task", "pause", "task_01")
    assert res.rc == 0 and res.doc["state"] == "pausing"


def test_illegal_transition_is_a_usage_error_with_the_rest_error(cli, stub):
    res = cli("task", "resume", "task_01")                  # running cannot resume
    assert res.rc == 2
    details = res.doc["error"]["details"]
    assert details["http_status"] == 409
    assert details["rest_error"]["code"] == "task_state_conflict"
    assert details["rest_error"]["details"] == {"state": "running"}


def test_retry_and_continue(cli, stub):
    res = cli("task", "retry", "task_01", "--modules", "task_success, skill_profile")
    assert res.rc == 0
    assert res.doc["subtask"]["kind"] == "retry" and res.doc["links"]
    assert _last(stub)["body"] == {"modules": ["task_success", "skill_profile"]}
    res = cli("task", "retry", "task_01")
    assert res.rc == 0 and _last(stub)["body"] is None
    res = cli("task", "continue", "task_01")
    assert res.rc == 0 and res.doc["subtask"]["kind"] == "resume"


def test_continue_after_source_change_exits_6(cli, stub):
    stub.tasks["task_01"]["state"] = "failed"
    stub.tasks["task_01"]["state_reason"] = "source_changed"
    res = cli("task", "continue", "task_01")
    assert res.rc == 6 and res.doc["error"]["code"] == "source_changed"
    assert res.doc["error"]["details"]["rest_error"]["code"] == "source_changed"


def test_report(cli, stub):
    res = cli("task", "report", "task_01", "--rev", "3")
    assert res.rc == 0
    assert res.doc["revision"] == 3 and res.doc["report"]["revision"] == 3
    assert res.doc["links"]
    assert _last(stub)["query"] == {"rev": ["3"]}


def test_adjudication(cli, stub):
    res = cli("task", "adjudication", "task_01")
    assert res.rc == 0
    assert res.doc["task_id"] == "task_01" and res.doc["pending"] == 3
    assert res.doc["counts"] == {"decided": 1, "pending": 3, "unapplied": 1}
    assert [ln["rel"] for ln in res.doc["links"]] == ["adjudication"]
    assert res.doc["links"][0]["url"].endswith("/tasks/task_01/adjudication?status=pending")


def test_daemon_errors_map_to_exit_codes(cli, stub, monkeypatch):
    res = cli("task", "get", "task_missing")
    assert res.rc == 2 and res.doc["error"]["details"]["rest_error"]["code"] == "not_found"
    res = cli("task", "get", "task_boom")                   # 500
    assert res.rc == 3 and res.doc["error"]["details"]["http_status"] == 500
    monkeypatch.setenv("CURATOR_PASSWORD", "wrong")
    res = cli("task", "get", "task_01")
    assert res.rc == 3 and res.doc["error"]["details"]["http_status"] == 401
    assert "wrong" not in res.out + res.err


def test_connection_settings(cli, stub, monkeypatch):
    monkeypatch.delenv("CURATOR_URL")
    res = cli("task", "get", "task_01")
    assert res.rc == 2 and "CURATOR_URL" in res.doc["error"]["message"]
    assert cli("task", "get", "task_01", "--url", stub.url + "/api/v1/").rc == 0
    monkeypatch.delenv("CURATOR_PASSWORD")
    res = cli("task", "get", "task_01", "--url", stub.url)
    assert res.rc == 2 and "CURATOR_PASSWORD" in res.doc["error"]["message"]
    monkeypatch.setenv("CURATOR_PASSWORD", stub.password)
    res = cli("task", "get", "task_01", "--url", "http://127.0.0.1:9")   # nothing listens
    assert res.rc == 3 and res.doc["error"]["code"] == "input_unreachable"
    assert cli("task", "get", "task_01", "--url", "ftp://x").rc == 2


def test_human_output_shows_the_links(cli, stub):
    res = cli("task", "get", "task_01", json_mode=False)
    assert res.rc == 0
    assert "task_01  running" in res.out
    assert "3 episodes need human judgement: https://curator.example.com/curation/tasks/" \
           "task_01/adjudication?status=pending" in res.out


def test_wait_helper():
    assert task_client.finished({"state": "succeeded", "active_subtask": None})
    assert not task_client.finished({"state": "succeeded", "active_subtask": {"id": "s"}})
    assert not task_client.finished({"state": "paused"})
