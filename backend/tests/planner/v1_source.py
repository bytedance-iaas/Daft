"""v1's VLM gate sizes, read from v1 itself: its ``default.yaml`` and the code that sizes each gate.

Three of the eight gates have no config key in v1; ``pipeline/funnel.py``
computes them inline (``max(2, vlm_episode_concurrency * 2)`` and so on). So
this module finds, in v1's source, the expression v1 passes where each gate is
built (:data:`GATE_SITES`), resolves the local names it uses back through the
enclosing functions' assignments, and evaluates it against a config. Nothing
here is a copy of a v1 number: if v1 changes a default or a formula, the
values read here change with it.
"""
from __future__ import annotations

import ast
import copy
import functools
import pathlib
from typing import Any

import yaml

from curation.pipeline import config as v1_config

V1_ROOT = pathlib.Path(v1_config.__file__).resolve().parent.parent      # backend/curation

#: Where v1 sizes each gate: (file, top-level function, callee, argument), where the
#: argument is a positional index or a keyword name. Every site of a gate must agree.
GATE_SITES: dict[str, list[tuple[str, str, str, int | str]]] = {
    "episode": [("pipeline/funnel.py", "run_funnel", "_episode_gate", 0)],
    "probe": [("adapters/vlm_client.py", "vlm_completion_from_config",
               "make_multiview_completion", "max_concurrency")],
    "endstate": [("pipeline/funnel.py", "run_funnel", "make_endstate_voter", "max_in_flight")],
    "arbitration": [("pipeline/funnel.py", "build_arbitration_deps", "SharedGate", 0)],
    "guard_caption": [("pipeline/funnel.py", "build_arbitration_deps", "make_vlm_captioner",
                       "max_in_flight")],
    "caption": [("pipeline/run.py", "run_pipeline", "make_vlm_captioner", "max_in_flight"),
                ("pipeline/run.py", "run_pipeline", "caption_episodes", "max_concurrency"),
                ("pipeline/run.py", "_skill_profile_stage", "caption_episodes", "max_concurrency")],
    "llm": [("pipeline/run.py", "_skill_profile_stage", "refine_taxonomy", "concurrency"),
            ("pipeline/run.py", "_skill_profile_stage", "repair_unassigned", "concurrency")],
    "audit": [("pipeline/run.py", "_skill_profile_stage", "audit_labels", "judge_concurrency")],
}

_BUILTINS = {"int": int, "float": float, "max": max, "min": min, "dict": dict}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def factory_config() -> dict:
    """v1's factory configuration: ``pipeline/default.yaml`` as ``load_config`` builds it
    (no site file, whatever ``CURATION_CONFIG`` says)."""
    with open(v1_config.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    v1_config.validate_config(cfg, v1_config.DEFAULT_CONFIG_PATH)
    cfg.setdefault("pipeline", {})
    return cfg


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class _Source:
    def __init__(self, rel: str) -> None:
        self.path = V1_ROOT / rel
        self.tree = ast.parse(self.path.read_text(encoding="utf-8"), filename=str(self.path))
        self.parents = {child: parent for parent in ast.walk(self.tree)
                        for child in ast.iter_child_nodes(parent)}
        self.constants: dict[str, Any] = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                try:
                    self.constants[node.targets[0].id] = ast.literal_eval(node.value)
                except ValueError:
                    pass

    def function(self, name: str) -> ast.AST:
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise LookupError(f"{self.path}: no top-level function {name}()")

    def scopes(self, node: ast.AST) -> list[ast.AST]:
        """Enclosing functions, innermost first."""
        out, cur = [], self.parents.get(node)
        while cur is not None:
            if isinstance(cur, _SCOPES):
                out.append(cur)
            cur = self.parents.get(cur)
        return out

    @staticmethod
    def _own_nodes(scope: ast.AST):
        """Nodes of a function body, not descending into nested functions or classes."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            yield node
            if not isinstance(node, _SCOPES + (ast.ClassDef,)):
                stack.extend(ast.iter_child_nodes(node))

    def _assignment(self, name: str, before: int, scopes: list[ast.AST]):
        for i, scope in enumerate(scopes):
            found = [n for n in self._own_nodes(scope) if isinstance(n, ast.Assign)
                     and n.lineno < before
                     and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
            if found:
                return max(found, key=lambda n: n.lineno), scopes[i:]
        return None

    def evaluate(self, expr: ast.expr, line: int, scopes: list[ast.AST], env: dict) -> Any:
        namespace = dict(_BUILTINS)
        namespace.update(env)
        for name in sorted({n.id for n in ast.walk(expr) if isinstance(n, ast.Name)} - set(namespace)):
            found = self._assignment(name, line, scopes)
            if found is not None:
                assign, outer = found
                namespace[name] = self.evaluate(assign.value, assign.lineno, outer, env)
            elif name in self.constants:
                namespace[name] = self.constants[name]
            else:
                raise LookupError(f"{self.path}:{line}: cannot resolve {name!r}")
        code = compile(ast.fix_missing_locations(ast.Expression(body=expr)), str(self.path), "eval")
        return eval(code, {"__builtins__": {}}, namespace)       # noqa: S307 - v1's own source

    def call_arguments(self, function: str, callee: str, argument: int | str, cfg: dict) -> list:
        fn = self.function(function)
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n) == callee]
        if not calls:
            raise LookupError(f"{self.path}: {function}() never calls {callee}()")
        values = []
        for call in calls:
            if isinstance(argument, int):
                expr = call.args[argument]
            else:
                matches = [k.value for k in call.keywords if k.arg == argument]
                if not matches:
                    raise LookupError(f"{self.path}:{call.lineno}: {callee}() has no {argument}=")
                expr = matches[0]
            values.append(self.evaluate(expr, call.lineno, self.scopes(call),
                                        {"cfg": copy.deepcopy(cfg)}))
        return values


@functools.lru_cache(maxsize=None)
def _source(rel: str) -> _Source:
    return _Source(rel)


def gate_values(cfg: dict) -> dict[str, list[int]]:
    """Every site's value for every gate under ``cfg`` (a gate is a semaphore of >= 1)."""
    return {gate: [max(1, int(v)) for rel, fn, callee, arg in sites
                   for v in _source(rel).call_arguments(fn, callee, arg, cfg)]
            for gate, sites in GATE_SITES.items()}


def gates(cfg: dict) -> dict[str, int]:
    """One value per gate; raises if v1's call sites of a gate disagree."""
    out = {}
    for gate, values in gate_values(cfg).items():
        if len(set(values)) != 1:
            raise AssertionError(f"v1 sizes gate {gate} differently at its call sites: {values}")
        out[gate] = values[0]
    return out


def factory_gates() -> dict[str, int]:
    return gates(factory_config())
