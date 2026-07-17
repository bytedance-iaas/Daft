"""P0 骨架验收:core 无 daft(纪律)/ 全模块可导入 / cli --help 可跑。"""
from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = PROJECT_ROOT / "curation" / "core"


def test_core_has_no_daft_import():
    """关键纪律(DESIGN.md §11.2):core/ 是框架中立层,禁止 import daft。

    用 AST 只查真实 import 语句(docstring/注释里提到 daft 不算)。
    """
    import ast

    offenders = []
    for py in CORE_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "daft" or name.startswith("daft."):
                    offenders.append(f"{py.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert not offenders, f"core/ 出现 daft import(违反契约纪律): {offenders}"


def test_all_modules_importable():
    """骨架所有模块能被 import(语法/依赖错误在此暴露)。"""
    import curation

    failures = []
    for mod in pkgutil.walk_packages(curation.__path__, prefix="curation."):
        try:
            importlib.import_module(mod.name)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{mod.name}: {e!r}")
    assert not failures, "\n".join(failures)


def test_cli_help():
    r = subprocess.run(
        [sys.executable, "-m", "curation.cli", "--help"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert r.returncode == 0, r.stderr
    assert "run" in r.stdout


def test_cli_run_bad_input_fails_loud():
    """run 已接通(P4.4);坏输入应报清晰错误而非静默成功(正向覆盖见 test_cli_e2e)。"""
    r = subprocess.run(
        [sys.executable, "-m", "curation.cli", "run",
         "--input", "/nonexistent/dataset", "--output", "/tmp/curation-test-out"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert r.returncode != 0
    assert "nonexistent" in r.stderr or "No such file" in r.stderr
