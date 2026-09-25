"""Helpers for the W11 tests: read the Dockerfile, and the environment the deployment gives the
Daemon.

The Helm chart lives in the rerun repository (dataverse, D53). What it hands the Daemon is
agreed in design doc 09 §2.1, a table of environment variables; :func:`deployment_env` reads
that table, so the tests here check the code against the agreement without the chart.
"""
from __future__ import annotations

import ast
import dataclasses
import functools
import json
import pathlib
import posixpath
import re
import shlex

BACKEND = pathlib.Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
DOCKERFILE = REPO / "deploy" / "Dockerfile"
ENTRYPOINT = REPO / "deploy" / "docker-entrypoint.sh"
DOCKERIGNORE = REPO / ".dockerignore"
DEPLOYMENT_DOC = REPO / "docs" / "design" / "09-deployment.md"


@dataclasses.dataclass(frozen=True)
class EnvRow:
    name: str
    value: str         # the literal the chart sets, or a description ("Secret 的 `…`")
    source: str        # where it comes from; mentions secretKeyRef for the secret ones

    @property
    def secret(self) -> bool:
        return "secretKeyRef" in self.source

    @property
    def literal(self) -> str | None:
        """The value when it is one backquoted literal, else None."""
        m = re.fullmatch(r"`([^`]*)`", self.value.strip())
        return m.group(1) if m else None


@functools.lru_cache(maxsize=1)
def deployment_env() -> dict[str, EnvRow]:
    """Design doc 09 §2.1: the Daemon container's environment as the dataverse chart sets it."""
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    section = text[text.index("### 2.1"):text.index("### 2.2")]
    rows = re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \| (.*?) \| (.*?) \|$", section, re.M)
    assert rows, "design doc 09 §2.1 has no environment table"
    names = [r[0] for r in rows]
    assert len(names) == len(set(names)), f"duplicate rows: {names}"
    return {name: EnvRow(name, value, source) for name, value, source in rows}


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
