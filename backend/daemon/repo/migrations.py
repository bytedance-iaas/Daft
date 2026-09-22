"""SQLite schema migrations (design doc 01, sections 2 and 4.1).

Every Daemon start runs the missing steps in order; the version lives in
``PRAGMA user_version``. Steps are append-only: never edit a released step,
add a new one. Each step runs in one transaction together with the version bump,
so a crash leaves the database at the previous version, never half migrated.

Deviations from the sketch in design doc 01, all additive:

* ``vlm_backend`` carries verification columns and every table has
  ``created_at`` / ``updated_at`` (the C5 dataclasses have them).
* ``subtask`` has ``result_rev`` and ``created_at`` (C5 ``Subtask``).
* ``task.vlm_model_id`` is ``ON DELETE SET NULL``: deleting a backend keeps the
  finished tasks that used it, which then get rebound like a deleted access key.
* ``event.id`` is ``AUTOINCREMENT`` so ids are never reused after the 90-day
  purge; the SSE epoch is the id of the ``daemon.start`` event.

Step 2 (C5 1.1 / 1.2) adds the dataset registry of design doc 01, section 2.8,
``task.dataset_id``, ``subtask.pause_reason`` and one table of our own:

* ``dataset.format`` is derived from the stored preflight on every write (the
  ``fmt`` filter of ``list_datasets``); ``created_at`` / ``updated_at`` as everywhere.
* The address is unique through an expression index over ``COALESCE(region, '')``:
  SQLite never treats two NULL regions as equal, so a plain ``UNIQUE`` would not do.
* ``subtask.pause_reason`` is filled from the audit events for subtasks that were
  pausing or paused when the step ran (step 1 kept the reason only there); one
  whose events are gone counts as paused by the user, so it is not resumed behind
  anybody's back (step 1 kept such subtasks paused too).
* ``token_timeline`` sums actual-ledger tokens per owner and 15-minute UTC slot;
  ``token_usage`` has no time axis, and the overview charts tokens per day
  (see ``daemon.repo.extras``).

Step 3 (C4 1.6) adds ``vlm_model.is_default``: the model a new task starts with.
At most one per owner across every backend, which no index can express (the owner
sits on the backend), so ``set_default_vlm_model`` clears and sets in one statement
pair inside one transaction; the partial index is for finding it.
"""
from __future__ import annotations

TASK_STATES = ("'created','queued','running','pausing','paused','stopping','stopped',"
               "'succeeded','completed_with_errors','failed'")
SUBTASK_STATES = ("'queued','running','pausing','paused','stopping','stopped',"
                  "'succeeded','completed_with_errors','failed'")
MODULE_STATES = ("'pending','running','succeeded','completed_with_errors','failed',"
                 "'skipped','stale'")

