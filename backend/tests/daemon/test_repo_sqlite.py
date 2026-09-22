"""SQLite specifics behind the C5 protocol (design doc 01, section 4.1)."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from daemon.repo import migrations
from daemon.repo import protocol as P
from daemon.repo.sqlite import SqliteRepository

from .conftest import T0


def _open(tmp_path, **kw):
    return SqliteRepository(tmp_path / "curator.db", clock=lambda: T0, **kw)


def test_wal_foreign_keys_and_schema_version(tmp_path):
    repo = _open(tmp_path)
    try:
        conn = sqlite3.connect(tmp_path / "curator.db")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"credential", "vlm_backend", "vlm_model", "task", "task_module", "subtask",
                "token_usage", "adjudication", "event", "preflight_cache",
                "idempotency_key", "dataset", "dataset_check", "token_timeline"} <= tables
        conn.close()
        assert repo.schema_version() == migrations.LATEST_VERSION
    finally:
        repo.close()


def test_reopening_does_not_migrate_again(tmp_path):
    repo = _open(tmp_path)
    t = repo.create_task(P.TaskCreate(name="keep", input_source="tos", input_uri="tos://b/x",
                                      output_uri="tos://b/o", delivery_key="tos://b/o",
                                      episode_selector={"mode": "all"}, params={}, modules=[]))
    repo.close()
    again = _open(tmp_path)
    try:
        assert again.get_task(t.id).name == "keep"
    finally:
        again.close()


def _v1_database(path) -> None:
    """A database at step 1, whose subtasks kept their pause reason in audit events only."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(f"BEGIN;\n{migrations.MIGRATIONS[0][1]}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute("INSERT INTO task (id, name, state, input_source, input_uri, output_uri,"
                 " delivery_key, episode_selector, params, created_at, updated_at) VALUES"
                 " ('task_1', 't', 'completed_with_errors', 'tos', 'tos://b/x', 'tos://b/o',"
                 " 'tos://b/o', '{\"mode\":\"all\"}', '{}', 1, 1)")
    for sub, state in (("sub_user", "paused"), ("sub_system", "pausing"), ("sub_unknown", "paused"),
                       ("sub_done", "succeeded")):
        conn.execute("INSERT INTO subtask (id, task_id, kind, scope, state, created_at)"
                     " VALUES (?, 'task_1', 'retry', '{}', ?, 1)", (sub, state))
    for sub, to, reason in (("sub_user", "pausing", "user"), ("sub_user", "paused", "user"),
                            ("sub_system", "pausing", "system"), ("sub_done", "pausing", "user")):
        conn.execute("INSERT INTO event (owner_id, actor, action, resource, detail, at)"
                     " VALUES ('default', 'system', 'subtask.state', 'task_1', ?, 1)",
                     (json.dumps({"subtask_id": sub, "to": to, "pause_reason": reason}),))
    conn.close()


def test_upgrading_a_first_release_database(tmp_path):
    _v1_database(tmp_path / "curator.db")
    repo = _open(tmp_path)
    try:
        assert repo.schema_version() == migrations.LATEST_VERSION
        reasons = {s.id: s.pause_reason for s in repo.list_subtasks("task_1")}
        assert reasons == {"sub_user": "user", "sub_system": "system",
                           "sub_unknown": "user",          # events gone: kept paused, as before
                           "sub_done": None}
        task = repo.get_task("task_1")
        assert task.dataset_id is None and task.state == "completed_with_errors"
        ds, created = repo.register_dataset(P.Dataset(
            id="", name="x", source="tos", uri="tos://b/x", preflight={}, meta_fingerprint="m",
            source_fingerprint={}, preflighted_at=T0))
        assert created and repo.list_datasets(page=1, page_size=5, fmt="unsupported").total == 1
        repo.update_task_fields("task_1", if_updated_at=None, dataset_id=ds.id)
        assert repo.list_tasks(page=1, page_size=5, dataset_id=ds.id).total == 1
        repo.delete_dataset(ds.id)                    # the added column keeps ON DELETE SET NULL
        assert repo.get_task("task_1").dataset_id is None
    finally:
        repo.close()


