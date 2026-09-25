"""deploy/README.md stays executable: its commands use the Secret keys the deployment reads
(design doc 09 §2.1) and never overwrite the shared Secret, its backup snippet takes a real
snapshot (repo rule: the manual steps in the README must keep working)."""
from __future__ import annotations

import re
import sqlite3

from .support import REPO, deployment_env, pyproject

README = (REPO / "deploy" / "README.md").read_text(encoding="utf-8")


def blocks(lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", README, re.S)


def test_secret_commands_use_the_keys_the_deployment_reads():
    commands = " ".join(blocks("bash")).replace("\\\n", " ")      # continuation lines joined
    referenced = {m for row in deployment_env().values() if row.secret
                  for m in re.findall(r"`([a-z_]+)`", row.value)}
    assert referenced == {"curator_master_key", "curator_master_key_next", "curator_master_key_version"}
    for key in referenced:
        assert key in commands, f"the README never sets {key}"
    # dataverse-secrets also holds the viewer's and the catalog's keys: patch single keys, and pass
    # the values through files, never as command-line literals
    assert "patch secret dataverse-secrets --type merge" in commands
    assert not re.search(r"create secret generic dataverse-secrets|dataverse-secrets.*replace -f", commands)
    assert not re.search(r"--from-literal=curator_master_key|-p '\{\"(data|stringData)", commands)
    for line in re.findall(r"patch secret dataverse-secrets[^\n]*", commands):
        assert "--patch-file <(" in line


def test_the_commands_name_real_programs():
    scripts = pyproject()["project"]["scripts"]
    for program in ("curator-rotate-master-key", "curator-daemon", "curation"):
        assert program in README and program in scripts


def test_the_backup_snippet_takes_a_consistent_snapshot(tmp_path):
    [snippet] = re.findall(r'python -c "\n(.*?)"\n', README, re.S)
    assert "VACUUM INTO" in snippet
    db = tmp_path / "curator.db"
    live = sqlite3.connect(db, isolation_level=None)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE task (id TEXT)")
    live.execute("INSERT INTO task VALUES ('t1')")
    live.execute("BEGIN IMMEDIATE")                  # the Daemon in the middle of a write
    live.execute("INSERT INTO task VALUES ('t2')")
    namespace: dict = {}
    exec(compile(snippet.replace("/data", str(tmp_path)), "README", "exec"), namespace)
    live.execute("COMMIT")
    [snapshot] = (tmp_path / "backups").glob("curator-*.db")
    assert sqlite3.connect(snapshot).execute("SELECT id FROM task").fetchall() == [("t1",)]


def test_no_key_material_in_the_readme():
    assert not re.search(r"[A-Za-z0-9+/]{40,}={0,2}", README)
