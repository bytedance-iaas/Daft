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
    # W8 secrets and resources (daemon/secrets, routes/access_keys.py, vlm.py, media.py)
    "listCredentials": ("GET", "/api/v1/credentials"),
    "createCredential": ("POST", "/api/v1/credentials"),
    "updateCredential": ("PUT", "/api/v1/credentials/{id}"),
    "deleteCredential": ("DELETE", "/api/v1/credentials/{id}"),
    "verifyCredential": ("POST", "/api/v1/credentials/{id}/verify"),
    "listVlmBackends": ("GET", "/api/v1/vlm-backends"),
    "createVlmBackend": ("POST", "/api/v1/vlm-backends"),
    "updateVlmBackend": ("PUT", "/api/v1/vlm-backends/{id}"),
    "deleteVlmBackend": ("DELETE", "/api/v1/vlm-backends/{id}"),
    "verifyVlmBackend": ("POST", "/api/v1/vlm-backends/{id}/verify"),
    "refreshVlmModels": ("POST", "/api/v1/vlm-backends/{id}/refresh-models"),
    "addVlmModel": ("POST", "/api/v1/vlm-backends/{id}/models"),
    "updateVlmModel": ("PATCH", "/api/v1/vlm-backends/{id}/models/{model_id}"),
    "deleteVlmModel": ("DELETE", "/api/v1/vlm-backends/{id}/models/{model_id}"),
    "probeDelivery": ("POST", "/api/v1/deliveries/probe"),
    "signMedia": ("GET", "/api/v1/media/sign"),
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
    "listDatasets": ("GET", "/api/v1/datasets"),
    "getDataset": ("GET", "/api/v1/datasets/{id}"),
    "updateDataset": ("PATCH", "/api/v1/datasets/{id}"),
    "deleteDataset": ("DELETE", "/api/v1/datasets/{id}"),
    "getOverview": ("GET", "/api/v1/overview"),
}

#: operationId -> (owner, what it still needs)
PENDING: dict[str, tuple[str, str]] = {
    # W3 CLI-backed (run through W5's executor)
    "preflight": ("W3", "curation preflight --json; cache with repo.put_preflight"),
    "browseDatasets": ("W3", "curation datasets list (TOS listing / public catalog), needs W8 keys"),
    "listDatasetEpisodes": ("W3", "episodes.jsonl metadata + W8 presigned camera URLs"),
    # W5 orchestration: queue, subprocesses, prechecks, work directory and result revisions
    "createTask": ("W5", "prechecks (D30), start, planner (W6); reuse daemon.taskspec.resolve_config"),
    "createTasksBatch": ("W5", "one task per dataset with the shared configuration"),
    "taskAction": ("W5", "start/pause/resume/stop through daemon.transitions + the worker pool"),
    "retryTask": ("W5", "retry subtask (D25, D35)"),
    "continueTask": ("W5", "resume subtask; its end recomputes the parent (C5 1.2 edges)"),
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
    # contract 1.1 (D36, D37): registering and checking datasets runs the CLI
    "createDataset": ("W5", "curation preflight + snapshot through the executor; register_dataset"),
    "recheckDataset": ("W5", "curation snapshot, compare with the kept listing"),
    "repreflightDataset": ("W5", "curation preflight + snapshot, refresh the registration"),
    "repreflightTask": ("W5", "D37: re-preflight, compatibility check, then start"),
}


def contract_operations() -> dict[str, tuple[str, str]]:
    """operationId -> (METHOD, path relative to the mount prefix) for every C4 operation."""
    from curation.contracts import schemas

    out: dict[str, tuple[str, str]] = {}
    for path, item in schemas.load("openapi.yaml")["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                prefix = "" if path in ("/healthz", "/readyz") or path.startswith("/events/") \
                    else "/api/v1"
                out[op["operationId"]] = (method.upper(), prefix + path)
    return out
