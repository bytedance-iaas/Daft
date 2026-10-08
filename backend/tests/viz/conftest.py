"""Fixtures of the visualizer tests: the Daemon test app (from tests/daemon) and the local datasets
(LeRobot v2 / v3, and the v3 one converted to the three Lance layouts, design doc 19 §4)."""
from __future__ import annotations

import pytest

from ..daemon.conftest import FakeClock, assert_error, assert_schema, clean_env, client_for, clock, make_app  # noqa: F401
from .fixtures import make_v2, make_v3, make_v3_depth
from .lance_fixtures import make_lance, make_lance_depth


@pytest.fixture(scope="session")
def data_root(tmp_path_factory):
    import shutil

    root = tmp_path_factory.mktemp("viz-data")
    make_v2(str(root / "lerobot_v2"))
    make_v3(str(root / "lerobot_v3"))
    make_v3_depth(str(root / "lerobot_v3_depth"))
    make_lance_depth(str(root / "lance_depth"))
    for layout in ("0.3", "0.2-video", "0.2-frames"):
        make_lance(str(root / f"lance_{layout.replace('.', '').replace('-', '_')}"), layout)
    # a 0.3 root that holds only the tables: meta/ comes from meta.lance
    shutil.copytree(root / "lance_03", root / "lance_03_tables")
    shutil.rmtree(root / "lance_03_tables" / "meta")
    return root
