"""P0 骨架验收:core 无 daft(纪律)/ 全模块可导入 / cli --help 可跑。"""
from __future__ import annotations

import importlib
import os
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


def test_cli_top_help_is_a_directory_not_a_manual():
    """顶层 --help 是"选命令的目录":一条一句话、无行话残留;细节住在子命令
    自己的 --help(description)里,没丢只是搬家(2026-08-26 用户审阅定)。"""
    import argparse

    from curation.cli import build_parser
    p = build_parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    for ca in sub._choices_actions:
        assert len(ca.help) <= 40, f"{ca.dest}:顶层一条要一句话,细节下沉子命令 --help"
        assert "**" not in ca.help and ".csv" not in ca.help, \
            f"{ca.dest}:markdown 星号 / 实现细节不进顶层目录"
    # 参数级 help 与子命令 description 也不许带 markdown 星号(终端不渲染)
    for name, sp in sub.choices.items():
        assert "**" not in (sp.description or ""), f"{name}: description 带星号"
        for a in sp._actions:
            assert "**" not in (a.help or ""), f"{name} --{a.dest}: help 带星号"
    top = p.format_help()
    assert "--version" in top and "显示本帮助并退出" in top
    assert "curation <命令> --help" in top          # 结尾指引在
    # --help 只亮可靠的(2026-08-26 用户定):RRD 关闭态的 --rrd-fps 与
    # 无验证记录的 --refresh 藏起来,但功能保留(隐藏≠删除)
    assert "--rrd-fps" not in sub.choices["review-page"].format_help()
    assert "--refresh" not in sub.choices["public"].format_help()
    # --run-name 是 UI 任务台的内部通道(自由名会造出 prune/latest 不认的
    # 幽灵批次,入口已校验拒绝)→ 对人隐藏,机器照用
    assert "--run-name" not in sub.choices["run"].format_help()
    ns = p.parse_args(["run", "--input", "x", "--output", "y",
                       "--run-name", "20260826-000001"])
    assert ns.run_name == "20260826-000001"
    a = p.parse_args(["review-page", "--input", "x", "--output", "y",
                      "--rrd-fps", "30"])
    assert a.rrd_fps == 30.0
    assert p.parse_args(["public", "--refresh"]).refresh is True
    # 细节搬进了子命令 --help,一个字没丢
    assert "label_decisions.csv" in sub.choices["rejudge"].format_help()
    # reprofile 整命令对客户隐藏(2026-08-27 用户定):顶层目录 / usage /
    # 错误提示的候选列表都不出现;功能与自身 --help 原样保留(运维工具)
    assert "reprofile" not in sub.choices and "reprofile" not in top
    from curation.cli import _reprofile_parser
    _flat = _reprofile_parser().format_help().replace("\n", "").replace(" ", "")
    assert "第二次报0条变化" in _flat and "与rejudge的区别" in _flat
    from curation.cli import main as _main
    ns = _reprofile_parser().parse_args(["--delivery", "/tmp/x"])
    assert ns.delivery == "/tmp/x"          # 隐藏≠删除,参数面原样
    # 80 列终端正文零超宽(中文双宽按显示宽度折行;usage 段是 argparse 自家
    # 折行逻辑,轻微超宽属可接受,不在此列)
    import unicodedata

    def dw(line):
        return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in line)

    os.environ["COLUMNS"] = "80"          # argparse 折行宽度经 shutil 读它
    try:
        helps = {name: sp.format_help()
                 for name, sp in [("<top>", p)] + list(sub.choices.items())}
    finally:
        os.environ.pop("COLUMNS", None)
    for name, text in helps.items():
        in_usage = False
        for line in text.splitlines():
            if line.startswith("usage:"):
                in_usage = True
            elif in_usage and not line.startswith(" "):
                in_usage = False
            if not in_usage:
                assert dw(line) <= 80, f"{name} 超宽 {dw(line)} 列:{line[:40]}"


def test_cli_version_flag():
    r = subprocess.run(
        [sys.executable, "-m", "curation.cli", "--version"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("curation ")
