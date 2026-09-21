"""Every failure becomes the one C4 Error body with the status the OpenAPI declares."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from daemon import errors
from daemon.errors import STATUS, ApiError
from daemon.pagination import CursorError
from daemon.repo import protocol as P

from .conftest import assert_error, assert_schema

RAISERS = {
    "not-found": (P.NotFound("task_x"), "not_found", 404),
    "state": (P.StateConflict("running"), "task_state_conflict", 409),
    "subtask": (P.Conflict("subtask_active"), "subtask_active", 409),
    "cred": (P.Conflict("credential_in_use"), "credential_in_use", 409),
    "backend": (P.Conflict("backend_in_use"), "backend_in_use", 409),
    "name": (P.Conflict("name_taken"), "name_taken", 409),
    "unknown-conflict": (P.Conflict("something_new"), "task_state_conflict", 409),
    "precondition": (P.PreconditionFailed("stale"), "precondition_failed", 412),
    "cursor": (CursorError("bad"), "validation_failed", 400),
    "api": (ApiError("precheck_failed", "输出目录写不进去", details={"checks": ["output"]}),
            "precheck_failed", 422),
    "crash": (RuntimeError("password=hunter2"), "internal", 500),
}


@pytest.fixture
def client():
    app = FastAPI()
    errors.install(app)

    @app.get("/raise/{name}")
    def raise_(name: str):
        raise RAISERS[name][0]

    @app.get("/typed")
    def typed(n: int = Query(..., ge=1)):
        return {"n": n}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("name", sorted(RAISERS))
def test_exceptions_map_to_codes(client, name):
    _, code, status = RAISERS[name]
    body = assert_error(client.get(f"/raise/{name}"), code, status)
    assert body["error"]["message"]                           # always something to show
    assert "hunter2" not in str(body)


def test_request_validation_is_validation_failed(client):
    body = assert_error(client.get("/typed", params={"n": "zero"}), "validation_failed")
    assert body["error"]["message"].startswith("请求参数不合法：查询参数 n")
    assert body["error"]["details"]["errors"][0]["field"] == "query.n"
    assert_error(client.get("/typed"), "validation_failed")


def test_every_code_has_a_status_and_wording():
    from curation.contracts import schemas

    enum = schemas.load("openapi.yaml")["components"]["schemas"]["Error"]["properties"]["error"][
        "properties"]["code"]["enum"]
    assert set(STATUS) == set(enum) == set(errors.DEFAULT_MESSAGE)
    for code in enum:
        assert_schema("Error", errors.error_body(code))
    with pytest.raises(ValueError):
        ApiError("teapot")


def test_schema_errors_read_like_sentences():
    from curation.contracts import schemas

    errs = list(schemas.validator("openapi.yaml#/components/schemas/TaskPatch").iter_errors(
        {"nmae": "x", "note": 5}))
    err = errors.validation_error(errs)
    assert err.code == "validation_failed" and "另有 1 处问题" in err.message
    assert {e["field"] for e in err.details["errors"]} == {"请求体", "note"}
