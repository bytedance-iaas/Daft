"""The A-class guard."""
from __future__ import annotations

import json
import os
import shutil

from parity import aclass
from parity.dump_v1 import git_blob_sha1

from .conftest import ROBOT_CURATION, run_parity


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


def test_a_declared_change_passes_until_the_file_changes_again(tmp_path, v1_src):
    tree = tmp_path / "curation"
    shutil.copytree(f"{v1_src}/curation", tree)
    target = tree / "core" / "checks" / "kinematics.py"
    with open(target, "a") as fh:
        fh.write("\n# adopted\n")
    (tree / "core" / "new_check.py").write_text("x = 1\n")
    declared = tmp_path / "declared.json"
    declared.write_text(json.dumps({"files": {
        rel: {"blob": git_blob_sha1(str(tree / rel)), "since": "abc", "why": "test"}
        for rel in ("core/checks/kinematics.py", "core/new_check.py")}}))
    res = aclass.check(str(tree), declared_path=str(declared))
    assert res["changed"] == [] and res["added"] == []
    assert res["declared"] == ["core/checks/kinematics.py", "core/new_check.py"]
    proc = run_parity("a-class-check", "--tree", str(tree), "--declared", str(declared))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with open(target, "a") as fh:
        fh.write("# and one more line\n")
    res = aclass.check(str(tree), declared_path=str(declared))
    assert res["changed"] == ["core/checks/kinematics.py"]
    assert res["declared"] == ["core/new_check.py"]
    proc = run_parity("a-class-check", "--tree", str(tree), "--declared", str(declared))
    assert proc.returncode == 1


def test_working_tree_is_clean():
    """Right now no A-class file differs from the freeze commit other than the ones
    ``a_class_declared.json`` pins (design 13's video judgement, 2026-09-24)."""
    proc = run_parity("a-class-check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # every pin is in use: a declared file that went back to the freeze leaves the ledger too
    res = aclass.check(os.path.join(ROBOT_CURATION, "backend", "curation"))
    assert res["declared"] == sorted(aclass.load_declared())
