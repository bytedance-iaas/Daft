"""Web 终端(/ws/term)的 shell 启动配方。

2026-08-26 用户要求:提示符 root@pod 要一眼可辨(绿),路径蓝 —— 通过
bash --rcfile 实现:先照常吃系统/个人 rc,最后定住彩色 PS1(rc 会无条件
重设 PS1,只 export 环境变量会被盖掉,所以必须走 rcfile 收尾)。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from curation.ui import terminal as T


def test_rcfile_sets_colored_prompt_and_keeps_user_rc():
    path = T._rcfile()
    body = open(path, encoding="utf-8").read()
    assert "PS1=" in body and "1;32m" in body, "提示符没上绿色"
    assert "/etc/profile" in body and "~/.bashrc" in body, \
        "不许为了配色丢掉原有的 rc 加载(PATH/别名都在里面)"
    assert body.index("~/.bashrc") < body.index("PS1="), \
        "PS1 必须最后设,否则被用户 rc 盖回素色"
    assert T._rcfile() == path                      # 进程内复用,不攒临时文件


def test_rcfile_survives_deletion():
    path = T._rcfile()
    os.remove(path)
    again = T._rcfile()
    assert os.path.exists(again), "临时目录被清后要能重建(pod 长驻会遇到)"


@pytest.mark.skipif(shutil.which("bash") is None, reason="无 bash")
def test_bash_accepts_the_rcfile():
    r = subprocess.run(["bash", "--rcfile", T._rcfile(), "-i"],
                       input="echo RC-OK; exit\n",
                       capture_output=True, text=True, timeout=10)
    assert "RC-OK" in r.stdout, r.stderr
    assert r.returncode == 0
