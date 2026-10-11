"""The ops commands ``curation ls`` and ``curation prune`` (design doc 27 §1, D89).

``ls`` on a local directory is asserted here in full; on ``tos://`` only a malformed address's loud refusal (the
real bucket is checked on a deployed instance). ``prune`` runs on a v2 delivery directory laid out on local disk.
"""
from __future__ import annotations

import json
import os

from curation.cli import app, ops


def _run(capsys, argv):
    rc = app.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


# ---------------------------------------------------------------- ls

def test_ls_local_dirs_first_files_with_size(tmp_path, capsys):
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "z.txt").write_bytes(b"x" * 2048)
    (tmp_path / "a.bin").write_bytes(b"y" * 10)
    rc, out, _ = _run(capsys, ["ls", str(tmp_path)])
    assert rc == 0
    lines = out.strip().splitlines()
    assert lines[0] == "a_dir/" and lines[1] == "b_dir/", "directories first, by name"
    assert any(line.startswith("a.bin") and "10 B" in line for line in lines)
    assert any(line.startswith("z.txt") and "2.0 KB" in line for line in lines)
    assert lines[-1] == "共 2 个目录、2 个文件，文件合计 2.0 KB"


def test_ls_empty_dir_says_empty(tmp_path, capsys):
    rc, out, _ = _run(capsys, ["ls", str(tmp_path)])
    assert rc == 0 and out.strip() == "（空）"


def test_ls_not_a_dir_fails_loud(tmp_path, capsys):
    rc, _, err = _run(capsys, ["ls", str(tmp_path / "nope")])
    assert rc == 2 and "不是目录" in err


def test_ls_file_cap_reports_omitted(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ops, "LS_FILE_CAP", 2)
    for i in range(4):
        (tmp_path / f"f{i}").write_bytes(b"1")
    rc, out, _ = _run(capsys, ["ls", str(tmp_path)])
    assert rc == 0
    assert "还有 2 个文件未列出" in out, "a cut list says how much it left out"
    assert "共 0 个目录、4 个文件" in out


def test_ls_bad_tos_url_fails_loud(capsys):
    rc, _, err = _run(capsys, ["ls", "tos://"])
    assert rc == 2 and "[输入错误]" in err


