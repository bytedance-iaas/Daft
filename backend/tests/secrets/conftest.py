"""Fixtures for the W8 tests: W4's app fixtures (imported, not copied) plus the two fakes."""
from __future__ import annotations

import pytest

from ..daemon.conftest import (  # noqa: F401 - fixtures are registered by importing them
    T0,
    FakeClock,
    assert_error,
    assert_schema,
    clean_env,
    client_for,
    clock,
    make_app,
    make_settings,
    seed_task,
)
from .fakes import AK, AK2, SK, SK2, FakeTos, StubServer, VlmStub

#: Every write says it is JSON, even without a body (routes/common.py).
JSON = {"Content-Type": "application/json"}
API = "/curation/api/v1"


@pytest.fixture
def fake_tos() -> FakeTos:
    """Two key pairs: the input one reads ``datasets``; the output one writes ``deliveries``."""
    tos = FakeTos()
    tos.add_key(AK, SK, read={"datasets"}, write=set())
    tos.add_key(AK2, SK2, read={"deliveries"}, write={"deliveries"})
    tos.put("datasets", "droid_100/meta/info.json", b'{"codebase_version": "v2.1"}')
    tos.put("public-mirror", "lerobot/pusht/meta/info.json", b'{"codebase_version": "v2.1"}')
    return tos


@pytest.fixture
def vlm_stub():
    with StubServer(VlmStub()) as stub:
        yield stub


@pytest.fixture
def secret_client(client_for, fake_tos):
    """``secret_client(**settings)`` -> a started TestClient under ``/curation`` whose
    secrets service talks to the fake TOS (and to real HTTP for the VLM stub)."""
    from daemon.secrets import service_of
    from daemon.secrets.vlm import VlmConnector

    def build(**kw):
        kw.setdefault("base_path", "/curation")
        c = client_for(**kw)
        svc = service_of(c.app.state.runtime)
        svc.tos_factory = fake_tos.factory
        svc.vlm = VlmConnector(list_timeout_s=3, call_timeout_s=3)
        return c

    return build


def runtime(client):
    return client.app.state.runtime


def service(client):
    from daemon.secrets import service_of

    return service_of(runtime(client))


def add_access_key(client, name="prod-tos", ak=AK, sk=SK, **extra):
    body = {"name": name, "access_key_id": ak, "secret_access_key": sk, "region": "cn-beijing",
            **extra}
    r = client.post(f"{API}/credentials", json=body, headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()


def add_backend(client, stub: VlmStub, name="ark-prod", kind="ark", api_key=None, **extra):
    body = {"name": name, "kind": kind, "endpoint": stub.url, **extra}
    if api_key is not None:
        body["api_key"] = api_key
    r = client.post(f"{API}/vlm-backends", json=body, headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()
