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
    # W5b result readers (daemon/results, routes/results.py, routes/adjudication.py)
    "getReport": ("GET", "/api/v1/tasks/{id}/report"),
    "getReportTable": ("GET", "/api/v1/tasks/{id}/report/tables/{table}"),
    "getEpisode": ("GET", "/api/v1/tasks/{id}/episodes/{index}"),
    "listPipelineEpisodes": ("GET", "/api/v1/tasks/{id}/pipeline/episodes"),
    "getPipelineEpisode": ("GET", "/api/v1/tasks/{id}/pipeline/episodes/{index}"),
    "getPerf": ("GET", "/api/v1/tasks/{id}/perf"),
    "listAdjudication": ("GET", "/api/v1/tasks/{id}/adjudication"),
    "submitAdjudication": ("POST", "/api/v1/tasks/{id}/adjudication"),
    # W5a orchestration (daemon/exec, daemon/orchestr, routes/runs.py, routes/datasets_exec.py)
    "createTask": ("POST", "/api/v1/tasks"),
    "createTasksBatch": ("POST", "/api/v1/tasks/batch"),
    "taskAction": ("POST", "/api/v1/tasks/{id}/actions/{action}"),
    "repreflightTask": ("POST", "/api/v1/tasks/{id}/repreflight"),
    "retryTask": ("POST", "/api/v1/tasks/{id}/retry"),
    "continueTask": ("POST", "/api/v1/tasks/{id}/continue"),
    "reexportTask": ("POST", "/api/v1/tasks/{id}/reexport"),
    "applyAdjudication": ("POST", "/api/v1/tasks/{id}/adjudication/apply"),
    "purgeTaskArtifacts": ("POST", "/api/v1/tasks/{id}/purge-artifacts"),
    "getTaskPlan": ("GET", "/api/v1/tasks/{id}/plan"),
    "preflight": ("POST", "/api/v1/preflight"),
    # F5.5 input files of module parameters (routes/uploads.py, daemon/uploads.py)
    "createUpload": ("POST", "/api/v1/uploads"),
    "getUpload": ("GET", "/api/v1/uploads/{upload_id}"),
    "browseDatasets": ("GET", "/api/v1/datasets/browse"),
    "listDatasetEpisodes": ("GET", "/api/v1/datasets/episodes"),
    "createDataset": ("POST", "/api/v1/datasets"),
    "recheckDataset": ("POST", "/api/v1/datasets/{id}/recheck"),
    "repreflightDataset": ("POST", "/api/v1/datasets/{id}/repreflight"),
}

#: operationId -> (owner, what it still needs); empty since W5a and W5b landed
PENDING: dict[str, tuple[str, str]] = {}


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