def test_dataset_address_is_unique_even_without_a_region(tmp_path):
    """SQLite never treats two NULLs as equal; the expression index does."""
    repo = _open(tmp_path)
    try:
        def insert(c):
            for _ in range(2):
                c.execute("INSERT INTO dataset (id, name, source, uri, region, preflight, format,"
                          " meta_fingerprint, source_fingerprint, preflighted_at, created_at,"
                          " updated_at) VALUES (lower(hex(randomblob(8))), 'n', 'public',"
                          " 'tos://hf/x', NULL, '{}', 'unsupported', 'm', '{}', 1, 1, 1)")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            repo._write(insert)
    finally:
        repo.close()


def test_refuses_a_database_from_a_newer_build(tmp_path):
    path = tmp_path / "curator.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {migrations.LATEST_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this Daemon"):
        SqliteRepository(path)


def test_a_failing_migration_leaves_the_previous_version(tmp_path, monkeypatch):
    repo = _open(tmp_path)
    repo.close()
    broken = migrations.MIGRATIONS + (
        (migrations.LATEST_VERSION + 1, "CREATE TABLE later (x INTEGER); SELECT * FROM no_such_table;"),)
    monkeypatch.setattr(migrations, "MIGRATIONS", broken)
    monkeypatch.setattr(migrations, "LATEST_VERSION", migrations.LATEST_VERSION + 1)
    with pytest.raises(sqlite3.OperationalError):
        _open(tmp_path)
    conn = sqlite3.connect(tmp_path / "curator.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == broken[-2][0]
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='later'").fetchone()[0] == 0
    conn.close()


def test_writes_run_on_the_single_writer_thread(tmp_path, monkeypatch):
    repo = _open(tmp_path)
    try:
        seen = set()
        original = repo._write

        def spy(fn):
            def wrapped(conn):
                seen.add(threading.current_thread().name)
                return fn(conn)
            return original(wrapped)

        monkeypatch.setattr(repo, "_write", spy)
        threads = [threading.Thread(target=lambda: repo.append_event(
            actor="a", action="b", resource="r", at=T0)) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(10)
        assert seen == {"sqlite-writer"}
    finally:
        repo.close()


def test_read_connections_are_read_only(tmp_path):
    repo = _open(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            repo._read(lambda c: c.execute("DELETE FROM task"))
    finally:
        repo.close()


def _commit_fails(c):
    """A write whose COMMIT fails - a deferred foreign key stands in for a full disk."""
    c.execute("PRAGMA defer_foreign_keys = ON")
    c.execute("INSERT INTO task_module (task_id, module_id, selected, availability, state)"
              " VALUES ('task_missing', 'x', 1, 'available', 'pending')")


def test_a_failed_commit_leaves_the_writer_usable(tmp_path):
    repo = _open(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            repo._write(_commit_fails)
        repo.append_event(actor="a", action="after", resource="r", at=T0)
        with pytest.raises(sqlite3.IntegrityError):
            with repo.transaction():
                repo._write(_commit_fails)
        with repo.transaction():
            repo.append_event(actor="a", action="in a block", resource="r", at=T0)
        assert [e.action for e in repo.list_events().items] == ["in a block", "after"]
        assert repo._read(lambda c: c.execute("SELECT COUNT(*) FROM task_module").fetchone()[0]) == 0
    finally:
        repo.close()


def test_an_aborted_transaction_refuses_further_calls(tmp_path):
    """If SQLite drops the whole transaction (SQLITE_FULL does), later calls must not autocommit."""
    from daemon.repo.sqlite import TransactionAborted

    def dropped_then_failed(c):
        c.execute("ROLLBACK")
        raise sqlite3.OperationalError("database or disk is full")

    repo = _open(tmp_path)
    try:
        with pytest.raises(TransactionAborted):
            with repo.transaction():
                repo.append_event(actor="a", action="before", resource="r", at=T0)
                with pytest.raises(sqlite3.OperationalError, match="disk is full"):
                    repo._write(dropped_then_failed)             # the caller carries on...
                repo.append_event(actor="a", action="must not land", resource="r", at=T0)
        assert repo.list_events().items == []                    # ...but nothing autocommits
        repo.append_event(actor="a", action="fine again", resource="r", at=T0)
        assert [e.action for e in repo.list_events().items] == ["fine again"]
    finally:
        repo.close()


def test_closed_repository_refuses_writes(tmp_path):
    repo = _open(tmp_path)
    repo.close()
    with pytest.raises(RuntimeError, match="closed"):
        repo.append_event(actor="a", action="b", resource="r", at=T0)
