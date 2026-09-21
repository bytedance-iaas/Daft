"""issue #141:跑批包装 bash 退出后无人 wait → <defunct> 攒着不消失。UI 进程装 SIGCHLD 收尸器。"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from curation.ui.reaper import install_child_reaper, reap_all, track

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc 与 SIGCHLD 语义只在 Linux 上验")


def _state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[1].split()[0]
    except OSError:
        return "gone"


def test_unwaited_wrapper_does_not_linger_as_zombie():
    """照 runner 的样子起一个 bash 包装、永不 wait:装了收尸器后它退出即被收走。"""
    assert install_child_reaper() is True
    proc = subprocess.Popen(["/bin/bash", "-c", "true"], start_new_session=True,
                            stdin=subprocess.DEVNULL)
    pid = proc.pid
    track(pid)                                 # runner 起包装时就是这么登记的
    deadline = time.time() + 5
    while time.time() < deadline and _state(pid) not in ("gone",):
        time.sleep(0.05)
    assert _state(pid) == "gone", f"pid {pid} 仍在({_state(pid)}),收尸器没生效"
    # Popen 自己再 wait 也不炸(ECHILD 由 Popen 内部消化)
    proc.wait(timeout=1)


def test_reap_all_is_idempotent_without_children():
    assert reap_all() == 0
    assert install_child_reaper() is True      # 幂等


def test_untracked_children_keep_their_exit_status_when_not_pid1():
    """非 PID 1 时不动别人的孩子:subprocess.run 照样拿到真实退出码(否则 UI 里别处的
    subprocess.run 会被吞成 0)。"""
    install_child_reaper()
    assert subprocess.run(["/bin/bash", "-c", "exit 3"]).returncode == 3


def test_pid1_mode_adopts_orphans():
    """PID 1 语义(显式打开):不登记的孩子退出也被收走。"""
    proc = subprocess.Popen(["/bin/bash", "-c", "true"], stdin=subprocess.DEVNULL)
    time.sleep(0.3)
    assert reap_all(adopt_orphans=True) >= 1
    assert _state(proc.pid) == "gone"
