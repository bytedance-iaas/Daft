"""The A-class guard."""
from __future__ import annotations

import shutil

from parity import aclass

from .conftest import run_parity


def test_classification():
    assert aclass.is_a_class("core/checks/task_success.py")
    assert aclass.is_a_class("pipeline/verdict.py")
    assert not aclass.is_a_class("pipeline/run.py")        # B class
    assert not aclass.is_a_class("cli.py")
    assert not aclass.is_a_class("coreutils.py")


def test_freeze_tree_passes_and_edits_are_caught(tmp_path, v1_src, monkeypatch):
    tree = tmp_path / "curation"
    shutil.copytree(f"{v1_src}/curation", tree)
    assert aclass.check(str(tree))["changed"] == []
    with open(tree / "core" / "checks" / "kinematics.py", "a") as fh:
        fh.write("\n# tweak\n")
    (tree / "pipeline" / "run.py").write_text("# B class may change\n")
    (tree / "core" / "new_check.py").write_text("x = 1\n")
    res = aclass.check(str(tree))
    assert res["changed"] == ["core/checks/kinematics.py"]
    assert res["added"] == ["core/new_check.py"]
    proc = run_parity("a-class-check", "--tree", str(tree))
    assert proc.returncode == 1 and "parity-change:" in proc.stderr
    monkeypatch.setenv("PR_BODY", "parity-change: comment fix, parity run attached")
    proc = run_parity("a-class-check", "--tree", str(tree), "--pr-body-env", "PR_BODY")
    assert proc.returncode == 0


def test_working_tree_is_clean():
    """Right now no A-class file differs from the freeze commit."""
    proc = run_parity("a-class-check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
