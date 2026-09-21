"""The operations table: implemented + pending = every C4 operation, and the router agrees."""
from __future__ import annotations

import re

import pytest

from curation.contracts import schemas
from daemon.operations import IMPLEMENTED, PENDING

METHODS = ("get", "post", "put", "patch", "delete")
OWNERS = {"W3", "W5", "W6", "W7", "W8", "W10"}
UNKNOWN = "这个接口不存在（或还没有实现）"      # the catch-all's words: no handler matched


def _contract_ops() -> dict[str, tuple[str, str]]:
    spec = schemas.load("openapi.yaml")
    out = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                prefix = "" if path in ("/healthz", "/readyz") or path.startswith("/events/") \
                    else "/api/v1"
                out[op["operationId"]] = (method.upper(), prefix + path)
    return out


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_implemented_and_pending_cover_the_contract_exactly():
    contract = _contract_ops()
    assert not set(IMPLEMENTED) & set(PENDING)
    assert set(IMPLEMENTED) | set(PENDING) == set(contract)
    for op, (method, path) in IMPLEMENTED.items():
        assert (method, _norm(path)) == (contract[op][0], _norm(contract[op][1])), op
    assert {owner for owner, _ in PENDING.values()} <= OWNERS


def _call(client, op, base="/curation"):
    method, path = _contract_ops()[op]
    some_id = "ds_x" if path.startswith("/api/v1/datasets/") else "task_x"
    url = base + path.replace("{id}", some_id).replace("{model_id}", "vm_x") \
        .replace("{action}", "start").replace("{index}", "3").replace("{table}", "t")
    return client.request(method, url, json={} if method in ("POST", "PUT", "PATCH") else None)


@pytest.mark.parametrize("base", ["", "/curation"])
def test_every_implemented_operation_reaches_its_handler(client_for, base):
    c = client_for(base_path=base)
    for op in IMPLEMENTED:
        r = _call(c, op, base)
        if r.status_code == 404:        # the task does not exist - but a handler said so
            assert r.json()["error"]["message"] != UNKNOWN, op
        else:
            assert r.status_code in (200, 400), (op, r.status_code, r.text)


@pytest.mark.parametrize("op", sorted(PENDING))
def test_pending_operations_are_not_registered(client_for, op):
    r = _call(client_for(base_path="/curation"), op)
    assert r.status_code == 404, (op, r.status_code, r.text)
    assert r.json()["error"] == {"code": "not_found", "message": UNKNOWN}