_V1 = f"""
CREATE TABLE credential (
  id               TEXT PRIMARY KEY,
  owner_id         TEXT NOT NULL DEFAULT 'default',
  name             TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('tos','ark','custom_vlm')),
  payload_enc      BLOB NOT NULL,
  key_version      INTEGER NOT NULL DEFAULT 1,
  payload_meta     TEXT NOT NULL,
  verify_state     TEXT NOT NULL DEFAULT 'unverified'
                   CHECK (verify_state IN ('unverified','ok','failed')),
  last_verified_at INTEGER,
  last_verify_error TEXT,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  UNIQUE (owner_id, name)
);
CREATE INDEX idx_credential_key_version ON credential(key_version, id);

CREATE TABLE vlm_backend (
  id               TEXT PRIMARY KEY,
  owner_id         TEXT NOT NULL DEFAULT 'default',
  name             TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('ark','custom')),
  credential_id    TEXT REFERENCES credential(id),
  endpoint         TEXT NOT NULL,
  max_concurrency  INTEGER NOT NULL DEFAULT 64,
  verify_state     TEXT NOT NULL DEFAULT 'unverified'
                   CHECK (verify_state IN ('unverified','ok','failed')),
  last_verified_at INTEGER,
  last_verify_error TEXT,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  UNIQUE (owner_id, name)
);
CREATE INDEX idx_vlm_backend_credential ON vlm_backend(credential_id);

CREATE TABLE vlm_model (
  id               TEXT PRIMARY KEY,
  backend_id       TEXT NOT NULL REFERENCES vlm_backend(id) ON DELETE CASCADE,
  model_name       TEXT NOT NULL,
  reasoning_effort TEXT,
  max_concurrency  INTEGER,
  capabilities     TEXT NOT NULL DEFAULT '{{}}',
  source           TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('listed','manual')),
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  UNIQUE (backend_id, model_name)
);

CREATE TABLE task (
  id                   TEXT PRIMARY KEY,
  owner_id             TEXT NOT NULL DEFAULT 'default',
  name                 TEXT NOT NULL,
  note                 TEXT,
  state                TEXT NOT NULL CHECK (state IN ({TASK_STATES})),
  state_reason         TEXT,
  pause_reason         TEXT CHECK (pause_reason IN ('user','system')),
  input_source         TEXT NOT NULL CHECK (input_source IN ('tos','public','local')),
  input_uri            TEXT NOT NULL,
  input_region         TEXT,
  input_cred_id        TEXT REFERENCES credential(id) ON DELETE SET NULL,
  output_uri           TEXT NOT NULL,
  output_region        TEXT,
  output_cred_id       TEXT REFERENCES credential(id) ON DELETE SET NULL,
  delivery_key         TEXT NOT NULL,
  episode_selector     TEXT NOT NULL,
  embodiment_id        TEXT,
  vlm_model_id         TEXT REFERENCES vlm_model(id) ON DELETE SET NULL,
  vlm_reasoning_effort TEXT,
  vlm_snapshot         TEXT,
  params               TEXT NOT NULL,
  preflight            TEXT,
  source_fingerprint   TEXT,
  result_rev           INTEGER NOT NULL DEFAULT 0,
  export_fingerprint   TEXT,
  run_id               TEXT,
  progress             TEXT,
  summary              TEXT,
  delivery_stale       INTEGER NOT NULL DEFAULT 0,
  started_at           INTEGER,
  finished_at          INTEGER,
  deleted_at           INTEGER,
  created_at           INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL
);
CREATE INDEX idx_task_list ON task(owner_id, created_at DESC, id DESC);
CREATE INDEX idx_task_state ON task(state);
CREATE INDEX idx_task_delivery ON task(owner_id, delivery_key);
CREATE INDEX idx_task_input_cred ON task(input_cred_id);
CREATE INDEX idx_task_output_cred ON task(output_cred_id);
CREATE INDEX idx_task_vlm_model ON task(vlm_model_id);
CREATE INDEX idx_task_deleted ON task(deleted_at);

CREATE TABLE task_module (
  task_id            TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  module_id          TEXT NOT NULL,
  selected           INTEGER NOT NULL,
  availability       TEXT NOT NULL CHECK (availability IN ('available','needs_input','unsupported')),
  unavailable_reason TEXT,
  state              TEXT NOT NULL CHECK (state IN ({MODULE_STATES})),
  error              TEXT,
  input_digest       TEXT,
  episodes_total     INTEGER NOT NULL DEFAULT 0,
  episodes_error     INTEGER NOT NULL DEFAULT 0,
  params             TEXT,
  started_at         INTEGER,
  finished_at        INTEGER,
  PRIMARY KEY (task_id, module_id)
);

CREATE TABLE subtask (
  id           TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL CHECK (kind IN ('retry','resume','apply_adjudication','reexport')),
  scope        TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ({SUBTASK_STATES})),
  state_reason TEXT,
  progress     TEXT,
  result_rev   INTEGER,
  created_at   INTEGER NOT NULL,
  started_at   INTEGER,
  finished_at  INTEGER
);
CREATE INDEX idx_subtask_task ON subtask(task_id, created_at, id);

CREATE TABLE token_usage (
  task_id                TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  ledger                 TEXT NOT NULL CHECK (ledger IN ('actual','attributed')),
  subtask_id             TEXT NOT NULL DEFAULT '',
  module_id              TEXT NOT NULL,
  call_kind              TEXT NOT NULL,
  model_name             TEXT NOT NULL,
  prompt_tokens          INTEGER NOT NULL DEFAULT 0,
  completion_tokens      INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens       INTEGER NOT NULL DEFAULT 0,
  cached_tokens          INTEGER NOT NULL DEFAULT 0,
  requests               INTEGER NOT NULL DEFAULT 0,
  requests_unknown_usage INTEGER NOT NULL DEFAULT 0,
  updated_at             INTEGER NOT NULL,
  PRIMARY KEY (task_id, ledger, subtask_id, module_id, call_kind, model_name)
);

CREATE TABLE adjudication (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id           TEXT NOT NULL DEFAULT 'default',
  task_id            TEXT NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  episode_index      INTEGER NOT NULL,
  line               TEXT NOT NULL CHECK (line IN ('label','task_verdict','reject_appeal')),
  decision           TEXT NOT NULL,
  new_label          TEXT,
  note               TEXT,
  decided_by         TEXT NOT NULL,
  decided_at         INTEGER NOT NULL,
  applied_in_subtask TEXT REFERENCES subtask(id) ON DELETE SET NULL
);
CREATE INDEX idx_adj_lookup ON adjudication(task_id, line, episode_index, id);

CREATE TABLE event (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id TEXT NOT NULL,
  actor    TEXT NOT NULL,
  action   TEXT NOT NULL,
  resource TEXT NOT NULL,
  detail   TEXT,
  at       INTEGER NOT NULL
);
CREATE INDEX idx_event_resource ON event(owner_id, resource, id);
CREATE INDEX idx_event_at ON event(at);

CREATE TABLE preflight_cache (
  id           TEXT PRIMARY KEY,
  owner_id     TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result       TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
CREATE INDEX idx_preflight_created ON preflight_cache(created_at);

CREATE TABLE idempotency_key (
  key        TEXT NOT NULL,
  owner_id   TEXT NOT NULL,
  route      TEXT NOT NULL,
  response   TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (owner_id, route, key)
);
CREATE INDEX idx_idempotency_created ON idempotency_key(created_at);
"""

