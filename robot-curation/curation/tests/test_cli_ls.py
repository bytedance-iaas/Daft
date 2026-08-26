"""`curation ls`:直连形态下终端看数据集桶 / 交付桶的入口(2026-08-26)。

本地一层列举在这全断言;tos:// 的真机行为按参数审计纪律在部署实例上验,
这里只钉 URL 写法错误的响亮拒。
"""
from __future__ import annotations

import pytest

from curation import cli


def _run(capsys, argv):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_ls_local_dirs_first_files_with_size(tmp_path, capsys):
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "z.txt").write_bytes(b"x" * 2048)
    (tmp_path / "a.bin").write_bytes(b"y" * 10)
    rc, out, _ = _run(capsys, ["ls", str(tmp_path)])
    assert rc == 0
    lines = out.strip().splitlines()
    assert lines[0] == "a_dir/" and lines[1] == "b_dir/", "目录在前、按名排序"
    assert any(l.startswith("a.bin") and "10 B" in l for l in lines)
    assert any(l.startswith("z.txt") and "2.0 KB" in l for l in lines)
    assert lines[-1] == "共 2 个目录、2 个文件,文件合计 2.0 KB"


def test_ls_empty_dir_says_empty(tmp_path, capsys):
    rc, out, _ = _run(capsys, ["ls", str(tmp_path)])
    assert rc == 0 and out.strip() == "(空)"


def test_ls_not_a_dir_fails_loud(tmp_path, capsys):
    rc, _, err = _run(capsys, ["ls", str(tmp_path / "nope")])
    assert rc == 2 and "不是目录" in err


def test_ls_file_cap_reports_omitted(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_LS_FILE_CAP", 2)
    for i in range(4):
        (tmp_path / f"f{i}").write_bytes(b"1")
    rc, out, _ = _run(capsys, ["ls", str(tmp_path)])
    assert rc == 0
    assert "还有 2 个文件未列出" in out, "截断必须说出省略了多少"
    assert "共 0 个目录、4 个文件" in out


def test_ls_bad_tos_url_fails_loud(capsys):
    rc, _, err = _run(capsys, ["ls", "tos://"])
    assert rc == 2 and "[输入错误]" in err


def test_ls_appears_in_top_help():
    import argparse

    p = cli.build_parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    assert "ls" in sub.choices
    ca = next(a for a in sub._choices_actions if a.dest == "ls")
    assert len(ca.help) <= 40


def test_human_size():
    assert cli._human_size(0) == "0 B"
    assert cli._human_size(1536) == "1.5 KB"
    assert cli._human_size(3 * 1024 ** 3) == "3.0 GB"


def test_tos_error_is_one_plain_line():
    class Fake(Exception):
        pass

    e = Fake({"code": "NoSuchBucket", "message": "The specified bucket does not exist.",
              "header": {"x": "y"}, "request_id": "abc"})
    line = cli._tos_err_line(e)
    assert line == "桶不存在(NoSuchBucket)"
    assert "header" not in line and "request_id" not in line
    e2 = Fake({"code": "SomethingOdd", "message": "boom"})
    assert cli._tos_err_line(e2) == "SomethingOdd:boom"
