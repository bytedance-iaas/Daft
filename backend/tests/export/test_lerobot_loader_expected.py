"""Guard for the CI loader jobs: the loader cases skip themselves when lerobot is missing or
reads the other format, so a job could pass by skipping everything. CI names the format its
lerobot must read in CURATOR_EXPECT_LOADER_READS; locally the variable is unset and this skips."""
from __future__ import annotations

import os

import pytest

from .lerobot_check import lerobot_info

EXPECT = os.environ.get("CURATOR_EXPECT_LOADER_READS", "").strip()


@pytest.mark.skipif(not EXPECT, reason="CURATOR_EXPECT_LOADER_READS is not set")
def test_the_expected_loader_is_present():
    info = lerobot_info()
    assert info is not None, "lerobot is not importable, every loader case would skip"
    assert info[1] == EXPECT, f"lerobot {info[0]} reads {info[1]}, this job expects {EXPECT}"
