"""Fixtures for the v2 CLI tests (W3).

``cli(...)`` runs ``curation.cli.main`` in-process and checks the output
discipline on every call: with ``--json`` stdout is exactly one JSON document
(the error envelope when the exit code is non-zero) and every stderr line is a
C3 event (``docs/contracts/progress.schema.json``).
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field

import pytest

from curation.contracts import schemas

from .fakes import FakeCloud

#: Every variable that could leak a developer's real setup into the tests.
ENV_VARS = ("CURATION_INPUT_TOS_ACCESS_KEY", "CURATION_INPUT_TOS_SECRET_KEY",
            "CURATION_INPUT_TOS_SESSION_TOKEN", "CURATION_OUTPUT_TOS_ACCESS_KEY",
            "CURATION_OUTPUT_TOS_SECRET_KEY", "CURATION_OUTPUT_TOS_SESSION_TOKEN",
            "TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_SESSION_TOKEN", "TOS_ENDPOINT", "TOS_REGION",
            "CURATOR_URL", "CURATOR_USER", "CURATOR_PASSWORD", "CURATION_CONFIG",
            "CURATION_SEMANTICS_IGNORE_PROFILES")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    from curation.ingest import public_catalog

    public_catalog.reset()
    yield
    public_catalog.reset()


@pytest.fixture(scope="session")
def mini_dataset(tmp_path_factory) -> str:
    """tools/parity's 8-episode LeRobot v2.1 dataset (franka, two cameras, H.264)."""
    from parity.fixtures import make_mini_lerobot

    return make_mini_lerobot(str(tmp_path_factory.mktemp("mini") / "mini"))


@pytest.fixture(scope="session")
def vlm_stage(tmp_path_factory, mini_dataset) -> dict:
    """A run directory ready for the VLM stage, and that stage's records when it runs.

    ``base`` went through preflight, plan, snapshot, autolabel and the numeric and
    frame stages against the fake model; ``reference`` holds the task_success
    records of ``check --modules task_success`` run with default options on a
    copy of it; ``episodes`` is the frame stage's survivors (``@file``). Tests copy
    ``base`` before changing anything.
    """
    from .fakevlm_server import FakeVlmServer
    from .pipeline import Chain, results, run

    tmp = tmp_path_factory.mktemp("vlm-stage")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV_VARS:
            mp.delenv(name, raising=False)
        with FakeVlmServer() as vlm:
            c = Chain(mini_dataset, str(tmp / "base"), vlm.url)
            c.front()
            c.before_vlm()
            ref = str(tmp / "reference")
            shutil.copytree(c.rd, ref)
            episodes = "@" + c.path("stages", "frame.txt")
            before = len(vlm.calls)
            res = run("check", "--modules", "task_success", "--input", mini_dataset,
                      "--run-dir", ref, "--episodes", episodes, *c.vlm)
            assert res.rc == 0, res.doc
            calls = len([x for x in vlm.calls[before:]
                         if x["path"].endswith("/chat/completions")])
    return {"base": c.rd, "reference": results(ref, "task_success"), "reference_dir": ref,
            "reference_posts": calls, "episodes": episodes, "dataset": mini_dataset,
            "tmp": tmp}


@pytest.fixture
def dataset(mini_dataset, tmp_path) -> str:
    """A private, writable copy of the mini dataset."""
    dst = tmp_path / "mini"
    shutil.copytree(mini_dataset, dst)
    return str(dst)


def edit_info(root: str, **changes) -> None:
    path = os.path.join(root, "meta", "info.json")
    with open(path, encoding="utf-8") as fh:
        info = json.load(fh)
    for key, value in changes.items():
        if value is None:
            info.pop(key, None)
        else:
            info[key] = value
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)


@dataclass
class CliRun:
    rc: int
    out: str
    err: str
    doc: object = None
    events: list = field(default_factory=list)


@pytest.fixture
def cli(capsys):
    from curation.cli import main

    def run(*argv: str, json_mode: bool = True) -> CliRun:
        args = [str(a) for a in argv]
        if json_mode and "--json" not in args:
            args.append("--json")
        capsys.readouterr()
        rc = main(args)
        out, err = capsys.readouterr()
        res = CliRun(rc, out, err)
        if json_mode:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            assert len(lines) == 1, f"stdout must be one JSON document, got {out!r}"
            res.doc = json.loads(lines[0])
            for line in err.splitlines():
                event = json.loads(line)
                problems = schemas.errors("progress.schema.json", event)
                assert not problems, f"stderr line breaks C3: {line} -> {problems}"
                res.events.append(event)
            if rc != 0:
                problems = schemas.errors("cli/error.schema.json", res.doc)
                assert not problems, f"bad error envelope {res.doc}: {problems}"
                assert res.doc["exit_code"] == rc
        return res

    return run


@pytest.fixture
def cloud(monkeypatch) -> FakeCloud:
    """An in-memory TOS behind ``curation.cli.creds.CLIENT_FACTORY``."""
    from curation.cli import creds

    fake = FakeCloud()
    monkeypatch.setattr(creds, "CLIENT_FACTORY", fake.factory)
    return fake