_V2 = """
CREATE TABLE dataset (
  id                 TEXT PRIMARY KEY,
  owner_id           TEXT NOT NULL DEFAULT 'default',
  name               TEXT NOT NULL,
  note               TEXT,
  source             TEXT NOT NULL CHECK (source IN ('tos','public','local')),
  uri                TEXT NOT NULL,
  region             TEXT,
  credential_id      TEXT REFERENCES credential(id) ON DELETE SET NULL,
  preflight          TEXT NOT NULL,
  format             TEXT NOT NULL CHECK (format IN ('lerobot_v2','lerobot_v3','unsupported')),
  meta_fingerprint   TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  manifest_path      TEXT,
  check_state        TEXT NOT NULL DEFAULT 'ok' CHECK (check_state IN ('ok','changed')),
  checked_at         INTEGER,
  preflighted_at     INTEGER NOT NULL,
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_dataset_address ON dataset(owner_id, source, uri, COALESCE(region, ''));
CREATE INDEX idx_dataset_list ON dataset(owner_id, created_at DESC, id DESC);
CREATE INDEX idx_dataset_credential ON dataset(credential_id);

CREATE TABLE dataset_check (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id TEXT NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  at         INTEGER NOT NULL,
  "trigger"  TEXT NOT NULL CHECK ("trigger" IN ('add','recheck','task_start','repreflight')),
  result     TEXT NOT NULL CHECK (result IN ('same','changed')),
  change     TEXT
);
CREATE INDEX idx_dataset_check ON dataset_check(dataset_id, at, id);

ALTER TABLE task ADD COLUMN dataset_id TEXT REFERENCES dataset(id) ON DELETE SET NULL;
CREATE INDEX idx_task_dataset ON task(dataset_id);
CREATE INDEX idx_task_finished ON task(owner_id, finished_at);

ALTER TABLE subtask ADD COLUMN pause_reason TEXT CHECK (pause_reason IN ('user','system'));
CREATE INDEX idx_subtask_state ON subtask(state);
UPDATE subtask SET pause_reason = COALESCE((
  SELECT json_extract(e.detail, '$.pause_reason') FROM event e
  WHERE e.resource = subtask.task_id AND e.action = 'subtask.state'
    AND json_extract(e.detail, '$.subtask_id') = subtask.id
    AND json_extract(e.detail, '$.to') IN ('pausing','paused')
    AND json_extract(e.detail, '$.pause_reason') IN ('user','system')
  ORDER BY e.id DESC LIMIT 1), 'user')
WHERE state IN ('pausing','paused');

CREATE TABLE token_timeline (
  owner_id TEXT NOT NULL,
  slot     INTEGER NOT NULL,
  tokens   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (owner_id, slot)
);
CREATE INDEX idx_token_timeline_slot ON token_timeline(slot);
"""

_V3 = """
ALTER TABLE vlm_model ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_vlm_model_default ON vlm_model(is_default) WHERE is_default = 1;
"""

#: (version, script). Append only.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _V1),
    (2, _V2),
    (3, _V3),
)

LATEST_VERSION = MIGRATIONS[-1][0]


def migrate(conn) -> list[int]:
    """Apply missing steps on an autocommit connection; returns the versions applied."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > LATEST_VERSION:
        raise RuntimeError(f"database schema version {current} is newer than this Daemon "
                           f"({LATEST_VERSION}); refusing to run an older build against it")
    applied = []
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
