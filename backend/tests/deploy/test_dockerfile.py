"""deploy/Dockerfile and its build context, checked without docker (design doc 09 §1).

No docker here, so these read the Dockerfile itself: the two stages and what each copies;
the lessons of the v1 image that must survive (bookworm pin, PyPI mirror default, public apt
mirror default, internal-only repositories behind NO_MIRROR, daft from pip only, curl +
tini, oniond); where the console and the contracts land and that the Daemon is told; the
entry point; and that .dockerignore lets through everything the COPY lines need while
keeping host artefacts out.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import posixpath
import re
import subprocess
import sys

import pytest

from curation.contracts import schemas
from daemon.settings import SECRET_ENVS, Settings

from .support import (CHART, DOCKERFILE, ENTRYPOINT, PYPROJECT, REPO, assignments,
                      chart_values, copies, daemon_strings, dockerfile, dockerignored, mini_toml,
                      pyproject, stage)

WEB, RUNTIME = 0, 1


def global_args() -> dict[str, str]:
    out: dict[str, str] = {}
    for instr in dockerfile():
        if instr.stage == -1 and instr.op == "ARG":
            out.update(assignments(instr))
    return out


def args_of(index: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for instr in stage(index):
        if instr.op == "ARG":
            out.update(assignments(instr))
    return out


def env_of(index: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for instr in stage(index):
        if instr.op == "ENV":
            out.update(assignments(instr))
    return out


def runs(index: int | None = None) -> list[str]:
    return [i.args for i in dockerfile() if i.op == "RUN" and (index is None or i.stage == index)]


def exec_form(op: str) -> list[str]:
    [instr] = [i for i in dockerfile() if i.op == op]
    return json.loads(instr.args)


# ---------------------------------------------------------------------------
# stages and base images
# ---------------------------------------------------------------------------

def test_two_stages_the_console_then_the_runtime():
    assert [i.args for i in dockerfile() if i.op == "FROM"] == ["${NODE_IMAGE} AS web",
                                                               "${BASE_IMAGE}"]


def test_both_bases_are_pinned_to_bookworm():
    base = global_args()
    # v1 lesson 1: a bare python:3.10-slim drifts to trixie, where oniond's repository is missing
    assert base["BASE_IMAGE"] == "python:3.10-slim-bookworm"
    assert re.fullmatch(r"node:20(\.\d+)*-bookworm-slim", base["NODE_IMAGE"])
    # the Python of the image is the lowest the package supports (and what CI tests with)
    assert pyproject()["project"]["requires-python"] == ">=3.10"
    engines = json.loads((REPO / "frontend" / "package.json").read_text(encoding="utf-8"))["engines"]
    assert engines["node"].startswith("^20."), "the Node 20 base no longer satisfies frontend engines"


def test_pypi_defaults_to_the_internal_mirror_and_apt_to_the_public_one():
    args = args_of(RUNTIME)
    # v1 lesson 2: official PyPI crawls in the Volcano build cluster
    assert args["PIP_INDEX_URL"].startswith("https://mirrors.ivolces.com/pypi/")
    assert env_of(RUNTIME)["PIP_INDEX_URL"] == "${PIP_INDEX_URL}"
    # v1 lesson 3: the apt layer is not gated, so its default must resolve outside Volcano
    assert args["APT_MIRROR"] == "https://mirrors.volces.com"


def test_internal_only_hosts_are_behind_the_no_mirror_gate():
    assert args_of(RUNTIME)["NO_MIRROR"] == ""
    gated = [r for r in runs() if "ivolces" in r]
    assert gated, "the oniond layer is gone"
    for run in gated:
        assert run.startswith('if [ "$NO_MIRROR" != "1" ]; then'), run
    assert any("onion-ai-data" in r for r in gated)


def test_curl_and_tini_are_installed_for_every_build():
    [apt] = [r for r in runs(RUNTIME) if "apt-get install" in r and "curl tini" in r]
    assert "NO_MIRROR" not in apt
    assert "${APT_MIRROR}" in apt


def test_daft_comes_from_pip_only():
    # v1 lesson 4: never built from source
    for run in runs():
        assert not re.search(r"git clone|maturin|cargo |rustup|pip install[^&]*git\+", run), run
    requirements = (REPO / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^daft==\d+\.\d+\.\d+\s", requirements, re.M), "daft must be pinned exactly"


# ---------------------------------------------------------------------------
# the console (stage 1)
# ---------------------------------------------------------------------------

def test_web_stage_installs_from_the_lock_file_and_builds():
    web = runs(WEB)
    assert any(re.search(r"\bnpm ci\b", r) for r in web)
    assert not any(re.search(r"\bnpm (install|i)\b", r) for r in web)
    assert any(r == "npm run build" for r in web)
    # the lock file is copied (and installed) before the sources: the layer caches
    [first, *_] = copies(WEB)
    assert first[1] == ["frontend/package.json", "frontend/package-lock.json"]
    assert (REPO / "frontend" / "package-lock.json").is_file()
    scripts = json.loads((REPO / "frontend" / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["build"].startswith("vite build")


def test_web_stage_sees_the_contracts_the_mocks_import():
    [workdir] = [i.args for i in stage(WEB) if i.op == "WORKDIR"]
    dest = {tuple(src): dst for _, src, dst in copies(WEB)}[("docs/contracts/",)]
    # src/mocks/world.ts imports ../../../docs/contracts/modules.json
    mocks = posixpath.join(workdir, "src", "mocks")
    assert posixpath.normpath(posixpath.join(mocks, "../../../docs/contracts")) + "/" == dest
    assert "../../../docs/contracts/modules.json" in (
        REPO / "frontend" / "src" / "mocks" / "world.ts").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the runtime (stage 2)
# ---------------------------------------------------------------------------

def test_console_lands_where_the_daemon_serves_it_from():
    [workdir] = [i.args for i in stage(WEB) if i.op == "WORKDIR"]
    [(flags, [src], dest)] = [c for c in copies(RUNTIME) if c[0].get("from") == "web"]
    out_dir = re.search(r"outDir:\s*'([^']+)'",
                        (REPO / "frontend" / "vite.config.ts").read_text(encoding="utf-8")).group(1)
    assert src.rstrip("/") == posixpath.join(workdir, out_dir)
    static = env_of(RUNTIME)["CURATOR_STATIC_DIR"]
    assert static == dest.rstrip("/") == "/app/web"      # also the Daemon's own default


def test_contracts_land_where_the_daemon_looks():
    dest = {tuple(src): dst for _, src, dst in copies(RUNTIME)}[("docs/contracts/",)].rstrip("/")
    assert env_of(RUNTIME)["CURATOR_CONTRACTS_DIR"] == dest
    # without the variable the loader walks up from the package: /app/curation/contracts -> /app
    package = {tuple(src): dst for _, src, dst in copies(RUNTIME)}[("backend/curation/",)]
    assert posixpath.join(posixpath.dirname(package.rstrip("/")), "docs", "contracts") == dest
    assert (pathlib.Path(schemas.__file__).parent.name, schemas.CONTRACTS_ENV) == (
        "contracts", "CURATOR_CONTRACTS_DIR")
    assert (REPO / "docs" / "contracts" / schemas.OPENAPI).is_file()


def test_backend_packages_are_installed_after_their_requirements():
    order = [(i.op, i.args) for i in stage(RUNTIME) if i.op in ("COPY", "RUN")]

    def index(op, needle):
        return next(n for n, (o, a) in enumerate(order) if o == op and needle in a)

    requirements = index("COPY", "backend/requirements.txt")
    installed = index("RUN", "pip install --no-cache-dir -r requirements.txt")
    sources = [index("COPY", p) for p in ("backend/pyproject.toml", "backend/curation/",
                                          "backend/daemon/")]
    package = index("RUN", "pip install --no-cache-dir --no-deps -e .")
    assert requirements < installed < min(sources) and max(sources) < package
    # both packages are found by the build (curation and daemon)
    assert pyproject()["tool"]["setuptools"]["packages"]["find"]["include"] == ["curation*",
                                                                                "daemon*"]


def test_default_command_is_the_daemon_under_tini():
    assert exec_form("ENTRYPOINT") == ["tini", "--", "curator-entrypoint"]
    assert exec_form("CMD") == ["curator-daemon"]
    target = {tuple(src): dst for _, src, dst in copies(RUNTIME)}[("deploy/docker-entrypoint.sh",)]
    assert target == "/usr/local/bin/curator-entrypoint"
    assert any(r == f"chmod 0755 {target}" for r in runs(RUNTIME))
    scripts = pyproject()["project"]["scripts"]
    assert {"curator-daemon", "curation", "curator-rotate-master-key"} <= set(scripts)


@pytest.mark.parametrize("argv,want", [
    ([], ["curator-daemon"]),
    (["curator-daemon"], ["curator-daemon"]),
    (["--check-config"], ["curator-daemon", "--check-config"]),
    (["--port", "9000"], ["curator-daemon", "--port", "9000"]),
    (["curation", "--help"], ["curation", "--help"]),
    (["curator-rotate-master-key", "--batch", "10"], ["curator-rotate-master-key", "--batch", "10"]),
])
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_entrypoint_routes_arguments(argv, want, tmp_path):
    for name in ("curator-daemon", "curation", "curator-rotate-master-key"):
        stub = tmp_path / name
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$(basename "$0")" "$@"\n')
        stub.chmod(0o755)
    proc = subprocess.run(["sh", str(ENTRYPOINT), *argv], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin"}, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == want


def test_entrypoint_execs_so_signals_reach_the_program():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert re.search(r'^exec "\$@"$', text, re.M)
    assert os.access(ENTRYPOINT, os.X_OK)


def test_runs_as_a_non_root_user_that_owns_the_volumes():
    [user] = [i.args for i in stage(RUNTIME) if i.op == "USER"]
    uid, gid = user.split(":")
    assert uid != "0" and uid.isdigit()
    [setup] = [r for r in runs(RUNTIME) if "useradd" in r]
    assert f"--uid {uid}" in setup and f"--gid {gid}" in setup
    assert "mkdir -p /data /scratch" in setup and "chown curator:curator /data /scratch" in setup
    # the chart's mount points are the directories prepared here
    values = chart_values()
    assert values["persistence"]["data"]["mountPath"] == "/data"
    assert values["persistence"]["scratch"]["mountPath"] == "/scratch"


def test_exposes_the_daemons_port():
    [expose] = [i.args for i in stage(RUNTIME) if i.op == "EXPOSE"]
    port = {f.name: f.default for f in dataclasses.fields(Settings)}["port"]
    assert int(expose) == port == chart_values()["server"]["port"]


def test_image_variables_are_settings_the_code_reads():
    for name in env_of(RUNTIME):
        if name.startswith("CURATOR_"):
            assert name in daemon_strings(), name


def test_no_secret_is_baked_into_the_image():
    for instr in dockerfile():
        if instr.op in ("ENV", "ARG"):
            for name in assignments(instr):
                assert name not in SECRET_ENVS and not name.startswith("CURATOR_MASTER_KEY"), name
    assert not any(i.op == "VOLUME" for i in dockerfile())


# ---------------------------------------------------------------------------
# the build context
# ---------------------------------------------------------------------------

_BUILD_LEFTOVERS = {"node_modules", "__pycache__", "dist", "coverage", ".pytest_cache", "build"}


def _context_files(path: pathlib.Path) -> list[pathlib.Path]:
    """The files under ``path`` a clean checkout has: git-tracked ones when git is here
    (a developer's untracked junk, such as Finder's .DS_Store, is not part of CI's
    context), otherwise everything on disk."""
    if path.is_file():
        return [path]
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--", str(path.relative_to(REPO))],
                             cwd=REPO, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return [p for p in path.rglob("*") if p.is_file()]
    return [REPO / name for name in out.decode().split("\0") if name]


def test_every_copy_source_is_in_the_build_context():
    for index in (WEB, RUNTIME):
        for flags, sources, _ in copies(index):
            if "from" in flags:
                continue
            for src in sources:
                path = REPO / src
                assert path.exists(), f"COPY {src}: no such path"
                assert not dockerignored(src), f"COPY {src}: excluded by .dockerignore"
                for f in _context_files(path):
                    rel = f.relative_to(REPO).as_posix()
                    if _BUILD_LEFTOVERS & set(rel.split("/")) or rel.endswith((".pyc", ".swp")):
                        continue
                    assert not dockerignored(rel), f"{rel} is needed by COPY {src} but ignored"


@pytest.mark.parametrize("path", [
    "frontend/node_modules/esbuild/bin/esbuild",         # host binaries over the Linux ones
    "frontend/dist/index.html",
    ".venv/bin/python",
    ".git/HEAD",
    ".claude/worktrees/agent-x/backend/daemon/app.py",   # every agent worktree is a full checkout
    "backend/daemon/__pycache__/app.cpython-310.pyc",
    "backend/curation.egg-info/PKG-INFO",
    ".env",
])
def test_host_artefacts_stay_out_of_the_context(path):
    assert dockerignored(path)


def test_the_python_3_10_pyproject_reader_agrees_with_tomllib():
    tomllib = pytest.importorskip("tomllib")
    with open(PYPROJECT, "rb") as fh:
        assert mini_toml(PYPROJECT.read_text(encoding="utf-8")) == tomllib.load(fh)


def test_dockerignore_rules_parse_like_docker():
    # the last matching rule wins and a parent directory's match covers its files
    assert dockerignored("docs/design/00-overview.md")
    assert not dockerignored("docs/contracts/openapi.yaml")
    assert not dockerignored("backend/curation/pipeline/default.yaml")
    assert not dockerignored("deploy/docker-entrypoint.sh")
    assert dockerignored("deploy/charts/curator/values.yaml")
    assert DOCKERFILE.parent.name == "deploy" and CHART.is_dir()
