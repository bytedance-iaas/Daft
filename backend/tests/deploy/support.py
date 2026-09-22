"""Helpers for the W11 tests: render the Helm chart without a cluster, read the Dockerfile.

``helm`` is looked up on ``PATH`` (or ``$CURATOR_HELM``); the chart tests skip without it.
Every helm call runs with an empty ``KUBECONFIG`` and throwaway helm homes, so nothing can
reach a cluster configured on the machine and no helm state is written outside a
temporary directory.
"""
from __future__ import annotations

import ast
import atexit
import dataclasses
import functools
import json
import os
import pathlib
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile

import pytest
import yaml

BACKEND = pathlib.Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
CHART = REPO / "deploy" / "charts" / "curator"
DOCKERFILE = REPO / "deploy" / "Dockerfile"
ENTRYPOINT = REPO / "deploy" / "docker-entrypoint.sh"
DOCKERIGNORE = REPO / ".dockerignore"

_HELM_CANDIDATES = ("/opt/homebrew/bin/helm", "/usr/local/bin/helm")


@functools.lru_cache(maxsize=1)
def helm_binary() -> str | None:
    explicit = os.environ.get("CURATOR_HELM")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    found = shutil.which("helm")
    if found:
        return found
    return next((c for c in _HELM_CANDIDATES if os.access(c, os.X_OK)), None)


@functools.lru_cache(maxsize=1)
def _helm_home() -> pathlib.Path:
    home = pathlib.Path(tempfile.mkdtemp(prefix="curator-helm-"))
    atexit.register(shutil.rmtree, home, True)
    (home / "kubeconfig").write_text("")
    return home


def _helm_env() -> dict[str, str]:
    home = _helm_home()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HELM_", "KUBE"))}
    env.update({"KUBECONFIG": str(home / "kubeconfig"), "HELM_CACHE_HOME": str(home / "cache"),
                "HELM_CONFIG_HOME": str(home / "config"), "HELM_DATA_HOME": str(home / "data")})
    return env


def run_helm(*args: str, values: dict | None = None) -> subprocess.CompletedProcess:
    binary = helm_binary()
    if binary is None:
        pytest.skip("helm is not installed (set CURATOR_HELM to its path)")
    cmd = [binary, *args]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(values or {}, fh, allow_unicode=True)
        values_file = fh.name
    try:
        return subprocess.run([*cmd, "-f", values_file], capture_output=True, text=True,
                              env=_helm_env(), timeout=120)
    finally:
        os.unlink(values_file)


def render(values: dict | None = None, *, release: str = "curator",
           namespace: str = "curator") -> list[dict]:
    """``helm template`` -> the manifests, one dict per document."""
    proc = run_helm("template", release, str(CHART), "-n", namespace, values=values)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def render_error(values: dict) -> str:
    """The error of a render that must fail (the chart's own ``fail`` messages)."""
    proc = run_helm("template", "curator", str(CHART), "-n", "curator", values=values)
    assert proc.returncode != 0, "the chart rendered values it should refuse"
    return proc.stderr


def only(docs: list[dict], kind: str, name: str | None = None) -> dict:
    found = [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)]
    assert len(found) == 1, f"expected one {kind} {name or ''}, got {len(found)}"
    return found[0]


def kinds(docs: list[dict]) -> list[str]:
    return sorted(d["kind"] for d in docs)


def container(docs: list[dict]) -> dict:
    pod = only(docs, "StatefulSet")["spec"]["template"]["spec"]
    assert len(pod["containers"]) == 1
    return pod["containers"][0]


def env_entries(docs: list[dict]) -> dict[str, dict]:
    entries = container(docs).get("env") or []
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names)), f"duplicate env names: {names}"
    return {e["name"]: e for e in entries}


def plain_env(docs: list[dict]) -> dict[str, str]:
    """The literal (non-secret) environment of the Daemon container."""
    return {n: e["value"] for n, e in env_entries(docs).items() if "value" in e}


def mounts(docs: list[dict]) -> dict[str, dict]:
    return {m["name"]: m for m in container(docs).get("volumeMounts") or []}


def pod_volumes(docs: list[dict]) -> dict[str, dict]:
    pod = only(docs, "StatefulSet")["spec"]["template"]["spec"]
    return {v["name"]: v for v in pod.get("volumes") or []}


def claim_templates(docs: list[dict]) -> dict[str, dict]:
    sts = only(docs, "StatefulSet")
    return {t["metadata"]["name"]: t for t in sts["spec"].get("volumeClaimTemplates") or []}


def chart_values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


PYPROJECT = BACKEND / "pyproject.toml"


def mini_toml(text: str) -> dict:
    """Enough TOML for backend/pyproject.toml: ``[a.b]`` tables of ``key = "string"`` /
    ``key = ["list"]`` lines (the values are valid JSON too)."""
    root: dict = {}
    table = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            table = root
            for part in line.strip("[]").split("."):
                table = table.setdefault(part.strip(), {})
            continue
        key, _, value = line.partition("=")
        table[key.strip().strip('"')] = json.loads(value.strip())
    return root


