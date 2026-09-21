"""Source datasets for the export tests, built once per session outside the repo."""
from __future__ import annotations

import pytest

from parity.fixtures import make_mini_lerobot

from .v3_fixture import make_mini_lerobot_v3


@pytest.fixture(scope="session")
def v2_source(tmp_path_factory) -> str:
    """W0's 8-episode LeRobot v2.1 dataset (H.264, two cameras)."""
    return make_mini_lerobot(str(tmp_path_factory.mktemp("v2-source") / "dataset"))


@pytest.fixture(scope="session")
def v3_source(tmp_path_factory) -> str:
    """The 6-episode LeRobot v3.0 dataset of ``v3_fixture``."""
    return make_mini_lerobot_v3(str(tmp_path_factory.mktemp("v3-source") / "dataset"))
