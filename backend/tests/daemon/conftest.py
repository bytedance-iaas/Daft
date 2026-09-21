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
