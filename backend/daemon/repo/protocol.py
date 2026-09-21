"""C5 - the Repository interface: every read and write of Daemon state goes through it.

Business code never writes SQL (design doc 01, section 4). The SQLite
implementation (W4) and a future RDS one implement this protocol; nothing else
may assume which one is behind it.

Rules every implementation keeps:

* **State changes are compare-and-set.** ``update_task_state(frm={...}, to=...)``
  succeeds only if the current state is in ``frm``; it never reads-then-writes.
  Worker threads and request threads race even in one process.
* **Transactions are explicit.** Callers open ``transaction()``; methods do not
  start hidden transactions of their own.
* **Every query is scoped by owner.** The owner is ``"default"`` in this release
  (single tenant, D3) but the code path is real, so IAM can be added without
  touching the callers.
* **Two paginations.** The task and dataset lists use page numbers with a ``total`` (D21);
  "scroll down" content (logs, adjudication queue, episode lists) uses opaque
  cursors built from the last row's sort key.
* Times are integer epoch milliseconds. Secrets are stored encrypted by the
  caller (``payload_enc``); the repository never sees plaintext keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ContextManager, Generic, Iterable, Literal, Protocol, TypeVar

T = TypeVar("T")

DEFAULT_OWNER = "default"

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

TaskState = Literal["created", "queued", "running", "pausing", "paused", "stopping", "stopped",
                    "succeeded", "completed_with_errors", "failed"]
TERMINAL_STATES: frozenset[str] = frozenset({"stopped", "succeeded", "completed_with_errors",
                                             "failed"})
PauseReason = Literal["user", "system"]
ModuleRunState = Literal["pending", "running", "succeeded", "completed_with_errors", "failed",
                         "skipped", "stale"]
Availability = Literal["available", "needs_input", "unsupported"]
SubtaskKind = Literal["retry", "resume", "apply_adjudication", "reexport"]
CredentialKind = Literal["tos", "ark", "custom_vlm"]
VerifyState = Literal["unverified", "ok", "failed"]
BackendKind = Literal["ark", "custom"]
ModelSource = Literal["listed", "manual"]
Ledger = Literal["actual", "attributed"]
#: v1's call kinds are probe, endstate, arbitration, caption and llm; ``merged`` marks a request that
#: carried several modules. New modules send under kinds of their own (C3 1.1), so this is open.
CallKind = str
DatasetCheckState = Literal["ok", "changed"]
DatasetCheckTrigger = Literal["add", "recheck", "task_start", "repreflight"]
AdjudicationLine = Literal["label", "task_verdict", "reject_appeal"]
InputSource = Literal["tos", "public", "local"]

#: Legal task transitions (design doc 01, section 3.1). Anything else is a 409.
#: ``succeeded`` <-> ``completed_with_errors`` happens when a subtask finishes and
#: the terminal state is recomputed from the current results (D25).
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"queued"}),
    "queued": frozenset({"running", "stopped"}),
    "running": frozenset({"pausing", "stopping", "succeeded", "completed_with_errors", "failed"}),
    "pausing": frozenset({"paused", "stopping"}),
    "paused": frozenset({"queued", "stopping"}),
    "stopping": frozenset({"stopped"}),
    # a resume subtask that finishes re-derives the task's terminal state (01 §2.5); if it
    # fails or is stopped the task keeps its state
    "stopped": frozenset({"succeeded", "completed_with_errors"}),
    "succeeded": frozenset({"completed_with_errors"}),
    "completed_with_errors": frozenset({"succeeded"}),
    "failed": frozenset({"succeeded", "completed_with_errors"}),
}

#: Subtasks share the machine but never have ``created`` and are never resumed themselves,
#: so ``stopped`` and ``failed`` are final for them (design doc 01, section 3).
SUBTASK_TRANSITIONS: dict[str, frozenset[str]] = {
    k: (frozenset() if k in ("stopped", "failed") else v)
    for k, v in TASK_TRANSITIONS.items() if k != "created"}

#: Parent states a subtask kind may start from (design doc 01, section 2.5).
SUBTASK_PARENT_STATES: dict[str, frozenset[str]] = {
    "retry": frozenset({"completed_with_errors"}),
    "resume": frozenset({"stopped", "failed"}),
    "apply_adjudication": frozenset({"succeeded", "completed_with_errors"}),
    "reexport": frozenset({"succeeded", "completed_with_errors"}),
}


def can_transition(frm: str, to: str, *, subtask: bool = False) -> bool:
    table = SUBTASK_TRANSITIONS if subtask else TASK_TRANSITIONS
    return to in table.get(frm, frozenset())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RepositoryError(Exception):
    """Base class; the API maps subclasses to the error codes of the OpenAPI contract."""


class NotFound(RepositoryError):
    """-> 404 not_found"""


class StateConflict(RepositoryError):
    """A CAS on a state failed -> 409 task_state_conflict (the caller refetches)."""


class Conflict(RepositoryError):
    """A uniqueness or reference rule -> 409 (name_taken, subtask_active, credential_in_use...)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class PreconditionFailed(RepositoryError):
    """``If-Match`` did not match ``updated_at`` -> 412 precondition_failed."""


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PagedResult(Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True)
class CursorPage(Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool


# ---------------------------------------------------------------------------
# Rows (one dataclass per table of design doc 01, section 2)
# ---------------------------------------------------------------------------

@dataclass
class Credential:
    id: str
    name: str
    kind: CredentialKind
    payload_enc: bytes
    key_version: int
    payload_meta: dict
    verify_state: VerifyState = "unverified"
    last_verified_at: int | None = None
    last_verify_error: str | None = None
    owner_id: str = DEFAULT_OWNER
    created_at: int = 0
    updated_at: int = 0


@dataclass
class VlmModel:
    id: str
    backend_id: str
    model_name: str
    reasoning_effort: str | None = None      # native value; None = do not send the field
    max_concurrency: int | None = None       # None = the backend's
    capabilities: dict = field(default_factory=dict)
    source: ModelSource = "manual"
    created_at: int = 0
    updated_at: int = 0


@dataclass
class VlmBackend:
    id: str
    name: str
    kind: BackendKind
    endpoint: str
    credential_id: str | None
    max_concurrency: int = 64
    verify_state: VerifyState = "unverified"
    last_verified_at: int | None = None
    last_verify_error: str | None = None
    models: list[VlmModel] = field(default_factory=list)
    owner_id: str = DEFAULT_OWNER
    created_at: int = 0
    updated_at: int = 0



@dataclass
class Dataset:
    """A registered dataset (D36): where it lives, its last preflight and both fingerprints.
    Registration is a record, not a copy; the data stays on TOS."""

    id: str
    name: str
    source: InputSource
    uri: str
    preflight: dict                          # the last preflight result (C2 preflight.schema.json)
    meta_fingerprint: str
    source_fingerprint: dict                 # listing summary: objects, bytes, digest
    preflighted_at: int
    note: str | None = None
    region: str | None = None
    credential_id: str | None = None
    manifest_path: str | None = None         # the kept file listing, to tell which files changed
    check_state: DatasetCheckState = "ok"
    checked_at: int | None = None
    owner_id: str = DEFAULT_OWNER
    created_at: int = 0
    updated_at: int = 0


@dataclass
class DatasetCheck:
    """One fingerprint comparison (D37); the dataset page shows them as its change history."""

    dataset_id: str
    at: int
    trigger: DatasetCheckTrigger
    result: Literal["same", "changed"]
    change: dict | None = None               # C4 SourceChange when result == "changed"
    id: int | None = None


@dataclass
class Task:
    id: str
    name: str
    state: TaskState
    input_source: InputSource
    input_uri: str
    output_uri: str
    delivery_key: str                        # normalized delivery directory; publishing is serial per key
    episode_selector: dict
    params: dict
    note: str | None = None
    state_reason: str | None = None
    pause_reason: PauseReason | None = None
    input_region: str | None = None
    input_cred_id: str | None = None
    dataset_id: str | None = None            # the registered dataset (D36)
    output_region: str | None = None
    output_cred_id: str | None = None
    embodiment_id: str | None = None
    vlm_model_id: str | None = None
    vlm_reasoning_effort: str | None = None
    vlm_snapshot: dict | None = None         # effective VLM settings frozen at start (P17)
    preflight: dict | None = None            # frozen at start
    source_fingerprint: dict | None = None   # source manifest summary, frozen at start (D27)
    result_rev: int = 0                      # switched by CAS once a revision is complete (D25)
    export_fingerprint: str | None = None
    run_id: str | None = None
    progress: dict | None = None
    summary: dict | None = None
    delivery_stale: bool = False             # computed: export fingerprint != current one
    started_at: int | None = None
    finished_at: int | None = None
    deleted_at: int | None = None            # soft delete, purged after 30 days
    owner_id: str = DEFAULT_OWNER
    created_at: int = 0
    updated_at: int = 0


@dataclass
class TaskCreate:
    name: str
    input_source: InputSource
    input_uri: str
    output_uri: str
    delivery_key: str
    episode_selector: dict
    params: dict
    modules: list["TaskModule"]
    state: Literal["created", "queued"] = "queued"
    note: str | None = None
    input_region: str | None = None
    input_cred_id: str | None = None
    dataset_id: str | None = None
    output_region: str | None = None
    output_cred_id: str | None = None
    embodiment_id: str | None = None
    vlm_model_id: str | None = None
    vlm_reasoning_effort: str | None = None
    owner_id: str = DEFAULT_OWNER


@dataclass
class TaskModule:
    task_id: str
    module_id: str
    selected: bool
    availability: Availability
    state: ModuleRunState = "pending"
    unavailable_reason: str | None = None
    error: str | None = None
    input_digest: str | None = None          # the episode set this result was computed on
    episodes_total: int = 0
    episodes_error: int = 0
    params: dict | None = None
    started_at: int | None = None
    finished_at: int | None = None


@dataclass
class Subtask:
    id: str
    task_id: str
    kind: SubtaskKind
    scope: dict
    state: TaskState
    state_reason: str | None = None
    pause_reason: PauseReason | None = None  # only while pausing/paused, like the task's
    progress: dict | None = None
    result_rev: int | None = None            # the result revision this subtask committed, if any
    created_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None


@dataclass
class UsageDelta:
    """Tokens to add to one aggregate bucket (an UPSERT that accumulates)."""

    task_id: str
    ledger: Ledger
    module_id: str
    call_kind: CallKind
    model_name: str
    subtask_id: str = ""                     # "" = main run
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    requests: int = 0
    requests_unknown_usage: int = 0


@dataclass
class UsageBucket(UsageDelta):
    updated_at: int = 0


@dataclass
class Adjudication:
    id: int
    task_id: str
    episode_index: int
    line: AdjudicationLine
    decision: str
    decided_by: str
    decided_at: int
    new_label: str | None = None
    note: str | None = None
    applied_in_subtask: str | None = None    # None = not executed yet
    owner_id: str = DEFAULT_OWNER


@dataclass
class AdjudicationCreate:
    task_id: str
    episode_index: int
    line: AdjudicationLine
    decision: str
    decided_by: str
    new_label: str | None = None
    note: str | None = None
    owner_id: str = DEFAULT_OWNER


@dataclass
class Event:
    id: int
    actor: str
    action: str
    resource: str
    at: int
    detail: dict | None = None
    owner_id: str = DEFAULT_OWNER


@dataclass
class IdempotencyRecord:
    key: str
    route: str
    response: dict
    created_at: int
    owner_id: str = DEFAULT_OWNER


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

class Repository(Protocol):
    # -- transactions and schema -------------------------------------------------
    def transaction(self) -> ContextManager[None]:
        """Explicit transaction boundary; nested use joins the outer one."""

    def schema_version(self) -> int:
        """Current migration version (SQLite: ``PRAGMA user_version``)."""

    # -- credentials (TOS keys; VLM keys are created with their backend) -------
    def create_credential(self, cred: Credential) -> Credential:
        """Raises Conflict('name_taken')."""

    def get_credential(self, cred_id: str, *, owner: str = DEFAULT_OWNER) -> Credential:
        """Raises NotFound."""

    def get_credential_by_name(self, name: str, *, owner: str = DEFAULT_OWNER) -> Credential:
        """Raises NotFound."""

    def list_credentials(self, *, owner: str = DEFAULT_OWNER,
                         kind: CredentialKind | None = None) -> list[Credential]: ...

    def update_credential(self, cred_id: str, *, owner: str = DEFAULT_OWNER, name: str | None = None,
                          payload_enc: bytes | None = None, key_version: int | None = None,
                          payload_meta: dict | None = None) -> Credential: ...

    def set_credential_verification(self, cred_id: str, state: VerifyState, at: int,
                                    error: str | None) -> None: ...

    def credential_references(self, cred_id: str) -> tuple[int, int]:
        """(tasks not in a terminal state, terminal tasks) that reference it."""

    def delete_credential(self, cred_id: str, *, owner: str = DEFAULT_OWNER) -> None:
        """Raises Conflict('credential_in_use') while a non-terminal task references it;
        terminal tasks get their reference set to NULL."""

    def credentials_below_key_version(self, version: int, *, limit: int = 100) -> list[Credential]:
        """For master-key rotation, resumable (design doc 08, section 2.1)."""

    # -- VLM backends and models ------------------------------------------------
    def create_vlm_backend(self, backend: VlmBackend, credential: Credential | None) -> VlmBackend:
        """Creates the backend and its API-key credential together (one user action)."""

    def get_vlm_backend(self, backend_id: str, *, owner: str = DEFAULT_OWNER) -> VlmBackend: ...

    def get_vlm_backend_by_name(self, name: str, *, owner: str = DEFAULT_OWNER) -> VlmBackend: ...

    def list_vlm_backends(self, *, owner: str = DEFAULT_OWNER) -> list[VlmBackend]:
        """Backends with their models."""

    def update_vlm_backend(self, backend_id: str, *, owner: str = DEFAULT_OWNER,
                           **fields) -> VlmBackend: ...

    def set_vlm_backend_verification(self, backend_id: str, state: VerifyState, at: int,
                                     error: str | None) -> None: ...

    def delete_vlm_backend(self, backend_id: str, *, owner: str = DEFAULT_OWNER) -> None:
        """Raises Conflict('backend_in_use'); cascades to its models and key."""

    def upsert_vlm_model(self, model: VlmModel) -> VlmModel:
        """Unique per (backend, model_name)."""

    def update_vlm_model(self, model_id: str, **fields) -> VlmModel: ...

    def delete_vlm_model(self, model_id: str) -> None: ...

    # -- datasets (D36, D37) ----------------------------------------------------------
    def register_dataset(self, dataset: Dataset) -> tuple[Dataset, bool]:
        """Get-or-create by (owner, source, uri, region); returns (dataset, created)."""

    def get_dataset(self, dataset_id: str, *, owner: str = DEFAULT_OWNER) -> Dataset:
        """Raises NotFound."""

    def list_datasets(self, *, owner: str = DEFAULT_OWNER, page: int, page_size: int,
                      q: str | None = None, fmt: str | None = None,
                      check_state: DatasetCheckState | None = None) -> PagedResult[Dataset]:
        """Newest first; ``fmt`` is lerobot_v2 | lerobot_v3 | unsupported, read from the preflight."""

    def update_dataset(self, dataset_id: str, *, owner: str = DEFAULT_OWNER, **fields) -> Dataset:
        """Name and note (PATCH), or a refreshed preflight with both fingerprints (repreflight)."""

    def record_dataset_check(self, check: DatasetCheck) -> DatasetCheck:
        """Appends the check and sets the dataset's check_state and checked_at in one step."""

    def list_dataset_checks(self, dataset_id: str, *, limit: int = 20) -> list[DatasetCheck]:
        """Newest first."""

    def delete_dataset(self, dataset_id: str, *, owner: str = DEFAULT_OWNER) -> None:
        """Raises Conflict('dataset_in_use') while an unfinished task uses it; tasks keep their
        own copy of the input, so finished ones only lose the link (dataset_id set to NULL)."""

    # -- tasks --------------------------------------------------------------------
    def create_task(self, spec: TaskCreate) -> Task:
        """Creates the task and its task_module rows in one transaction."""

    def get_task(self, task_id: str, *, owner: str = DEFAULT_OWNER,
                 include_deleted: bool = False) -> Task:
        """Raises NotFound."""

    def list_tasks(self, *, owner: str = DEFAULT_OWNER, page: int, page_size: int,
                   state: str | None = None, q: str | None = None,
                   delivery_key: str | None = None, dataset_id: str | None = None,
                   modules: list[str] | None = None) -> PagedResult[Task]:
        """Newest first. ``state='deleted'`` lists soft-deleted tasks; ``modules`` keeps tasks that
        selected every one of them."""

    def update_task_fields(self, task_id: str, *, if_updated_at: int | None,
                           owner: str = DEFAULT_OWNER, **fields) -> Task:
        """Config edits (PATCH). Raises PreconditionFailed when ``if_updated_at`` is stale.
        Which fields are editable in which state is the caller's rule (D20)."""

    def update_task_state(self, task_id: str, frm: set[str] | frozenset[str], to: TaskState, *,
                          reason: str | None = None,
                          pause_reason: PauseReason | None = None, at: int) -> bool:
        """CAS; returns whether it applied. Never called with an illegal transition."""

    def set_task_progress(self, task_id: str, progress: dict) -> None: ...

    def set_task_summary(self, task_id: str, summary: dict) -> None: ...

    def switch_result_rev(self, task_id: str, expected: int, new: int) -> bool:
        """CAS on result_rev: readers see either the old or the new revision (D25)."""

    def set_export_fingerprint(self, task_id: str, fingerprint: str | None,
                               delivery_stale: bool) -> None: ...

    def freeze_task_inputs(self, task_id: str, *, run_id: str, preflight: dict,
                           source_fingerprint: dict, vlm_snapshot: dict | None) -> None:
        """What start fixes for the task's lifetime (D27, P17)."""

    def rebind_task_credentials(self, task_id: str, *, input_cred_id: str | None,
                                output_cred_id: str | None) -> Task:
        """``None`` leaves that side unchanged."""

    def soft_delete_task(self, task_id: str, *, at: int) -> None:
        """Raises StateConflict unless created or terminal (D28), Conflict('subtask_active')
        while a subtask is not terminal."""

    def restore_task(self, task_id: str) -> Task: ...

    def purge_deleted_tasks(self, *, before: int) -> int:
        """Hard-delete tasks soft-deleted before ``before``, with their rows."""

    def tasks_in_states(self, states: Iterable[str]) -> list[Task]:
        """Startup reconciliation (design doc 01, section 3.2)."""

    def subtasks_in_states(self, states: Iterable[str]) -> list[Subtask]:
        """Startup reconciliation of subtasks, across all tasks."""

    # -- task modules -----------------------------------------------------------
    def get_task_modules(self, task_id: str) -> list[TaskModule]: ...

    def upsert_task_modules(self, task_id: str, rows: list[TaskModule]) -> None: ...

    def update_module_state(self, task_id: str, module_id: str, frm: set[str] | frozenset[str],
                            to: ModuleRunState, **fields) -> bool:
        """CAS on the module state; fields: error, input_digest, episodes_total..."""

    def mark_modules_stale(self, task_id: str, modules: list[str]) -> None: ...

    # -- subtasks -----------------------------------------------------------------
    def create_subtask(self, subtask: Subtask) -> Subtask:
        """Raises Conflict('subtask_active') while another subtask of the task is not terminal,
        StateConflict when the task's state does not allow this kind (SUBTASK_PARENT_STATES)."""

    def get_subtask(self, subtask_id: str) -> Subtask: ...

    def list_subtasks(self, task_id: str) -> list[Subtask]:
        """Oldest first."""

    def active_subtask(self, task_id: str) -> Subtask | None: ...

    def update_subtask_state(self, subtask_id: str, frm: set[str] | frozenset[str], to: TaskState,
                             *, reason: str | None = None, pause_reason: PauseReason | None = None,
                             at: int) -> bool:
        """CAS, like update_task_state; ``pause_reason`` is kept only while pausing/paused."""

    def set_subtask_result_rev(self, subtask_id: str, result_rev: int) -> None:
        """The revision the subtask committed; the timeline links to its report."""

    def set_subtask_progress(self, subtask_id: str, progress: dict) -> None: ...

    # -- token usage (two ledgers, never added together) ------------------------
    def add_usage(self, deltas: list[UsageDelta], *, at: int) -> None:
        """Accumulate into the buckets (called every 5 s and at each stage end)."""

    def usage_buckets(self, task_id: str, *, ledger: Ledger) -> list[UsageBucket]: ...

    # -- adjudication (append only, the latest row per task/line/episode wins) --
    def append_adjudication(self, rows: list[AdjudicationCreate], *, at: int) -> list[Adjudication]: ...

    def latest_adjudications(self, task_id: str, *, line: AdjudicationLine | None = None,
                             unapplied_only: bool = False) -> list[Adjudication]: ...

    def mark_adjudications_applied(self, ids: list[int], subtask_id: str) -> int: ...

    # -- events (audit, kept 90 days) --------------------------------------------
    def append_event(self, *, actor: str, action: str, resource: str, at: int,
                     detail: dict | None = None, owner: str = DEFAULT_OWNER) -> Event: ...

    def list_events(self, *, resource: str | None = None, cursor: str | None = None,
                    limit: int = 50, owner: str = DEFAULT_OWNER) -> CursorPage[Event]:
        """Newest first."""

    def purge_events(self, *, before: int) -> int: ...

    # -- preflight cache (30 minutes) -------------------------------------------
    def put_preflight(self, *, request_hash: str, result: dict, at: int,
                      owner: str = DEFAULT_OWNER) -> str:
        """Returns the preflight_id."""

    def get_preflight(self, preflight_id: str, *, max_age_ms: int, now: int,
                      owner: str = DEFAULT_OWNER) -> dict | None:
        """None when missing or expired (-> preflight_expired)."""

    # -- idempotency keys (24 hours) ------------------------------------------------
    def get_idempotent(self, *, key: str, route: str,
                       owner: str = DEFAULT_OWNER) -> IdempotencyRecord | None: ...

    def put_idempotent(self, record: IdempotencyRecord) -> None: ...

    def purge_expired(self, *, now: int) -> int:
        """Drop expired preflight results and idempotency keys."""
