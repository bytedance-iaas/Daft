"""Fixtures of the visualizer tests: the Daemon test app (from tests/daemon) and two local datasets."""
from __future__ import annotations

import pytest

from ..daemon.conftest import FakeClock, assert_error, assert_schema, clean_env, client_for, clock, make_app  # noqa: F401
from .fixtures import make_v2, make_v3


@pytest.fixture(scope="session")
def data_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("viz-data")
    make_v2(str(root / "lerobot_v2"))
    make_v3(str(root / "lerobot_v3"))
    return root
