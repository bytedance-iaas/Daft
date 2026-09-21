"""Which C4 operations the Daemon serves today, and who owns the rest.

``IMPLEMENTED`` operations are registered by :func:`daemon.app.create_app` and
their responses are validated against ``openapi.yaml`` in the tests. Everything
else is ``PENDING`` with its owning work package (design doc 11): those routes
are deliberately **not registered** (no half-done endpoints), so they answer 404.
``tests/daemon/test_operations.py`` asserts ``IMPLEMENTED | PENDING`` equals the
operations in ``openapi.yaml`` and that the router matches this table exactly.
"""
from __future__ import annotations

#: operationId -> (method, path relative to the mount prefix)
IMPLEMENTED: dict[str, tuple[str, str]] = {
    "healthz": ("GET", "/healthz"),
    "readyz": ("GET", "/readyz"),
    "getModules": ("GET", "/api/v1/modules"),
    "listTasks": ("GET", "/api/v1/tasks"),
    "getTask": ("GET", "/api/v1/tasks/{id}"),
    "updateTask": ("PATCH", "/api/v1/tasks/{id}"),
    "deleteTask": ("DELETE", "/api/v1/tasks/{id}"),
    "restoreTask": ("POST", "/api/v1/tasks/{id}/restore"),
    "rebindTaskCredentials": ("POST", "/api/v1/tasks/{id}/rebind-credentials"),
    "listSubtasks": ("GET", "/api/v1/tasks/{id}/subtasks"),
    "getTaskTimeline": ("GET", "/api/v1/tasks/{id}/timeline"),
    "getTaskLogs": ("GET", "/api/v1/tasks/{id}/logs"),
    "getTaskUsage": ("GET", "/api/v1/tasks/{id}/usage"),
    "taskEvents": ("GET", "/events/tasks/{id}"),
}

#: operationId -> (owner, what it still needs)
PENDING: dict[str, tuple[str, str]] = {
    # W8 secrets and resources: encryption with the master key, verification calls
    "listCredentials": ("W8", "credential routes as one unit (list + create/update/delete/verify)"),
    "createCredential": ("W8", "AES-GCM sealing with the master key; identity check (ListBuckets/HeadBucket)"),
    "updateCredential": ("W8", "re-sealing; empty secret = unchanged"),
    "deleteCredential": ("W8", "confirm=true for historical references (repo.credential_references)"),
    "verifyCredential": ("W8", "TOS identity check"),
    "listVlmBackends": ("W8", "backend routes as one unit"),
    "createVlmBackend": ("W8", "API key sealing; GET {endpoint}/models once"),
    "updateVlmBackend": ("W8", "API key re-sealing"),
    "deleteVlmBackend": ("W8", "confirm handling; repo.delete_vlm_backend"),
    "verifyVlmBackend": ("W8", "GET /models or a minimal chat call"),
    "refreshVlmModels": ("W8", "GET {endpoint}/models"),
    "addVlmModel": ("W8", "one minimal call before saving"),
    "updateVlmModel": ("W8", "reasoning-effort mapping table (08 §4.1)"),
    "deleteVlmModel": ("W8", "repo.delete_vlm_model"),
    "probeDelivery": ("W8", "real write + delete with a decrypted access key"),
    "signMedia": ("W8", "presigned TOS URLs on the public endpoint, prefix checks (08 §6)"),
    # W3 CLI-backed (run through W5's executor)
    "preflight": ("W3", "curation preflight --json; cache with repo.put_preflight"),
    "browseDatasets": ("W3", "curation datasets list (TOS listing / public catalog), needs W8 keys"),
    "listDatasetEpisodes": ("W3", "episodes.jsonl metadata + W8 presigned camera URLs"),
    # W5 orchestration: queue, subprocesses, prechecks, work directory and result revisions
    "createTask": ("W5", "prechecks (D30), start, planner (W6); reuse daemon.taskspec.resolve_config"),
    "createTasksBatch": ("W5", "one task per dataset with the shared configuration"),
    "taskAction": ("W5", "start/pause/resume/stop through daemon.transitions + the worker pool"),
    "retryTask": ("W5", "retry subtask (D25, D35)"),
    "continueTask": ("W5", "resume subtask; needs stopped/failed -> terminal edges (C5 gap)"),
    "reexportTask": ("W5", "reexport subtask (W7 incremental export)"),
    "purgeTaskArtifacts": ("W5", "delete <delivery>/<run_id>/ on TOS with confirm_path (D28), needs W8"),
    "getTaskPlan": ("W5", "plan.json written at start by W6's planner"),
    "getReport": ("W5", "committed result revisions (report.json) from the work dir / TOS"),
    "getReportTable": ("W5", "Parquet slices with revision-bound cursors (03 §6)"),
    "getEpisode": ("W5", "one episode across modules from the committed revision"),
    "getPerf": ("W5", "perf.json of the revision"),
    "listAdjudication": ("W5", "review.json of the revision + repo.latest_adjudications"),
    "submitAdjudication": ("W5", "append + CSV copy in the run directory (double write)"),
    "applyAdjudication": ("W5", "apply_adjudication subtask"),
    # contract 1.1 (D36, D37): registered datasets and the overview
    "listDatasets": ("W4", "registered datasets from the repository (C5 1.1)"),
    "getDataset": ("W4", "registration, recent checks and tasks from the repository"),
    "updateDataset": ("W4", "name and note"),
    "deleteDataset": ("W4", "dataset_in_use while an unfinished task uses it"),
    "getOverview": ("W4", "aggregates over tasks, datasets, credentials and backends"),
    "createDataset": ("W5", "curation preflight + snapshot through the executor"),
    "recheckDataset": ("W5", "curation snapshot, compare with the kept listing"),
    "repreflightDataset": ("W5", "curation preflight + snapshot, refresh the registration"),
    "repreflightTask": ("W5", "D37: re-preflight, compatibility check, then start"),
}
