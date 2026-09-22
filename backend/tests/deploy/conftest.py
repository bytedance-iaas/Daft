"""Fixtures for the W11 tests (helpers live in :mod:`.support`)."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def daemon_env_cleared(monkeypatch):
    """No CURATOR_* / CURATION_* / TOS_ENDPOINT from the developer's shell leaks into a test."""
    for key in list(os.environ):
        if key.startswith(("CURATOR_", "CURATION_")) or key == "TOS_ENDPOINT":
            monkeypatch.delenv(key, raising=False)
    return monkeypatch