def pyproject() -> dict:
    """backend/pyproject.toml; Python 3.10 (the image's and CI's) has no tomllib."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return mini_toml(PYPROJECT.read_text(encoding="utf-8"))
    with open(PYPROJECT, "rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# what the code reads
# ---------------------------------------------------------------------------

def _string_constants(root: pathlib.Path) -> frozenset[str]:
    """Every string literal in the non-test Python sources under ``root``."""
    out: set[str] = set()
    for path in root.rglob("*.py"):
        if "tests" in path.relative_to(root).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        out.update(node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str))
    return frozenset(out)


@functools.lru_cache(maxsize=None)
def daemon_strings() -> frozenset[str]:
    """Literals of the Daemon process: backend/daemon plus the contracts loader it imports."""
    return _string_constants(BACKEND / "daemon") | _string_constants(BACKEND / "curation" / "contracts")


@functools.lru_cache(maxsize=None)
def cli_strings() -> frozenset[str]:
    """Literals of the CLI the Daemon starts (backend/curation)."""
    return _string_constants(BACKEND / "curation")


def dummy_master_key() -> str:
    import base64

    return base64.b64encode(bytes(range(1, 33))).decode()


def daemon_environ(docs: list[dict], secrets: dict[str, str] | None = None) -> dict[str, str]:
    """The Daemon container's environment with its secret references filled in.

    ``secrets`` maps ``"<secret name>/<key>"`` to a value. A reference that is not given
    gets a test value (the master key a valid one) unless it is optional - then it stays
    unset, as Kubernetes does with an optional key that is missing.
    """
    secrets = secrets or {}
    env: dict[str, str] = {}
    for name, entry in env_entries(docs).items():
        if "value" in entry:
            env[name] = str(entry["value"])
            continue
        ref = entry["valueFrom"]["secretKeyRef"]
        given = secrets.get(f"{ref['name']}/{ref['key']}")
        if given is not None:
            env[name] = given
        elif not ref.get("optional"):
            env[name] = dummy_master_key() if name == "CURATOR_MASTER_KEY" else "not-a-real-secret"
    return env


# ---------------------------------------------------------------------------
# the Dockerfile
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Instruction:
    op: str            # upper-case keyword
    args: str          # the rest, continuation lines joined
    stage: int         # index of the FROM it belongs to; -1 before the first FROM
    line: int


@functools.lru_cache(maxsize=1)
def dockerfile() -> tuple[Instruction, ...]:
    out: list[Instruction] = []
    stage, buf, start = -1, "", 0
    for no, raw in enumerate(DOCKERFILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if line.startswith("#") or (not buf and not line):
            continue                      # comment lines are dropped, inside a continuation too
        if not buf:
            start = no
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        op, _, args = buf.strip().partition(" ")
        if op.upper() == "FROM":
            stage += 1
        out.append(Instruction(op.upper(), " ".join(args.split()), stage, start))
        buf = ""
    assert not buf, "the Dockerfile ends inside a continued instruction"
    return tuple(out)


def stage(index: int) -> list[Instruction]:
    return [i for i in dockerfile() if i.stage == index]


def assignments(instr: Instruction) -> dict[str, str]:
    """``ENV a=1 b=2`` / ``ARG a=1`` -> {name: value} (``ARG a`` -> {a: ""})."""
    out = {}
    for word in shlex.split(instr.args):
        name, _, value = word.partition("=")
        out[name] = value
    return out


def copies(index: int) -> list[tuple[dict, list[str], str]]:
    """COPY instructions of a stage: (flags, sources, absolute destination)."""
    workdir, out = "/", []
    for instr in stage(index):
        if instr.op == "WORKDIR":
            workdir = posixpath.join(workdir, instr.args)
        if instr.op != "COPY":
            continue
        words = shlex.split(instr.args)
        flags = dict(w[2:].partition("=")[::2] for w in words if w.startswith("--"))
        paths = [w for w in words if not w.startswith("--")]
        dest = paths[-1]
        absolute = posixpath.normpath(posixpath.join(workdir, dest)) + ("/" if dest.endswith("/") else "")
        out.append((flags, paths[:-1], absolute))
    return out


# ---------------------------------------------------------------------------
# .dockerignore (the rules of moby/patternmatcher, for the patterns used here)
# ---------------------------------------------------------------------------

def _glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "[":
            end = pattern.index("]", i)
            out, i = out + pattern[i:end + 1], end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


@functools.lru_cache(maxsize=1)
def dockerignore_rules() -> tuple[tuple[bool, re.Pattern], ...]:
    rules = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exception = line.startswith("!")
        rules.append((exception, _glob_regex(line.lstrip("!").strip().strip("/"))))
    return tuple(rules)


def dockerignored(relpath: str) -> bool:
    """Whether ``relpath`` (relative to the repository root) is kept out of the context.

    The last pattern that matches the path or one of its parent directories decides.
    """
    parts = relpath.strip("/").split("/")
    candidates = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
    excluded = False
    for exception, rx in dockerignore_rules():
        if any(rx.match(c) for c in candidates):
            excluded = not exception
    return excluded
