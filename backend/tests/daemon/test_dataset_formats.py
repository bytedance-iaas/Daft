"""mcap and lance datasets in the repository (D44, C4 1.11): the format column and step 4; step 5
opens the adjudication line (C1 1.9)."""
from __future__ import annotations

import json
import sqlite3

from daemon.repo import migrations
from daemon.repo import protocol as P
from daemon.repo.extras import DATASET_FORMATS, dataset_format
from daemon.repo.sqlite import SqliteRepository

from .conftest import T0, sample_preflight


def _pf(kind: str, *, supported: bool = True, version=None) -> dict:
    return {"format": {"kind": kind, "version": version, "supported": supported, "detail": kind}}


def test_the_format_of_a_preflight():
    assert dataset_format(_pf("lerobot", version="v2")) == "lerobot_v2"
    assert dataset_format(_pf("lerobot", version="v3")) == "lerobot_v3"
    assert dataset_format(_pf("mcap")) == "mcap"
    assert dataset_format(_pf("lance", version="v3")) == "lance"
    assert dataset_format(_pf("mcap", supported=False)) == "unsupported"   # switched off
    assert dataset_format(_pf("lancedb", supported=False)) == "unsupported"
    assert dataset_format(_pf("rrd", supported=False)) == "unsupported"
    assert dataset_format(None) == "unsupported"
    assert set(DATASET_FORMATS) == {"lerobot_v2", "lerobot_v3", "mcap", "lance", "unsupported"}


def _v3_database(path) -> None:
    """A database at step 3 with a dataset, one of its checks and a task that refers to it."""
    conn = sqlite3.connect(path, isolation_level=None)
    for version, script in migrations.MIGRATIONS[:3]:
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute(
        "INSERT INTO dataset (id, owner_id, name, source, uri, region, preflight, format,"
        " meta_fingerprint, source_fingerprint, preflighted_at, created_at, updated_at) VALUES"
        " ('ds_1', 'default', 'old', 'tos', 'tos://b/old', 'cn-beijing', ?, 'lerobot_v2',"
        " 'sha256:m', '{}', 1, 1, 1)", (json.dumps(sample_preflight()),))
    conn.execute("INSERT INTO dataset_check (dataset_id, at, \"trigger\", result) VALUES"
                 " ('ds_1', 2, 'add', 'same')")
    conn.execute("INSERT INTO task (id, name, state, input_source, input_uri, output_uri,"
                 " delivery_key, episode_selector, params, created_at, updated_at, dataset_id)"
                 " VALUES ('task_1', 't', 'succeeded', 'tos', 'tos://b/old', 'tos://b/o',"
                 " 'tos://b/o', '{\"mode\":\"all\"}', '{}', 1, 1, 'ds_1')")
    conn.close()


def test_step_4_widens_the_format_and_keeps_every_reference(tmp_path):
    path = tmp_path / "curator.db"
    _v3_database(path)
    repo = SqliteRepository(path, clock=lambda: T0)
    try:
        assert repo.schema_version() == migrations.LATEST_VERSION >= 4
        old = repo.get_dataset("ds_1")
        assert old.name == "old" and old.uri == "tos://b/old"
        assert [c.trigger for c in repo.list_dataset_checks("ds_1", limit=5)] == ["add"]
        assert repo.get_task("task_1").dataset_id == "ds_1"       # nothing cascaded away
        for kind in ("mcap", "lance"):
            ds, created = repo.register_dataset(P.Dataset(
                id="", name=kind, source="tos", uri=f"tos://b/{kind}", region="cn-beijing",
                preflight={**sample_preflight(), **_pf(kind, version="v3" if kind == "lance"
                                                       else None)},
                meta_fingerprint="sha256:m", source_fingerprint={}, preflighted_at=T0))
            assert created
        page = repo.list_datasets(page=1, page_size=10, fmt="mcap")
        assert [d.name for d in page.items] == ["mcap"]
        assert [d.name for d in repo.list_datasets(page=1, page_size=10, fmt="lance").items] \
            == ["lance"]
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'"
                                              " AND tbl_name='dataset'")}
        assert {"idx_dataset_address", "idx_dataset_list", "idx_dataset_credential"} <= indexes
        conn.close()
    finally:
        repo.close()


def test_foreign_keys_are_on_again_after_the_rebuild(tmp_path):
    path = tmp_path / "curator.db"
    _v3_database(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    assert migrations.migrate(conn) == [v for v, _ in migrations.MIGRATIONS if v >= 4]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_step_5_opens_the_review_line_and_keeps_every_answer(tmp_path):
    """C1 1.9 (F5.11): the EEF module's line ``eef_check`` is stored like v1's; older answers keep
    their ids and their subtask."""
    path = tmp_path / "curator.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for version, script in migrations.MIGRATIONS[:4]:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute("INSERT INTO task (id, name, state, input_source, input_uri, output_uri,"
                 " delivery_key, episode_selector, params, created_at, updated_at) VALUES"
                 " ('task_1', 't', 'succeeded', 'tos', 'tos://b/x', 'tos://b/o', 'tos://b/o',"
                 " '{\"mode\":\"all\"}', '{}', 1, 1)")
    for line, decision in (("label", "keep_label"), ("reject_appeal", "restore")):
        conn.execute("INSERT INTO adjudication (task_id, episode_index, line, decision, decided_by,"
                     " decided_at) VALUES ('task_1', 3, ?, ?, 'alice', 5)", (line, decision))
    conn.close()
    repo = SqliteRepository(path, clock=lambda: T0)
    try:
        assert repo.schema_version() == migrations.LATEST_VERSION >= 5
        rows = repo.append_adjudication([P.AdjudicationCreate(
            task_id="task_1", episode_index=3, line="eef_check", decision="consistent", decided_by="bob")], at=T0)
        assert rows[0].id == 3
        latest = {a.line: (a.id, a.decision) for a in repo.latest_adjudications("task_1")}
        assert latest == {"label": (1, "keep_label"), "reject_appeal": (2, "restore"),
                          "eef_check": (3, "consistent")}
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'"
                                           " AND tbl_name='adjudication'")} >= {"idx_adj_lookup"}
        conn.close()
    finally:
        repo.close()
