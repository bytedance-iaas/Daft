"""deploy/README.md stays executable: its values file renders, its commands use the chart's
Secret names and keys, its backup snippet takes a real snapshot (repo rule: the manual
steps in the README must keep working)."""
from __future__ import annotations

import re
import sqlite3

import yaml

from .support import (REPO, chart_values, env_entries, kinds, plain_env, pyproject, render)

README = (REPO / "deploy" / "README.md").read_text(encoding="utf-8")


def blocks(lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", README, re.S)


def test_the_documented_values_file_renders():
    [text] = [b for b in blocks("yaml") if "existingSecret: curator-master-key" in b]
    values = yaml.safe_load(re.sub(r"<[^>]+>", "placeholder", text))
    values["image"]["repository"] = "cr.example.com/kit/curator"
    docs = render(values)
    assert "Secret" not in kinds(docs)                  # the operator brings the master key
    assert plain_env(docs)["CURATOR_BASE_PATH"] == "/curation"
    ref = env_entries(docs)["CURATOR_MASTER_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "curator-master-key", "key": "masterKey"}


def test_secret_commands_use_the_charts_names_and_keys():
    values = chart_values()
    mk, auth = values["masterKey"], values["auth"]
    commands = " ".join(blocks("bash"))
    assert f"create secret generic curator-master-key --from-file={mk['key']}=" in commands
    assert f"--from-file={mk['nextKey']}=" in commands
    assert f"--from-literal={mk['versionKey']}=" in commands
    assert f"create secret generic {auth['htpasswdSecret']} --from-file={auth['htpasswdKey']}=" in commands
    assert f"--from-file={auth['passwordKey']}=" in commands
    # secrets travel in files, never as literal command-line values
    assert not re.search(r"--from-literal=(masterKey|masterKeyNext|password|htpasswd)=", commands)


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
