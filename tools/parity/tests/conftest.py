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
def v1_src(tmp_path_factory) -> str:
    """v1 exactly as at the freeze commit, taken from git (the working tree moved on)."""
    from parity.manifest import DEFAULT_COMMIT, extract_v1

    repo = subprocess.run(["git", "-C", TOOLS, "rev-parse", "--show-toplevel"], check=True,
                          capture_output=True, text=True).stdout.strip()
    return extract_v1(repo, DEFAULT_COMMIT, str(tmp_path_factory.mktemp("v1src")))


@pytest.fixture(scope="session")
def mini_dataset(tmp_path_factory) -> str:
    from parity.fixtures import make_mini_lerobot

    return make_mini_lerobot(str(tmp_path_factory.mktemp("mini") / "mini"))


#: v1's command line against the fake model (the tape hooks answer it)
V1_RUN = ["run", "--vlm-endpoint", "http://fake-vlm.local/v1", "--vlm-model", "fake-vlm"]


@pytest.fixture(scope="session")
def v1_golden(tmp_path_factory, mini_dataset, v1_src) -> str:
    """``dump-v1 --fake-vlm`` of the synthetic dataset: the golden baseline the end-to-end
    tests compare with (recorded once per session, about half a minute)."""
    tmp = tmp_path_factory.mktemp("golden")
    out = str(tmp / "rec1")
    proc = run_parity("dump-v1", "--out", out, "--v1-src", v1_src, "--fake-vlm", "--",
                      *V1_RUN, "--input", mini_dataset, "--output", str(tmp / "rec1-delivery"))
    assert proc.returncode == 0, proc.stderr[-4000:]
    return out
