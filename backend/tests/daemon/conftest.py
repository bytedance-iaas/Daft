"""Fixtures for the W4 tests: a fake clock, repositories, a configured app."""
from __future__ import annotations

import base64
import threading

import pytest

from .repo_impls import factories

#: 2025-09-19T16:40:00Z, the example timestamps of design doc 03.
T0 = 1_758_300_000_000


class FakeClock:
    """Epoch milliseconds that only move when a test says so."""

    def __init__(self, start: int = T0):
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self.now

    def advance(self, ms: int) -> int:
        with self._lock:
            self.now += ms
            return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=sorted(factories()))
def repo(request, tmp_path, clock):
    """Every Repository implementation, fresh and empty (the conformance suite runs on each)."""
    r = factories()[request.param](tmp_path, clock)
    yield r
    close = getattr(r, "close", None)
    if close is not None:
        close()


@pytest.fixture
def master_key_b64() -> str:
    return base64.b64encode(bytes(range(32))).decode()


_CLEAN_ENV = (
    "CURATOR_BASE_PATH", "CURATION_UI_ROOT_PATH", "CURATOR_DATA_DIR", "CURATOR_DB_PATH",
    "CURATOR_WORK_DIR", "CURATOR_SCRATCH_DIR", "CURATOR_STATIC_DIR", "CURATOR_PUBLIC_BASE_URL",
    "CURATOR_MASTER_KEY", "CURATOR_MASTER_KEY_NEXT", "CURATOR_MASTER_KEY_VERSION",
    "CURATOR_AUTH_MODE", "CURATOR_HTPASSWD_FILE", "CURATOR_AUTH_USER", "CURATOR_AUTH_PASSWORD",
    "CURATION_UI_HTPASSWD_FILE", "CURATION_UI_USER", "CURATION_UI_PASSWORD",
    "CURATOR_PUBLIC_DATASETS_BUCKET", "CURATOR_PUBLIC_DATASETS_PREFIX", "CURATOR_LOCAL_DATA_ROOT",
    "CURATOR_SSE_HEARTBEAT_S", "CURATOR_HOST", "CURATOR_PORT", "CURATOR_LOG_LEVEL",
    "CURATOR_LOG_FORMAT",
)


@pytest.fixture
def clean_env(monkeypatch):
    """The Daemon reads its configuration from the environment; start every test from none."""
    for key in _CLEAN_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def assert_schema(ref: str, instance) -> None:
    """Validate against C4 (a bare name means ``openapi.yaml#/components/schemas/<name>``)."""
    from curation.contracts import schemas

    if "#" not in ref and "/" not in ref:
        ref = f"openapi.yaml#/components/schemas/{ref}"
    problems = schemas.errors(ref, instance)
    assert not problems, f"{ref}: {problems}"


def assert_error(response, code: str, status: int | None = None) -> dict:
    from daemon.errors import STATUS

    assert response.status_code == (status or STATUS[code]), response.text
    body = response.json()
    assert_schema("Error", body)
    assert body["error"]["code"] == code, body
    return body


def make_settings(tmp_path, *, base_path="", **overrides):
    from daemon.masterkey import MasterKey
    from daemon.settings import Settings

    values = dict(master_key=MasterKey(bytes(range(32))), base_path=base_path,
                  data_dir=tmp_path / "data", scratch_dir=tmp_path / "scratch",
                  static_dir=None, sse_heartbeat_s=0.2)
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def make_app(tmp_path, clean_env, clock):
    """``make_app(base_path=..., **settings)`` -> a FastAPI app on a fresh database.

    Each app gets its own data volume (one Daemon per volume, see ``daemon.instance``).
    """
    from daemon.app import create_app

    count = [0]

    def build(*, clock_fn=None, **kw):
        count[0] += 1
        return create_app(make_settings(tmp_path / f"app{count[0]}", **kw),
                          clock=clock_fn or clock)

    return build


@pytest.fixture
def client_for(make_app):
    """``client_for(**settings)`` -> a started TestClient (lifespan runs); closed after the test."""
    from fastapi.testclient import TestClient

    opened = []

    def build(**kw):
        app = make_app(**kw)
        c = TestClient(app, raise_server_exceptions=False)
        c.__enter__()
        opened.append(c)
        return c

    yield build
    for c in opened:
        c.__exit__(None, None, None)


def seed_task(repo, name="droid 前 50 条质检", *, state="queued", selected=("timestamp_check",),
              owner="default", input_cred_id=None, output_cred_id=None, vlm_model_id=None,
              delivery="tos://deliveries/droid-50", input_uri="tos://bucket/datasets/droid_100",
              dataset_id=None):
    """A task through the repository only (what W5's create path will do)."""
    from curation.contracts import modules as registry
    from daemon.repo import protocol as P

    rows = [P.TaskModule(task_id="", module_id=m, selected=m in selected, availability="available")
            for m in registry.ids()]
    return repo.create_task(P.TaskCreate(
        name=name, input_source="tos", input_uri=input_uri, output_uri=delivery,
        delivery_key=delivery, episode_selector={"mode": "head", "n": 50},
        params={"export": True, "vlm_retry": 3}, modules=rows, state=state, owner_id=owner,
        input_cred_id=input_cred_id, output_cred_id=output_cred_id, vlm_model_id=vlm_model_id,
        input_region="cn-beijing", output_region="cn-beijing", dataset_id=dataset_id))


META_DIGEST = "sha256:" + "a" * 64
LISTING_DIGEST = "sha256:" + "b" * 64


def sample_preflight(*, version="v2", supported=True, episodes=200, robot_type="franka",
                     modules=None) -> dict:
    """A C2 ``preflight`` result (valid against ``cli/preflight.schema.json``)."""
    return {
        "schema_version": "1.0",
        "format": {"kind": "lerobot", "version": version, "supported": supported,
                   "detail": f"LeRobot {version}, {episodes} episodes"},
        "validation": [] if supported else ["meta/info.json: codebase_version missing"],
        "dataset": {"episode_count": episodes, "cameras": ["wrist", "exterior_1"], "fps": 15.0,
                    "robot_type": robot_type, "total_frames": episodes * 250,
                    "labels": {"with_task": episodes, "without_task": 0}, "profile": None},
        "modules": modules if modules is not None else [
            {"id": "timestamp_check", "availability": "available"}],
        "meta_fingerprint": META_DIGEST,
        "warnings": [],
    }


def seed_dataset(repo, uri="tos://bucket/datasets/droid_100", *, name=None, source="tos",
                 region="cn-beijing", credential_id=None, owner="default", preflighted_at=T0,
                 **preflight):
    """A registration through the repository only (what W5's POST /datasets will do)."""
    from daemon.repo import protocol as P

    ds, _ = repo.register_dataset(P.Dataset(
        id="", name=name or uri.rstrip("/").rsplit("/", 1)[-1], source=source, uri=uri,
        region=region, credential_id=credential_id, owner_id=owner,
        preflight=sample_preflight(**preflight), meta_fingerprint=META_DIGEST,
        source_fingerprint={"objects": 204, "bytes": 1_234_567, "digest": LISTING_DIGEST},
        preflighted_at=preflighted_at))
    return ds
