"""UI 进程的子进程收尸(issue #141,2026-09-16)。

容器里质检台 UI 就是 PID 1,而且每次跑批都会 Popen 一个 bash 包装(runner.py),
包装跑完后没人 wait() 它 —— runner 故意只读退出码文件和 /proc 判死活(2026-08-14
"停止卡死"的正解),于是每跑一次留一个 `[bash] <defunct>`,永远不消失。父进程是
PID 1 的话,被过继来的孤儿也同样无人收。

解法:装一个 SIGCHLD 处理器。runner 起包装时 track(pid) 登记,退出即被 waitpid 收走;
本进程是 PID 1 时再 waitpid(-1) 把过继来的孤儿一并收掉(非 PID 1 不做:那会吞掉别处
subprocess.run 正等着的退出码)。
- runner 从不靠 Popen.returncode 判结果,先被这里收走对它零影响;
- terminal.py 关会话时的 waitpid 本来就包了异常,收不到当没事。
只在 POSIX 上装;镜像入口另外套了 tini(Dockerfile)当兜底 init。
"""
from __future__ import annotations

import errno
import os
import signal

_installed = False
_tracked: set = set()          # 本进程 Popen 出来、事后没人 wait 的子进程(跑批包装 bash)


def track(pid: int) -> None:
    """登记一个"起了就不管"的子进程:退出后由收尸器 wait 它。"""
    if pid and pid > 0:
        _tracked.add(int(pid))


def _reap_one(pid: int) -> bool:
    try:
        got, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True                # 已经没这个孩子了(被别处收了):当收过
    except OSError:
        return False
    return got == pid


def reap_all(*, adopt_orphans: bool | None = None) -> int:
    """收尸:①登记过的包装进程逐个 waitpid;②本进程是 PID 1 时(容器里 UI 直接当 init),
    再把所有已退出的孩子(过继来的孤儿)一并收掉。返回收了几个。

    ②只在 PID 1 时做:waitpid(-1) 会把别人正等着的退出码也吞掉(subprocess.run 拿到的
    returncode 变 0),非 PID 1 的进程里没有孤儿要管,不做。adopt_orphans 可显式指定(测试用)。"""
    if not hasattr(os, "waitpid"):
        return 0
    n = 0
    for pid in list(_tracked):
        if _reap_one(pid):
            _tracked.discard(pid)
            n += 1
    if adopt_orphans is None:
        adopt_orphans = os.getpid() == 1
    if adopt_orphans:
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            except OSError as e:  # noqa: PERF203
                if e.errno in (errno.ECHILD, errno.EINTR):
                    break
                break
            if pid == 0:
                break
            _tracked.discard(pid)
            n += 1
    return n


def install_child_reaper() -> bool:
    """在当前进程装 SIGCHLD 收尸器(幂等)。装上返回 True;非 POSIX 返回 False。"""
    global _installed
    if _installed:
        return True
    if not hasattr(signal, "SIGCHLD"):
        return False

    def _on_sigchld(_signum, _frame):
        try:
            reap_all()
        except Exception:  # noqa: BLE001  信号处理器里绝不能抛
            pass

    signal.signal(signal.SIGCHLD, _on_sigchld)
    _installed = True
    reap_all()                     # 装之前已经死掉的一并收了
    return True
