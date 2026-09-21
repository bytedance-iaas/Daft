"""SQLite implementation of the C5 Repository (design doc 01, sections 2-4; D1).

Concurrency discipline (design doc 01, section 4.1):

* WAL journal, ``busy_timeout=5000``, foreign keys on.
* **One writer.** Every write runs on a dedicated writer thread that owns the only
  read-write connection; writes are serialized through its queue instead of
  racing for SQLite's database-level lock.
* Reads run on a per-thread read-only connection (connections are never shared
  between threads) and see the last committed state.
* ``transaction()`` pins the writer thread for the caller's block: every
  repository call the caller makes inside the block - reads included - runs on
  the writer connection inside one ``BEGIN IMMEDIATE`` ... ``COMMIT``. Nested
  use joins the outer block. An exception rolls the whole block back.
  Outside a block each method is atomic on its own.

Methods are blocking. Call them from worker threads (FastAPI runs sync handlers
in its thread pool; async code uses ``anyio.to_thread``). Never hold a
transaction across an ``await``, and never wait inside a transaction for
another thread that uses the repository - it would queue behind you.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import queue
import sqlite3
import threading
from typing import Any, Callable, Iterable, Iterator

from ..pagination import decode_cursor, encode_cursor
from ..util import new_id, now_ms
from . import migrations
from .protocol import (
    DEFAULT_OWNER,
    SUBTASK_PARENT_STATES,
    SUBTASK_TRANSITIONS,
    TASK_TRANSITIONS,
    TERMINAL_STATES,
    Adjudication,
    AdjudicationCreate,
    Conflict,
    Credential,
    CursorPage,
    Event,
    IdempotencyRecord,
    NotFound,
    PagedResult,
    PreconditionFailed,
    StateConflict,
    Subtask,
    Task,
    TaskCreate,
    TaskModule,
    UsageBucket,
    UsageDelta,
    VlmBackend,
    VlmModel,
)

#: Retention the repository applies in ``purge_expired`` (design doc 01, section 2.8).
PREFLIGHT_TTL_MS = 30 * 60 * 1000
IDEMPOTENCY_TTL_MS = 24 * 60 * 60 * 1000

#: Upper bound for one cursor page, whatever the caller asks for.
MAX_PAGE = 1000

_TERMINAL_SQL = "('stopped','succeeded','completed_with_errors','failed')"

#: Columns ``update_task_fields`` may touch (configuration; D20 decides which in what state).
_TASK_EDITABLE = frozenset({
    "name", "note", "input_source", "input_uri", "input_region", "input_cred_id",
    "output_uri", "output_region", "output_cred_id", "delivery_key", "episode_selector",
    "embodiment_id", "vlm_model_id", "vlm_reasoning_effort", "params", "preflight",
})
_TASK_JSON = frozenset({"episode_selector", "params", "vlm_snapshot", "preflight",
                        "source_fingerprint", "progress", "summary"})
_MODULE_RUNTIME_FIELDS = frozenset({"error", "input_digest", "episodes_total", "episodes_error",
                                    "started_at", "finished_at"})
_BACKEND_FIELDS = frozenset({"name", "kind", "endpoint", "credential_id", "max_concurrency"})
_MODEL_FIELDS = frozenset({"model_name", "reasoning_effort", "max_concurrency", "capabilities",
                           "source"})
_USAGE_COUNTERS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
                   "requests", "requests_unknown_usage")


# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------

class _Job:
    """A function to run on the writer connection; the caller waits for its outcome."""

    __slots__ = ("fn", "_done", "_result", "_error")

    def __init__(self, fn: Callable[[sqlite3.Connection], Any]):
        self.fn = fn
        self._done = threading.Event()
        self._result: Any = None
        self._error: BaseException | None = None

    def run(self, conn: sqlite3.Connection) -> None:
        try:
            self._result = self.fn(conn)
        except BaseException as err:          # delivered to the waiting caller
            self._error = err
        finally:
            self._done.set()

    def outcome(self, timeout: float | None = None) -> Any:
        if not self._done.wait(timeout):
            raise TimeoutError("the SQLite writer did not answer in time")
        if self._error is not None:
            raise self._error
        return self._result


class _Session:
    """Holds the writer for one explicit transaction; the caller feeds it jobs."""

    _COMMIT = object()
    _ROLLBACK = object()

    def __init__(self) -> None:
        self._calls: queue.SimpleQueue = queue.SimpleQueue()
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._finished = _Job(lambda conn: None)

    # -- writer side --------------------------------------------------------
    def run(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException as err:
            self._start_error = err
            self._started.set()
            return
        self._started.set()
        while True:
            item = self._calls.get()
            if item is self._COMMIT:
                def commit(c):
                    try:
                        c.execute("COMMIT")
                    except BaseException:
                        if c.in_transaction:
                            c.execute("ROLLBACK")
                        raise
                self._finished.fn = commit
                self._finished.run(conn)
                return
            if item is self._ROLLBACK:
                def rollback(c):
                    if c.in_transaction:
                        c.execute("ROLLBACK")
                self._finished.fn = rollback
                self._finished.run(conn)
                return
            item.run(conn)

    # -- caller side --------------------------------------------------------
    def wait_started(self) -> None:
        self._started.wait()
        if self._start_error is not None:
            raise self._start_error

    def call(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        job = _Job(fn)
        self._calls.put(job)
        return job.outcome()

    def finish(self, commit: bool) -> None:
        self._calls.put(self._COMMIT if commit else self._ROLLBACK)
        self._finished.outcome()


class _Writer:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, args=(connect,),
                                        name="sqlite-writer", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error

    def _loop(self, connect) -> None:
        try:
            conn = connect()
        except BaseException as err:
            self._startup_error = err
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                item.run(conn)
        finally:
            conn.close()

    def submit(self, item) -> None:
        if self._stopped:
            raise RuntimeError("repository is closed")
        self._queue.put(item)

    def call(self, fn: Callable[[sqlite3.Connection], Any], timeout: float | None = None) -> Any:
        job = _Job(fn)
        self.submit(job)
        return job.outcome(timeout)

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self._queue.put(None)
            self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _check_transitions(frm: Iterable[str], to: str, table: dict) -> list[str]:
    states = sorted(set(frm))
    if not states:
        raise ValueError("frm must name at least one state")
    for state in states:
        if to not in table.get(state, frozenset()):
            raise ValueError(f"illegal transition {state} -> {to} (C5 never allows it)")
    return states


def _is_unique_violation(err: sqlite3.IntegrityError) -> bool:
    return "UNIQUE" in str(err) or "PRIMARY KEY" in str(err)


def _credential(row) -> Credential:
    return Credential(
        id=row["id"], name=row["name"], kind=row["kind"], payload_enc=bytes(row["payload_enc"]),
        key_version=row["key_version"], payload_meta=_loads(row["payload_meta"]) or {},
        verify_state=row["verify_state"], last_verified_at=row["last_verified_at"],
        last_verify_error=row["last_verify_error"], owner_id=row["owner_id"],
        created_at=row["created_at"], updated_at=row["updated_at"])


def _model(row) -> VlmModel:
    return VlmModel(
        id=row["id"], backend_id=row["backend_id"], model_name=row["model_name"],
        reasoning_effort=row["reasoning_effort"], max_concurrency=row["max_concurrency"],
        capabilities=_loads(row["capabilities"]) or {}, source=row["source"],
        created_at=row["created_at"], updated_at=row["updated_at"])


def _backend(row, models: list[VlmModel]) -> VlmBackend:
    return VlmBackend(
        id=row["id"], name=row["name"], kind=row["kind"], endpoint=row["endpoint"],
        credential_id=row["credential_id"], max_concurrency=row["max_concurrency"],
        verify_state=row["verify_state"], last_verified_at=row["last_verified_at"],
        last_verify_error=row["last_verify_error"], models=models, owner_id=row["owner_id"],
        created_at=row["created_at"], updated_at=row["updated_at"])


def _task(row) -> Task:
    return Task(
        id=row["id"], name=row["name"], state=row["state"], input_source=row["input_source"],
        input_uri=row["input_uri"], output_uri=row["output_uri"], delivery_key=row["delivery_key"],
        episode_selector=_loads(row["episode_selector"]), params=_loads(row["params"]),
        note=row["note"], state_reason=row["state_reason"], pause_reason=row["pause_reason"],
        input_region=row["input_region"], input_cred_id=row["input_cred_id"],
        output_region=row["output_region"], output_cred_id=row["output_cred_id"],
        embodiment_id=row["embodiment_id"], vlm_model_id=row["vlm_model_id"],
        vlm_reasoning_effort=row["vlm_reasoning_effort"],
        vlm_snapshot=_loads(row["vlm_snapshot"]), preflight=_loads(row["preflight"]),
        source_fingerprint=_loads(row["source_fingerprint"]), result_rev=row["result_rev"],
        export_fingerprint=row["export_fingerprint"], run_id=row["run_id"],
        progress=_loads(row["progress"]), summary=_loads(row["summary"]),
        delivery_stale=bool(row["delivery_stale"]), started_at=row["started_at"],
        finished_at=row["finished_at"], deleted_at=row["deleted_at"], owner_id=row["owner_id"],
        created_at=row["created_at"], updated_at=row["updated_at"])


def _task_module(row) -> TaskModule:
    return TaskModule(
        task_id=row["task_id"], module_id=row["module_id"], selected=bool(row["selected"]),
        availability=row["availability"], state=row["state"],
        unavailable_reason=row["unavailable_reason"], error=row["error"],
        input_digest=row["input_digest"], episodes_total=row["episodes_total"],
        episodes_error=row["episodes_error"], params=_loads(row["params"]),
        started_at=row["started_at"], finished_at=row["finished_at"])


def _subtask(row) -> Subtask:
    return Subtask(
        id=row["id"], task_id=row["task_id"], kind=row["kind"], scope=_loads(row["scope"]) or {},
        state=row["state"], state_reason=row["state_reason"], progress=_loads(row["progress"]),
        result_rev=row["result_rev"], created_at=row["created_at"],
        started_at=row["started_at"], finished_at=row["finished_at"])


def _adjudication(row) -> Adjudication:
    return Adjudication(
        id=row["id"], task_id=row["task_id"], episode_index=row["episode_index"],
        line=row["line"], decision=row["decision"], decided_by=row["decided_by"],
        decided_at=row["decided_at"], new_label=row["new_label"], note=row["note"],
        applied_in_subtask=row["applied_in_subtask"], owner_id=row["owner_id"])


def _event(row) -> Event:
    return Event(id=row["id"], actor=row["actor"], action=row["action"],
                 resource=row["resource"], at=row["at"], detail=_loads(row["detail"]),
                 owner_id=row["owner_id"])


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------

class SqliteRepository:
    """C5 ``Repository`` on one SQLite file. See the module docstring for the rules."""

    def __init__(self, path: str | os.PathLike, *, clock: Callable[[], int] = now_ms,
                 busy_timeout_ms: int = 5000):
        self.path = os.fspath(path)
        self._clock = clock
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._writer = _Writer(self._connect_writer)
        try:
            self._writer.call(migrations.migrate)
        except BaseException:
            self._writer.stop()
            raise

    # -- connections -----------------------------------------------------------
    def _configure(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _connect_writer(self) -> sqlite3.Connection:
        conn = self._configure(sqlite3.connect(self.path, isolation_level=None))
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.close()
            raise RuntimeError(f"SQLite refused WAL mode (got {mode!r}); the data volume "
                               "must be a local block device, not a network file system")
        return conn

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "reader", None)
        if conn is None:
            conn = self._configure(sqlite3.connect(self.path, isolation_level=None))
            conn.execute("PRAGMA query_only = ON")
            self._local.reader = conn
        return conn

    def close(self) -> None:
        """Stop the writer; per-thread read connections close with their threads."""
        reader = getattr(self._local, "reader", None)
        if reader is not None:
            reader.close()
            self._local.reader = None
        self._writer.stop()

    # -- execution helpers ---------------------------------------------------------
    def _session(self) -> _Session | None:
        return getattr(self._local, "session", None)

    def _write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run ``fn`` atomically on the writer: inside the caller's transaction, or its own."""
        session = self._session()
        if session is not None:
            return session.call(fn)

        def atomic(conn: sqlite3.Connection) -> Any:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

        return self._writer.call(atomic)

    def _read(self, fn: Callable[[sqlite3.Connection], Any], *, snapshot: bool = False) -> Any:
        """Run ``fn`` on this thread's reader (or inside the caller's transaction)."""
        session = self._session()
        if session is not None:
            return session.call(fn)
        conn = self._reader()
        if not snapshot:
            return fn(conn)
        conn.execute("BEGIN")
        try:
            return fn(conn)
        finally:
            conn.execute("COMMIT")

    # -- transactions and schema -------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        if self._session() is not None:
            yield
            return
        session = _Session()
        self._writer.submit(session)
        session.wait_started()
        self._local.session = session
        try:
            yield
        except BaseException:
            self._local.session = None
            with contextlib.suppress(Exception):   # the original error is the one to report
                session.finish(commit=False)
            raise
        else:
            self._local.session = None
            session.finish(commit=True)

    def schema_version(self) -> int:
        return self._read(lambda c: c.execute("PRAGMA user_version").fetchone()[0])

    # -- credentials ---------------------------------------------------------------
    def create_credential(self, cred: Credential) -> Credential:
        now = self._clock()
        cred_id = cred.id or new_id("cred")

        def op(c):
            try:
                c.execute(
                    "INSERT INTO credential (id, owner_id, name, kind, payload_enc, key_version,"
                    " payload_meta, verify_state, last_verified_at, last_verify_error,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cred_id, cred.owner_id, cred.name, cred.kind, bytes(cred.payload_enc),
                     cred.key_version, _dumps(cred.payload_meta or {}), cred.verify_state,
                     cred.last_verified_at, cred.last_verify_error, cred.created_at or now,
                     cred.updated_at or now))
            except sqlite3.IntegrityError as err:
                if _is_unique_violation(err):
                    raise Conflict("name_taken", f"credential name {cred.name!r} exists") from None
                raise
            return self._get_credential(c, cred_id, cred.owner_id)

        return self._write(op)

    @staticmethod
    def _get_credential(c, cred_id: str, owner: str) -> Credential:
        row = c.execute("SELECT * FROM credential WHERE id=? AND owner_id=?",
                        (cred_id, owner)).fetchone()
        if row is None:
            raise NotFound(f"credential {cred_id}")
        return _credential(row)

    def get_credential(self, cred_id: str, *, owner: str = DEFAULT_OWNER) -> Credential:
        return self._read(lambda c: self._get_credential(c, cred_id, owner))

    def get_credential_by_name(self, name: str, *, owner: str = DEFAULT_OWNER) -> Credential:
        def op(c):
            row = c.execute("SELECT * FROM credential WHERE name=? AND owner_id=?",
                            (name, owner)).fetchone()
            if row is None:
                raise NotFound(f"credential named {name!r}")
            return _credential(row)
        return self._read(op)

    def list_credentials(self, *, owner: str = DEFAULT_OWNER,
                         kind: str | None = None) -> list[Credential]:
        sql = "SELECT * FROM credential WHERE owner_id=?"
        args: list[Any] = [owner]
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created_at, id"
        return self._read(lambda c: [_credential(r) for r in c.execute(sql, args).fetchall()])

    def update_credential(self, cred_id: str, *, owner: str = DEFAULT_OWNER, name: str | None = None,
                          payload_enc: bytes | None = None, key_version: int | None = None,
                          payload_meta: dict | None = None) -> Credential:
        sets: dict[str, Any] = {}
        if name is not None:
            sets["name"] = name
        if payload_enc is not None:
            sets["payload_enc"] = bytes(payload_enc)
        if key_version is not None:
            sets["key_version"] = int(key_version)
        if payload_meta is not None:
            sets["payload_meta"] = _dumps(payload_meta)
        now = self._clock()

        def op(c):
            current = self._get_credential(c, cred_id, owner)
            if not sets:
                return current
            cols = ", ".join(f"{k}=?" for k in sets)
            try:
                c.execute(f"UPDATE credential SET {cols}, updated_at=MAX(?, updated_at + 1)"
                          " WHERE id=? AND owner_id=?", (*sets.values(), now, cred_id, owner))
            except sqlite3.IntegrityError as err:
                if _is_unique_violation(err):
                    raise Conflict("name_taken", f"credential name {name!r} exists") from None
                raise
            return self._get_credential(c, cred_id, owner)

        return self._write(op)

    def set_credential_verification(self, cred_id: str, state: str, at: int,
                                    error: str | None) -> None:
        def op(c):
            cur = c.execute("UPDATE credential SET verify_state=?, last_verified_at=?,"
                            " last_verify_error=? WHERE id=?", (state, at, error, cred_id))
            if cur.rowcount == 0:
                raise NotFound(f"credential {cred_id}")
        self._write(op)

    @staticmethod
    def _credential_refs(c, cred_id: str) -> tuple[int, int]:
        row = c.execute(
            "SELECT"
            f" SUM(CASE WHEN state NOT IN {_TERMINAL_SQL} AND deleted_at IS NULL THEN 1 ELSE 0 END),"
            f" SUM(CASE WHEN state IN {_TERMINAL_SQL} OR deleted_at IS NOT NULL THEN 1 ELSE 0 END)"
            " FROM task WHERE input_cred_id=? OR output_cred_id=?", (cred_id, cred_id)).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def credential_references(self, cred_id: str) -> tuple[int, int]:
        return self._read(lambda c: self._credential_refs(c, cred_id), snapshot=True)

    def delete_credential(self, cred_id: str, *, owner: str = DEFAULT_OWNER) -> None:
        def op(c):
            self._get_credential(c, cred_id, owner)
            active, _ = self._credential_refs(c, cred_id)
            if active:
                raise Conflict("credential_in_use",
                               f"credential {cred_id} is used by {active} unfinished task(s)")
            backends = c.execute("SELECT COUNT(*) FROM vlm_backend WHERE credential_id=?",
                                 (cred_id,)).fetchone()[0]
            if backends:
                raise Conflict("credential_in_use",
                               f"credential {cred_id} belongs to a VLM backend; delete the backend")
            c.execute("DELETE FROM credential WHERE id=? AND owner_id=?", (cred_id, owner))
        self._write(op)

    def credentials_below_key_version(self, version: int, *, limit: int = 100) -> list[Credential]:
        return self._read(lambda c: [_credential(r) for r in c.execute(
            "SELECT * FROM credential WHERE key_version < ? ORDER BY id LIMIT ?",
            (version, max(1, int(limit)))).fetchall()])

    # -- VLM backends and models -------------------------------------------------
    @staticmethod
    def _models_of(c, backend_id: str) -> list[VlmModel]:
        return [_model(r) for r in c.execute(
            "SELECT * FROM vlm_model WHERE backend_id=? ORDER BY created_at, id",
            (backend_id,)).fetchall()]

    def _get_backend(self, c, where: str, args: tuple, what: str) -> VlmBackend:
        row = c.execute(f"SELECT * FROM vlm_backend WHERE {where}", args).fetchone()
        if row is None:
            raise NotFound(what)
        return _backend(row, self._models_of(c, row["id"]))

    def _insert_model(self, c, model: VlmModel, now: int) -> str:
        model_id = model.id or new_id("vm")
        c.execute("INSERT INTO vlm_model (id, backend_id, model_name, reasoning_effort,"
                  " max_concurrency, capabilities, source, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (model_id, model.backend_id, model.model_name, model.reasoning_effort,
                   model.max_concurrency, _dumps(model.capabilities or {}), model.source,
                   model.created_at or now, model.updated_at or now))
        return model_id

    def create_vlm_backend(self, backend: VlmBackend, credential: Credential | None) -> VlmBackend:
        now = self._clock()
        backend_id = backend.id or new_id("vb")

        def op(c):
            cred_id = backend.credential_id
            if credential is not None:
                cred_id = credential.id or new_id("cred")
                try:
                    c.execute(
                        "INSERT INTO credential (id, owner_id, name, kind, payload_enc, key_version,"
                        " payload_meta, verify_state, last_verified_at, last_verify_error,"
                        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cred_id, credential.owner_id, credential.name, credential.kind,
                         bytes(credential.payload_enc), credential.key_version,
                         _dumps(credential.payload_meta or {}), credential.verify_state,
                         credential.last_verified_at, credential.last_verify_error,
                         credential.created_at or now, credential.updated_at or now))
                except sqlite3.IntegrityError as err:
                    if _is_unique_violation(err):
                        raise Conflict("name_taken",
                                       f"credential name {credential.name!r} exists") from None
                    raise
            try:
                c.execute(
                    "INSERT INTO vlm_backend (id, owner_id, name, kind, credential_id, endpoint,"
                    " max_concurrency, verify_state, last_verified_at, last_verify_error,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (backend_id, backend.owner_id, backend.name, backend.kind, cred_id,
                     backend.endpoint, backend.max_concurrency, backend.verify_state,
                     backend.last_verified_at, backend.last_verify_error,
                     backend.created_at or now, backend.updated_at or now))
            except sqlite3.IntegrityError as err:
                if _is_unique_violation(err):
                    raise Conflict("name_taken", f"backend name {backend.name!r} exists") from None
                raise
            for model in backend.models:
                self._insert_model(c, dataclasses.replace(model, backend_id=backend_id), now)
            return self._get_backend(c, "id=?", (backend_id,), f"backend {backend_id}")

        return self._write(op)

    def get_vlm_backend(self, backend_id: str, *, owner: str = DEFAULT_OWNER) -> VlmBackend:
        return self._read(lambda c: self._get_backend(
            c, "id=? AND owner_id=?", (backend_id, owner), f"backend {backend_id}"), snapshot=True)

    def get_vlm_backend_by_name(self, name: str, *, owner: str = DEFAULT_OWNER) -> VlmBackend:
        return self._read(lambda c: self._get_backend(
            c, "name=? AND owner_id=?", (name, owner), f"backend named {name!r}"), snapshot=True)

    def list_vlm_backends(self, *, owner: str = DEFAULT_OWNER) -> list[VlmBackend]:
        def op(c):
            rows = c.execute("SELECT * FROM vlm_backend WHERE owner_id=? ORDER BY created_at, id",
                             (owner,)).fetchall()
            return [_backend(r, self._models_of(c, r["id"])) for r in rows]
        return self._read(op, snapshot=True)

    def update_vlm_backend(self, backend_id: str, *, owner: str = DEFAULT_OWNER,
                           **fields) -> VlmBackend:
        unknown = set(fields) - _BACKEND_FIELDS
        if unknown:
            raise ValueError(f"not editable on a backend: {sorted(unknown)}")
        now = self._clock()

        def op(c):
            self._get_backend(c, "id=? AND owner_id=?", (backend_id, owner), f"backend {backend_id}")
            if fields:
                cols = ", ".join(f"{k}=?" for k in fields)
                try:
                    c.execute(f"UPDATE vlm_backend SET {cols}, updated_at=MAX(?, updated_at + 1)"
                              " WHERE id=? AND owner_id=?",
                              (*fields.values(), now, backend_id, owner))
                except sqlite3.IntegrityError as err:
                    if _is_unique_violation(err):
                        raise Conflict("name_taken",
                                       f"backend name {fields.get('name')!r} exists") from None
                    raise
            return self._get_backend(c, "id=?", (backend_id,), f"backend {backend_id}")

        return self._write(op)

    def set_vlm_backend_verification(self, backend_id: str, state: str, at: int,
                                     error: str | None) -> None:
        def op(c):
            cur = c.execute("UPDATE vlm_backend SET verify_state=?, last_verified_at=?,"
                            " last_verify_error=? WHERE id=?", (state, at, error, backend_id))
            if cur.rowcount == 0:
                raise NotFound(f"backend {backend_id}")
        self._write(op)

    @staticmethod
    def _active_model_users(c, where: str, args: tuple) -> int:
        return c.execute(
            f"SELECT COUNT(*) FROM task WHERE vlm_model_id IN (SELECT id FROM vlm_model WHERE {where})"
            f" AND state NOT IN {_TERMINAL_SQL} AND deleted_at IS NULL", args).fetchone()[0]

    def delete_vlm_backend(self, backend_id: str, *, owner: str = DEFAULT_OWNER) -> None:
        def op(c):
            backend = self._get_backend(c, "id=? AND owner_id=?", (backend_id, owner),
                                        f"backend {backend_id}")
            if self._active_model_users(c, "backend_id=?", (backend_id,)):
                raise Conflict("backend_in_use",
                               f"backend {backend_id} is used by an unfinished task")
            c.execute("DELETE FROM vlm_backend WHERE id=?", (backend_id,))
            if backend.credential_id:
                others = c.execute("SELECT COUNT(*) FROM vlm_backend WHERE credential_id=?",
                                   (backend.credential_id,)).fetchone()[0]
                if not others:
                    c.execute("DELETE FROM credential WHERE id=? AND kind IN ('ark','custom_vlm')",
                              (backend.credential_id,))
        self._write(op)

    def upsert_vlm_model(self, model: VlmModel) -> VlmModel:
        now = self._clock()

        def op(c):
            if c.execute("SELECT 1 FROM vlm_backend WHERE id=?", (model.backend_id,)).fetchone() is None:
                raise NotFound(f"backend {model.backend_id}")
            row = c.execute("SELECT id FROM vlm_model WHERE backend_id=? AND model_name=?",
                            (model.backend_id, model.model_name)).fetchone()
            if row is None:
                model_id = self._insert_model(c, model, now)
            else:
                model_id = row["id"]
                c.execute("UPDATE vlm_model SET reasoning_effort=?, max_concurrency=?,"
                          " capabilities=?, source=?, updated_at=MAX(?, updated_at + 1) WHERE id=?",
                          (model.reasoning_effort, model.max_concurrency,
                           _dumps(model.capabilities or {}), model.source, now, model_id))
            return _model(c.execute("SELECT * FROM vlm_model WHERE id=?", (model_id,)).fetchone())

        return self._write(op)

    def update_vlm_model(self, model_id: str, **fields) -> VlmModel:
        unknown = set(fields) - _MODEL_FIELDS
        if unknown:
            raise ValueError(f"not editable on a model: {sorted(unknown)}")
        values = {k: (_dumps(v or {}) if k == "capabilities" else v) for k, v in fields.items()}
        now = self._clock()

        def op(c):
            if c.execute("SELECT 1 FROM vlm_model WHERE id=?", (model_id,)).fetchone() is None:
                raise NotFound(f"model {model_id}")
            if values:
                cols = ", ".join(f"{k}=?" for k in values)
                try:
                    c.execute(f"UPDATE vlm_model SET {cols}, updated_at=MAX(?, updated_at + 1)"
                              " WHERE id=?", (*values.values(), now, model_id))
                except sqlite3.IntegrityError as err:
                    if _is_unique_violation(err):
                        raise Conflict("name_taken", "model already listed on this backend") from None
                    raise
            return _model(c.execute("SELECT * FROM vlm_model WHERE id=?", (model_id,)).fetchone())

        return self._write(op)

    def delete_vlm_model(self, model_id: str) -> None:
        def op(c):
            if c.execute("SELECT 1 FROM vlm_model WHERE id=?", (model_id,)).fetchone() is None:
                raise NotFound(f"model {model_id}")
            if self._active_model_users(c, "id=?", (model_id,)):
                raise Conflict("backend_in_use", f"model {model_id} is used by an unfinished task")
            c.execute("DELETE FROM vlm_model WHERE id=?", (model_id,))
        self._write(op)

    # -- tasks ---------------------------------------------------------------------
    @staticmethod
    def _get_task(c, task_id: str, owner: str | None = None, include_deleted: bool = True) -> Task:
        sql, args = "SELECT * FROM task WHERE id=?", [task_id]
        if owner is not None:
            sql += " AND owner_id=?"
            args.append(owner)
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = c.execute(sql, args).fetchone()
        if row is None:
            raise NotFound(f"task {task_id}")
        return _task(row)

    @staticmethod
    def _upsert_modules(c, task_id: str, rows: Iterable[TaskModule]) -> None:
        for m in rows:
            c.execute(
                "INSERT INTO task_module (task_id, module_id, selected, availability,"
                " unavailable_reason, state, error, input_digest, episodes_total, episodes_error,"
                " params, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(task_id, module_id) DO UPDATE SET selected=excluded.selected,"
                " availability=excluded.availability, unavailable_reason=excluded.unavailable_reason,"
                " state=excluded.state, error=excluded.error, input_digest=excluded.input_digest,"
                " episodes_total=excluded.episodes_total, episodes_error=excluded.episodes_error,"
                " params=excluded.params, started_at=excluded.started_at,"
                " finished_at=excluded.finished_at",
                (task_id, m.module_id, int(bool(m.selected)), m.availability, m.unavailable_reason,
                 m.state, m.error, m.input_digest, int(m.episodes_total), int(m.episodes_error),
                 _dumps(m.params), m.started_at, m.finished_at))

    def create_task(self, spec: TaskCreate) -> Task:
        if spec.state not in ("created", "queued"):
            raise ValueError(f"a task starts as created or queued, not {spec.state}")
        now = self._clock()
        task_id = new_id("task")

        def op(c):
            c.execute(
                "INSERT INTO task (id, owner_id, name, note, state, input_source, input_uri,"
                " input_region, input_cred_id, output_uri, output_region, output_cred_id,"
                " delivery_key, episode_selector, embodiment_id, vlm_model_id,"
                " vlm_reasoning_effort, params, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, spec.owner_id, spec.name, spec.note, spec.state, spec.input_source,
                 spec.input_uri, spec.input_region, spec.input_cred_id, spec.output_uri,
                 spec.output_region, spec.output_cred_id, spec.delivery_key,
                 _dumps(spec.episode_selector), spec.embodiment_id, spec.vlm_model_id,
                 spec.vlm_reasoning_effort, _dumps(spec.params or {}), now, now))
            self._upsert_modules(c, task_id, spec.modules)
            return self._get_task(c, task_id)

        return self._write(op)

    def get_task(self, task_id: str, *, owner: str = DEFAULT_OWNER,
                 include_deleted: bool = False) -> Task:
        return self._read(lambda c: self._get_task(c, task_id, owner, include_deleted))

    def list_tasks(self, *, owner: str = DEFAULT_OWNER, page: int, page_size: int,
                   state: str | None = None, q: str | None = None,
                   delivery_key: str | None = None) -> PagedResult[Task]:
        page, page_size = int(page), int(page_size)
        if page < 1 or page_size < 1:
            raise ValueError("page and page_size start at 1")
        where, args = ["owner_id=?"], [owner]
        if state == "deleted":
            where.append("deleted_at IS NOT NULL")
        else:
            where.append("deleted_at IS NULL")
            if state is not None:
                where.append("state=?")
                args.append(state)
        if q:
            needle = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            args += [f"%{needle}%", f"%{needle}%"]
        if delivery_key is not None:
            where.append("delivery_key=?")
            args.append(delivery_key)
        clause = " AND ".join(where)

        def op(c):
            total = c.execute(f"SELECT COUNT(*) FROM task WHERE {clause}", args).fetchone()[0]
            rows = c.execute(f"SELECT * FROM task WHERE {clause} ORDER BY created_at DESC, id DESC"
                             " LIMIT ? OFFSET ?", (*args, page_size, (page - 1) * page_size)).fetchall()
            return PagedResult(items=[_task(r) for r in rows], page=page, page_size=page_size,
                               total=total)

        return self._read(op, snapshot=True)

    def update_task_fields(self, task_id: str, *, if_updated_at: int | None,
                           owner: str = DEFAULT_OWNER, **fields) -> Task:
        unknown = set(fields) - _TASK_EDITABLE
        if unknown:
            raise ValueError(f"not editable through update_task_fields: {sorted(unknown)}")
        values = {k: (_dumps(v) if k in _TASK_JSON else v) for k, v in fields.items()}
        now = self._clock()

        def op(c):
            current = self._get_task(c, task_id, owner, include_deleted=False)
            if if_updated_at is not None and int(if_updated_at) != current.updated_at:
                raise PreconditionFailed(
                    f"task {task_id} changed (updated_at {current.updated_at} != {if_updated_at})")
            if not values:
                return current
            cols = ", ".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE task SET {cols}, updated_at=MAX(?, updated_at + 1) WHERE id=?",
                      (*values.values(), now, task_id))
            return self._get_task(c, task_id)

        return self._write(op)

    def update_task_state(self, task_id: str, frm, to: str, *, reason: str | None = None,
                          pause_reason: str | None = None, at: int) -> bool:
        states = _check_transitions(frm, to, TASK_TRANSITIONS)
        pausing = to in ("pausing", "paused")
        terminal = to in TERMINAL_STATES

        def op(c):
            cur = c.execute(
                "UPDATE task SET state=?, state_reason=?,"
                " pause_reason=CASE WHEN ? THEN COALESCE(?, pause_reason) ELSE NULL END,"
                " started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END,"
                " finished_at=CASE WHEN ? THEN COALESCE(finished_at, ?) ELSE finished_at END,"
                " updated_at=MAX(?, updated_at + 1)"
                f" WHERE id=? AND deleted_at IS NULL AND state IN ({_placeholders(len(states))})",
                (to, reason, int(pausing), pause_reason, to, at, int(terminal), at, at, task_id,
                 *states))
            return cur.rowcount == 1

        return self._write(op)

    def _set_task_column(self, task_id: str, column: str, value: Any) -> None:
        def op(c):
            cur = c.execute(f"UPDATE task SET {column}=? WHERE id=?", (value, task_id))
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
        self._write(op)

    def set_task_progress(self, task_id: str, progress: dict) -> None:
        self._set_task_column(task_id, "progress", _dumps(progress))

    def set_task_summary(self, task_id: str, summary: dict) -> None:
        self._set_task_column(task_id, "summary", _dumps(summary))

    def switch_result_rev(self, task_id: str, expected: int, new: int) -> bool:
        if int(new) <= int(expected):
            raise ValueError("result revisions only move forward")

        def op(c):
            cur = c.execute("UPDATE task SET result_rev=? WHERE id=? AND result_rev=?",
                            (int(new), task_id, int(expected)))
            if cur.rowcount == 0:
                self._get_task(c, task_id)            # NotFound when missing
                return False
            return True

        return self._write(op)

    def set_export_fingerprint(self, task_id: str, fingerprint: str | None,
                               delivery_stale: bool) -> None:
        def op(c):
            cur = c.execute("UPDATE task SET export_fingerprint=?, delivery_stale=? WHERE id=?",
                            (fingerprint, int(bool(delivery_stale)), task_id))
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
        self._write(op)

    def freeze_task_inputs(self, task_id: str, *, run_id: str, preflight: dict,
                           source_fingerprint: dict, vlm_snapshot: dict | None) -> None:
        def op(c):
            cur = c.execute("UPDATE task SET run_id=?, preflight=?, source_fingerprint=?,"
                            " vlm_snapshot=? WHERE id=?",
                            (run_id, _dumps(preflight), _dumps(source_fingerprint),
                             _dumps(vlm_snapshot), task_id))
            if cur.rowcount == 0:
                raise NotFound(f"task {task_id}")
        self._write(op)

    def rebind_task_credentials(self, task_id: str, *, input_cred_id: str | None,
                                output_cred_id: str | None) -> Task:
        """``None`` leaves that side unchanged (the API rebinds one side or both)."""
        sets = {k: v for k, v in (("input_cred_id", input_cred_id),
                                  ("output_cred_id", output_cred_id)) if v is not None}
        now = self._clock()

        def op(c):
            self._get_task(c, task_id)
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                try:
                    c.execute(f"UPDATE task SET {cols}, updated_at=MAX(?, updated_at + 1)"
                              " WHERE id=?", (*sets.values(), now, task_id))
                except sqlite3.IntegrityError:
                    raise NotFound("credential to bind does not exist") from None
            return self._get_task(c, task_id)

        return self._write(op)

    def soft_delete_task(self, task_id: str, *, at: int) -> None:
        """Only ``created`` or terminal tasks without an active subtask (D28, 01 §2.5)."""
        def op(c):
            task = self._get_task(c, task_id, include_deleted=False)
            if task.state != "created" and task.state not in TERMINAL_STATES:
                raise StateConflict(f"task {task_id} is {task.state}; stop it before deleting")
            if self._active_subtask(c, task_id) is not None:
                raise StateConflict(f"task {task_id} has a subtask running")
            c.execute("UPDATE task SET deleted_at=?, updated_at=MAX(?, updated_at + 1) WHERE id=?",
                      (at, at, task_id))
        self._write(op)

    def restore_task(self, task_id: str) -> Task:
        now = self._clock()

        def op(c):
            task = self._get_task(c, task_id)
            if task.deleted_at is not None:
                c.execute("UPDATE task SET deleted_at=NULL, updated_at=MAX(?, updated_at + 1)"
                          " WHERE id=?", (now, task_id))
            return self._get_task(c, task_id)

        return self._write(op)

    def purge_deleted_tasks(self, *, before: int) -> int:
        return self._write(lambda c: c.execute(
            "DELETE FROM task WHERE deleted_at IS NOT NULL AND deleted_at < ?", (before,)).rowcount)

    def tasks_in_states(self, states: Iterable[str]) -> list[Task]:
        states = sorted(set(states))
        if not states:
            return []
        return self._read(lambda c: [_task(r) for r in c.execute(
            f"SELECT * FROM task WHERE state IN ({_placeholders(len(states))})"
            " ORDER BY created_at, id", states).fetchall()])

    # -- task modules ----------------------------------------------------------------
    def get_task_modules(self, task_id: str) -> list[TaskModule]:
        return self._read(lambda c: [_task_module(r) for r in c.execute(
            "SELECT * FROM task_module WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()])

    def upsert_task_modules(self, task_id: str, rows: list[TaskModule]) -> None:
        def op(c):
            self._get_task(c, task_id)
            self._upsert_modules(c, task_id, rows)
        self._write(op)

    def update_module_state(self, task_id: str, module_id: str, frm, to: str, **fields) -> bool:
        unknown = set(fields) - _MODULE_RUNTIME_FIELDS
        if unknown:
            raise ValueError(f"not a module runtime field: {sorted(unknown)}")
        states = sorted(set(frm))
        if not states:
            raise ValueError("frm must name at least one state")
        cols = "".join(f", {k}=?" for k in fields)

        def op(c):
            cur = c.execute(
                f"UPDATE task_module SET state=?{cols} WHERE task_id=? AND module_id=?"
                f" AND state IN ({_placeholders(len(states))})",
                (to, *fields.values(), task_id, module_id, *states))
            return cur.rowcount == 1

        return self._write(op)

    def mark_modules_stale(self, task_id: str, modules: list[str]) -> None:
        if not modules:
            return
        self._write(lambda c: c.execute(
            "UPDATE task_module SET state='stale' WHERE task_id=?"
            f" AND module_id IN ({_placeholders(len(modules))})"
            " AND state IN ('succeeded','completed_with_errors')", (task_id, *modules)))

    # -- subtasks ------------------------------------------------------------------------
    @staticmethod
    def _active_subtask(c, task_id: str) -> Subtask | None:
        row = c.execute(f"SELECT * FROM subtask WHERE task_id=? AND state NOT IN {_TERMINAL_SQL}"
                        " ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        return None if row is None else _subtask(row)

    def create_subtask(self, subtask: Subtask) -> Subtask:
        if subtask.state not in SUBTASK_TRANSITIONS:
            raise ValueError(f"a subtask cannot be {subtask.state}")
        if subtask.kind not in SUBTASK_PARENT_STATES:
            raise ValueError(f"unknown subtask kind {subtask.kind}")
        now = self._clock()
        sub_id = subtask.id or new_id("sub")

        def op(c):
            parent = self._get_task(c, subtask.task_id, include_deleted=False)
            if parent.state not in SUBTASK_PARENT_STATES[subtask.kind]:
                raise StateConflict(f"a {subtask.kind} subtask cannot start while the task is "
                                    f"{parent.state}")
            if self._active_subtask(c, subtask.task_id) is not None:
                raise Conflict("subtask_active",
                               f"task {subtask.task_id} already has an unfinished subtask")
            c.execute("INSERT INTO subtask (id, task_id, kind, scope, state, state_reason, progress,"
                      " result_rev, created_at, started_at, finished_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (sub_id, subtask.task_id, subtask.kind, _dumps(subtask.scope or {}),
                       subtask.state, subtask.state_reason, _dumps(subtask.progress),
                       subtask.result_rev, subtask.created_at or now, subtask.started_at,
                       subtask.finished_at))
            return _subtask(c.execute("SELECT * FROM subtask WHERE id=?", (sub_id,)).fetchone())

        return self._write(op)

    def get_subtask(self, subtask_id: str) -> Subtask:
        def op(c):
            row = c.execute("SELECT * FROM subtask WHERE id=?", (subtask_id,)).fetchone()
            if row is None:
                raise NotFound(f"subtask {subtask_id}")
            return _subtask(row)
        return self._read(op)

    def list_subtasks(self, task_id: str) -> list[Subtask]:
        return self._read(lambda c: [_subtask(r) for r in c.execute(
            "SELECT * FROM subtask WHERE task_id=? ORDER BY created_at, id", (task_id,)).fetchall()])

    def active_subtask(self, task_id: str) -> Subtask | None:
        return self._read(lambda c: self._active_subtask(c, task_id))

    def update_subtask_state(self, subtask_id: str, frm, to: str, *, reason: str | None = None,
                             at: int) -> bool:
        states = _check_transitions(frm, to, SUBTASK_TRANSITIONS)
        terminal = to in TERMINAL_STATES

        def op(c):
            cur = c.execute(
                "UPDATE subtask SET state=?, state_reason=?,"
                " started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END,"
                " finished_at=CASE WHEN ? THEN COALESCE(finished_at, ?) ELSE finished_at END"
                f" WHERE id=? AND state IN ({_placeholders(len(states))})",
                (to, reason, to, at, int(terminal), at, subtask_id, *states))
            return cur.rowcount == 1

        return self._write(op)

    def set_subtask_progress(self, subtask_id: str, progress: dict) -> None:
        def op(c):
            cur = c.execute("UPDATE subtask SET progress=? WHERE id=?", (_dumps(progress), subtask_id))
            if cur.rowcount == 0:
                raise NotFound(f"subtask {subtask_id}")
        self._write(op)

    # -- token usage -------------------------------------------------------------------------
    def add_usage(self, deltas: list[UsageDelta], *, at: int) -> None:
        if not deltas:
            return
        counters = ", ".join(_USAGE_COUNTERS)
        adds = ", ".join(f"{k}={k}+excluded.{k}" for k in _USAGE_COUNTERS)

        def op(c):
            for d in deltas:
                c.execute(
                    f"INSERT INTO token_usage (task_id, ledger, subtask_id, module_id, call_kind,"
                    f" model_name, {counters}, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(task_id, ledger, subtask_id, module_id, call_kind, model_name)"
                    f" DO UPDATE SET {adds}, updated_at=excluded.updated_at",
                    (d.task_id, d.ledger, d.subtask_id or "", d.module_id, d.call_kind, d.model_name,
                     *(int(getattr(d, k)) for k in _USAGE_COUNTERS), at))

        self._write(op)

    def usage_buckets(self, task_id: str, *, ledger: str) -> list[UsageBucket]:
        def op(c):
            rows = c.execute("SELECT * FROM token_usage WHERE task_id=? AND ledger=?"
                             " ORDER BY subtask_id, module_id, call_kind, model_name",
                             (task_id, ledger)).fetchall()
            return [UsageBucket(task_id=r["task_id"], ledger=r["ledger"], module_id=r["module_id"],
                                call_kind=r["call_kind"], model_name=r["model_name"],
                                subtask_id=r["subtask_id"],
                                **{k: r[k] for k in _USAGE_COUNTERS}, updated_at=r["updated_at"])
                    for r in rows]
        return self._read(op)

    # -- adjudication -------------------------------------------------------------------------
    def append_adjudication(self, rows: list[AdjudicationCreate], *, at: int) -> list[Adjudication]:
        def op(c):
            out = []
            for r in rows:
                cur = c.execute(
                    "INSERT INTO adjudication (owner_id, task_id, episode_index, line, decision,"
                    " new_label, note, decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r.owner_id, r.task_id, int(r.episode_index), r.line, r.decision, r.new_label,
                     r.note, r.decided_by, at))
                out.append(_adjudication(c.execute("SELECT * FROM adjudication WHERE id=?",
                                                   (cur.lastrowid,)).fetchone()))
            return out
        return self._write(op)

    def latest_adjudications(self, task_id: str, *, line: str | None = None,
                             unapplied_only: bool = False) -> list[Adjudication]:
        inner, args = "SELECT MAX(id) FROM adjudication WHERE task_id=?", [task_id]
        if line is not None:
            inner += " AND line=?"
            args.append(line)
        inner += " GROUP BY line, episode_index"
        sql = f"SELECT * FROM adjudication WHERE id IN ({inner})"
        if unapplied_only:
            sql += " AND applied_in_subtask IS NULL"
        sql += " ORDER BY episode_index, line"
        return self._read(lambda c: [_adjudication(r) for r in c.execute(sql, args).fetchall()])

    def mark_adjudications_applied(self, ids: list[int], subtask_id: str) -> int:
        if not ids:
            return 0
        return self._write(lambda c: c.execute(
            f"UPDATE adjudication SET applied_in_subtask=? WHERE id IN ({_placeholders(len(ids))})"
            " AND applied_in_subtask IS NULL", (subtask_id, *ids)).rowcount)

    # -- events (audit) ---------------------------------------------------------------------------
    def append_event(self, *, actor: str, action: str, resource: str, at: int,
                     detail: dict | None = None, owner: str = DEFAULT_OWNER) -> Event:
        def op(c):
            cur = c.execute("INSERT INTO event (owner_id, actor, action, resource, detail, at)"
                            " VALUES (?,?,?,?,?,?)", (owner, actor, action, resource,
                                                      _dumps(detail), at))
            return _event(c.execute("SELECT * FROM event WHERE id=?", (cur.lastrowid,)).fetchone())
        return self._write(op)

    def list_events(self, *, resource: str | None = None, cursor: str | None = None,
                    limit: int = 50, owner: str = DEFAULT_OWNER) -> CursorPage[Event]:
        """Newest first; the cursor is the id of the last row returned."""
        limit = int(limit)
        if limit < 1:
            raise ValueError("limit starts at 1")
        limit = min(limit, MAX_PAGE)
        scope = {"owner": owner, "resource": resource}
        where, args = ["owner_id=?"], [owner]
        if resource is not None:
            where.append("resource=?")
            args.append(resource)
        if cursor:
            last_id = decode_cursor(cursor, "events", scope=scope)
            if not isinstance(last_id, int):
                raise ValueError("events cursor must hold an id")
            where.append("id < ?")
            args.append(last_id)

        def op(c):
            rows = c.execute(f"SELECT * FROM event WHERE {' AND '.join(where)}"
                             " ORDER BY id DESC LIMIT ?", (*args, limit + 1)).fetchall()
            items = [_event(r) for r in rows[:limit]]
            more = len(rows) > limit
            return CursorPage(items=items, has_more=more,
                              next_cursor=encode_cursor("events", items[-1].id, scope=scope)
                              if more else None)

        return self._read(op)

    def purge_events(self, *, before: int) -> int:
        return self._write(lambda c: c.execute("DELETE FROM event WHERE at < ?", (before,)).rowcount)

    # -- preflight cache --------------------------------------------------------------------------
    def put_preflight(self, *, request_hash: str, result: dict, at: int,
                      owner: str = DEFAULT_OWNER) -> str:
        preflight_id = new_id("pf")
        self._write(lambda c: c.execute(
            "INSERT INTO preflight_cache (id, owner_id, request_hash, result, created_at)"
            " VALUES (?,?,?,?,?)", (preflight_id, owner, request_hash, _dumps(result), at)))
        return preflight_id

    def get_preflight(self, preflight_id: str, *, max_age_ms: int, now: int,
                      owner: str = DEFAULT_OWNER) -> dict | None:
        def op(c):
            row = c.execute("SELECT result, created_at FROM preflight_cache WHERE id=? AND owner_id=?",
                            (preflight_id, owner)).fetchone()
            if row is None or now - row["created_at"] > max_age_ms:
                return None
            return _loads(row["result"])
        return self._read(op)

    # -- idempotency keys -------------------------------------------------------------------------
    def get_idempotent(self, *, key: str, route: str,
                       owner: str = DEFAULT_OWNER) -> IdempotencyRecord | None:
        def op(c):
            row = c.execute("SELECT * FROM idempotency_key WHERE owner_id=? AND route=? AND key=?",
                            (owner, route, key)).fetchone()
            if row is None:
                return None
            return IdempotencyRecord(key=row["key"], route=row["route"],
                                     response=_loads(row["response"]),
                                     created_at=row["created_at"], owner_id=row["owner_id"])
        return self._read(op)

    def put_idempotent(self, record: IdempotencyRecord) -> None:
        self._write(lambda c: c.execute(
            "INSERT OR REPLACE INTO idempotency_key (key, owner_id, route, response, created_at)"
            " VALUES (?,?,?,?,?)", (record.key, record.owner_id, record.route,
                                    _dumps(record.response), record.created_at)))

    def purge_expired(self, *, now: int) -> int:
        def op(c):
            n = c.execute("DELETE FROM preflight_cache WHERE created_at < ?",
                          (now - PREFLIGHT_TTL_MS,)).rowcount
            n += c.execute("DELETE FROM idempotency_key WHERE created_at < ?",
                           (now - IDEMPOTENCY_TTL_MS,)).rowcount
            return n
        return self._write(op)
