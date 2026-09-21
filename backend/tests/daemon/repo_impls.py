"""Repository implementations the conformance suite runs against.

Each factory takes ``(tmp_path, clock)`` and returns a fresh, empty repository
whose ``created_at`` / ``updated_at`` come from ``clock()`` (epoch ms). A future
RDS implementation adds itself here - or, without touching this file, through
``CURATOR_EXTRA_REPO_FACTORIES="package.module:factory,..."`` - and must pass
``test_repo_conformance.py`` unchanged.
"""
from __future__ import annotations

import importlib
import os
from typing import Callable

from daemon.repo.sqlite import SqliteRepository


def sqlite_factory(tmp_path, clock: Callable[[], int]):
    return SqliteRepository(tmp_path / "curator.db", clock=clock)


def factories() -> dict[str, Callable]:
    out: dict[str, Callable] = {"sqlite": sqlite_factory}
    for spec in filter(None, os.environ.get("CURATOR_EXTRA_REPO_FACTORIES", "").split(",")):
        module, _, name = spec.strip().partition(":")
        out[spec.strip()] = getattr(importlib.import_module(module), name)
    return out
