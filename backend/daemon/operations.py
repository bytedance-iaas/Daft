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
    "signDataset": ("POST", "/api/v1/datasets/{id}/sign"),          # design doc 15 (D55)
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
    # F6.2 the report's Episode tab (results/episode_list.py, results/sync_curves.py)
    "listTaskEpisodes": ("GET", "/api/v1/tasks/{id}/episodes"),
    "getEpisodeSyncCurves": ("GET", "/api/v1/tasks/{id}/episodes/{index}/sync-curves"),
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
    # the route existed for mcap cameras before C4 declared it (2.4.0, design doc 18)
    "getTaskCameraVideo": ("GET", "/api/v1/tasks/{id}/episodes/{index}/cameras/{camera}.mp4"),
    # F13.2 the data visualizer: the presentation model, LeRobot reader (daemon/viz, routes/viz.py)
    "getDatasetViz": ("GET", "/api/v1/datasets/{id}/viz"),
    "listDatasetVizEpisodes": ("GET", "/api/v1/datasets/{id}/viz/episodes"),
    "getDatasetVizMeta": ("GET", "/api/v1/datasets/{id}/viz/meta"),
    "getDatasetEpisodeViz": ("GET", "/api/v1/datasets/{id}/episodes/{index}/viz"),
    "getDatasetEpisodeSeries": ("GET", "/api/v1/datasets/{id}/episodes/{index}/series"),
    "getDatasetCameraVideo": ("GET", "/api/v1/datasets/{id}/episodes/{index}/cameras/{camera}.mp4"),
    "putDatasetAnnotations": ("PUT", "/api/v1/datasets/{id}/annotations"),
    "getTaskViz": ("GET", "/api/v1/tasks/{id}/viz"),
    "getTaskEpisodeViz": ("GET", "/api/v1/tasks/{id}/episodes/{index}/viz"),
    "getTaskEpisodeSeries": ("GET", "/api/v1/tasks/{id}/episodes/{index}/series"),
}

_MCAP = "the mcap reader, mappings and templates (design doc 18 §6)"
#: operationId -> (owner, what it still needs): the data visualizer lands in F13.2 / F13.3
PENDING: dict[str, tuple[str, str]] = {
    "getDatasetCameraFrames": ("F13.3", "JPEG frame packs (design doc 18 §4.2)"),
    "getDatasetCameraFrameIndex": ("F13.3", "JPEG frame packs (design doc 18 §4.2)"),
    "getTaskCameraFrames": ("F13.3", "JPEG frame packs (design doc 18 §4.2)"),
    "getTaskCameraFrameIndex": ("F13.3", "JPEG frame packs (design doc 18 §4.2)"),
    "getDatasetMapping": ("F13.3", _MCAP),
    "putDatasetMapping": ("F13.3", _MCAP),
    "probeMcap": ("F13.3", _MCAP),
    "listVizTemplates": ("F13.3", _MCAP),
    "createVizTemplate": ("F13.3", _MCAP),
    "deleteVizTemplate": ("F13.3", _MCAP),
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
