"""The A-class guard."""
from __future__ import annotations

import json
import os

import pytest

from parity import aclass

from .conftest import ROBOT_CURATION, run_parity


def test_classification():
    assert aclass.is_a_class("core/checks/kinematics.py")
    assert aclass.is_a_class("pipeline/verdict.py")
    assert not aclass.is_a_class("pipeline/funnel.py")      # B class
    assert not aclass.is_a_class("cli/app.py")
    assert not aclass.is_a_class("coreutils.py")


@pytest.fixture
def frozen(tmp_path):
    """A small curation tree and the manifest that freezes it."""
    tree = tmp_path / "curation"
    for rel, text in (("core/checks/kinematics.py", "LIMIT = 1\n"), ("ingest/reader.py", "x = 2\n"),
                      ("pipeline/funnel.py", "# B class\n")):
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"commit": "abc", "files": {
        rel: aclass.git_blob_sha1(str(tree / rel)) for rel in ("core/checks/kinematics.py", "ingest/reader.py")}}))
    return tree, str(manifest), tmp_path / "declared.json"


def test_blob_hash_is_gits(tmp_path):
    (tmp_path / "f").write_text("x = 2\n")
    assert aclass.git_blob_sha1(str(tmp_path / "f")) == "407de3068e7b5950585d5abed9776d104235a85d"  # git hash-object


def test_the_frozen_tree_passes_and_edits_are_caught(frozen, monkeypatch):
    tree, manifest, declared = frozen
    assert aclass.check(str(tree), manifest, str(declared))["changed"] == []
    with open(tree / "core" / "checks" / "kinematics.py", "a") as fh:
        fh.write("# tweak\n")
    (tree / "pipeline" / "funnel.py").write_text("# B class may change\n")
    (tree / "core" / "new_check.py").write_text("x = 1\n")
    res = aclass.check(str(tree), manifest, str(declared))
    assert res["changed"] == ["core/checks/kinematics.py"] and res["added"] == ["core/new_check.py"]
    proc = run_parity("a-class-check", "--tree", str(tree), "--manifest", manifest, "--declared", str(declared))
    assert proc.returncode == 1 and "parity-change:" in proc.stderr
    monkeypatch.setenv("PR_BODY", "parity-change: comment fix, parity run attached")
    proc = run_parity("a-class-check", "--tree", str(tree), "--manifest", manifest, "--declared", str(declared),
                      "--pr-body-env", "PR_BODY")
    assert proc.returncode == 0


def test_a_declared_change_passes_until_the_file_changes_again(frozen):
    tree, manifest, declared = frozen
    target = tree / "core" / "checks" / "kinematics.py"
    with open(target, "a") as fh:
        fh.write("# adopted\n")
    (tree / "core" / "new_check.py").write_text("x = 1\n")
    declared.write_text(json.dumps({"files": {
        rel: {"blob": aclass.git_blob_sha1(str(tree / rel)), "since": "abc", "why": "test"}
        for rel in ("core/checks/kinematics.py", "core/new_check.py")}}))
    res = aclass.check(str(tree), manifest, str(declared))
    assert res["changed"] == [] and res["added"] == []
    assert res["declared"] == ["core/checks/kinematics.py", "core/new_check.py"]
    with open(target, "a") as fh:
        fh.write("# and one more line\n")
    res = aclass.check(str(tree), manifest, str(declared))
    assert res["changed"] == ["core/checks/kinematics.py"] and res["declared"] == ["core/new_check.py"]


def test_a_declared_removal_passes_and_an_undeclared_one_does_not(frozen):
    """An A-class file may be deleted when the algorithm it carries loses its last caller: the
    removal is pinned in the ledger the same way a change is."""
    tree, manifest, declared = frozen
    (tree / "ingest" / "reader.py").unlink()
    assert aclass.check(str(tree), manifest, str(declared))["missing"] == ["ingest/reader.py"]
    declared.write_text(json.dumps({"files": {"ingest/reader.py": {"removed": True, "since": "abc", "why": "test"}}}))
    res = aclass.check(str(tree), manifest, str(declared))
    assert res["missing"] == [] and res["declared"] == ["ingest/reader.py"]
    proc = run_parity("a-class-check", "--tree", str(tree), "--manifest", manifest, "--declared", str(declared))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_manifest_holds_a_class_files_only():
    with open(aclass.DEFAULT_MANIFEST, encoding="utf-8") as fh:
        files = json.load(fh)["files"]
    assert files and all(aclass.is_a_class(rel) for rel in files)


def test_working_tree_is_clean():
    """Right now no A-class file differs from the freeze commit other than the ones
    ``a_class_declared.json`` pins."""
    proc = run_parity("a-class-check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # every pin is in use: a declared file that went back to the freeze leaves the ledger too
    res = aclass.check(os.path.join(ROBOT_CURATION, "backend", "curation"))
    assert res["declared"] == sorted(aclass.load_declared())