def test_top_help_names_the_ops_commands_only(capsys):
    rc = app.main(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ls, prune, fetch" in out
    for gone in ("rejudge", "review-page", "backends", "public"):
        assert gone not in out


def test_ops_help_is_short():
    import argparse

    p = ops.build_parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == {"ls", "prune", "fetch"}
    for ca in sub._choices_actions:
        assert len(ca.help) <= 40


def test_human_size():
    assert ops._human_size(0) == "0 B"
    assert ops._human_size(1536) == "1.5 KB"
    assert ops._human_size(3 * 1024 ** 3) == "3.0 GB"


def test_tos_error_is_one_plain_line():
    class Fake(Exception):
        pass

    e = Fake({"code": "NoSuchBucket", "message": "The specified bucket does not exist.",
              "header": {"x": "y"}, "request_id": "abc"})
    line = ops._tos_error(e)
    assert line == "桶不存在（NoSuchBucket）"
    assert "header" not in line and "request_id" not in line
    assert ops._tos_error(Fake({"code": "SomethingOdd", "message": "boom"})) == "SomethingOdd：boom"


# ---------------------------------------------------------------- prune

def _run_dir(root, run_id, *, revisions=(), complete=True, size=100):
    d = root / run_id
    (d / "checks").mkdir(parents=True)
    (d / "checks" / "x.jsonl").write_bytes(b"x" * size)
    for n, counts in revisions:
        rev = d / "revisions" / f"r{n:04d}"
        rev.mkdir(parents=True)
        (rev / "report.json").write_text(json.dumps({"overview": {"counts": counts}}))
        (rev / "commit.json").write_text("{}")
    if complete:
        (d / "_COMPLETE").write_text("")


def _delivery(tmp_path):
    root = tmp_path / "delivery"
    _run_dir(root, "20261001-090000", revisions=[(1, {"total": 8, "passed": 5, "rejected": 3, "held": 0})])
    _run_dir(root, "20261005-090000", revisions=[(1, {"total": 8, "passed": 4, "rejected": 4, "held": 0}),
                                                 (2, {"total": 8, "passed": 5, "rejected": 3, "held": 0})])
    _run_dir(root, "20261009-090000", complete=False)
    _run_dir(root, "20261009-090000-2", revisions=[(1, {"total": 2, "passed": 2, "rejected": 0, "held": 0})])
    (root / "latest").write_text("20261005-090000\n")
    (root / "notes.txt").write_text("not a run")
    return root


def _row(out, run_id):
    return next(line for line in out.splitlines() if f"] {run_id} " in line)


def test_prune_lists_runs_newest_first_and_deletes_nothing(tmp_path, capsys):
    root = _delivery(tmp_path)
    rc, out, _ = _run(capsys, ["prune", str(root)])
    assert rc == 0
    rows = [line for line in out.splitlines() if line.startswith("  [")]
    assert [r.split()[1] for r in rows] == ["20261009-090000-2", "20261009-090000",
                                           "20261005-090000", "20261001-090000"]
    assert all(r.startswith("  [保留]") for r in rows)
    latest = _row(out, "20261005-090000")
    assert "2026-10-05 09:00:00" in latest
    assert "r0002：共 8 条，通过 5、拒绝 3、待补跑 0" in latest, "the newest committed revision's counts"
    assert latest.endswith("← latest")
    assert "还没有结果版本" in _row(out, "20261009-090000")
    assert "默认只列不删" in out
    assert sorted(os.listdir(root)) == ["20261001-090000", "20261005-090000", "20261009-090000",
                                        "20261009-090000-2", "latest", "notes.txt"]


def test_prune_keep_latest_marks_the_older_runs_but_never_latest(tmp_path, capsys):
    root = _delivery(tmp_path)
    rc, out, _ = _run(capsys, ["prune", str(root), "--keep-latest", "1"])
    assert rc == 0
    assert _row(out, "20261009-090000-2").startswith("  [保留]")
    assert _row(out, "20261005-090000").startswith("  [保留]"), "the run latest names stays"
    assert _row(out, "20261009-090000").startswith("  [删除]")
    assert _row(out, "20261001-090000").startswith("  [删除]")
    assert "将删除上面标「删除」的 2 次跑批" in out and "--yes" in out
    assert len(os.listdir(root)) == 6, "without --yes nothing goes"


def test_prune_yes_deletes_the_marked_runs_only(tmp_path, capsys):
    root = _delivery(tmp_path)
    rc, out, _ = _run(capsys, ["prune", str(root), "--keep-latest", "1", "--yes"])
    assert rc == 0
    assert "已删除 20261009-090000" in out and "已删除 20261001-090000" in out
    assert "共删除 2 次跑批" in out
    assert sorted(os.listdir(root)) == ["20261005-090000", "20261009-090000-2", "latest", "notes.txt"]


def test_prune_nothing_to_delete(tmp_path, capsys):
    root = _delivery(tmp_path)
    rc, out, _ = _run(capsys, ["prune", str(root), "--keep-latest", "4"])
    assert rc == 0 and "没有要删的" in out


def test_prune_refuses_yes_without_keep_latest(tmp_path, capsys):
    root = _delivery(tmp_path)
    rc, _, err = _run(capsys, ["prune", str(root), "--yes"])
    assert rc == 2 and "--keep-latest" in err
    assert len(os.listdir(root)) == 6


def test_prune_refuses_keep_latest_zero(tmp_path, capsys):
    root = _delivery(tmp_path)
    rc, _, err = _run(capsys, ["prune", str(root), "--keep-latest", "0", "--yes"])
    assert rc == 2 and "至少是 1" in err
    assert len(os.listdir(root)) == 6


def test_prune_missing_dir_fails_loud(tmp_path, capsys):
    rc, _, err = _run(capsys, ["prune", str(tmp_path / "nope")])
    assert rc == 2 and "目录不存在" in err


def test_prune_dir_without_runs(tmp_path, capsys):
    (tmp_path / "loose.txt").write_text("x")
    rc, out, _ = _run(capsys, ["prune", str(tmp_path)])
    assert rc == 0 and "没有跑批" in out
