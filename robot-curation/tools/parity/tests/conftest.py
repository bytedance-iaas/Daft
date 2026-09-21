"""Shared helpers for the parity tool tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

TOOLS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROBOT_CURATION = os.path.dirname(TOOLS)
CONTRACTS = os.path.join(ROBOT_CURATION, "docs", "contracts")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: runs the v1 pipeline end to end (about a minute)")


def run_parity(*args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """``python -m parity <args>`` in a fresh process (v1 keeps module state)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = TOOLS + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "parity", *args], cwd=ROBOT_CURATION,
                          env=env, capture_output=True, text=True, timeout=timeout)


def load_schema(*parts: str) -> dict:
    with open(os.path.join(CONTRACTS, *parts), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def mini_dataset(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_lerobot

    return make_mini_lerobot(str(tmp_path_factory.mktemp("mini") / "mini"))
