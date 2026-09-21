"""内嵌式网页终端的服务端(2026-07-29 U4)。

**替代了 ttyd**:原方案是在 pod 里另起一个 ttyd 进程听 7681,UI 用 iframe 嵌它——
两个端口、一个外部二进制、iframe 的地址还得由**观众的浏览器**解析(必须 port-forward
7681,否则页签白屏)。现在终端就是 UI 应用自己的一条 WebSocket 路由:**单端口、
无外部二进制、鉴权与 UI 同一套**(公网化时只要护住一个入口)。

设计与线协议照搬同事在运的 lerobot-agent-console(`server.py::handle_term`),
只把 aiohttp 换成 starlette(gradio 的底座),语义逐条对齐:

  浏览器 → 服务端
      **文本**帧:JSON 控制/输入消息
          {"type":"input","data":"..."}          键盘输入
          {"type":"resize","cols":N,"rows":N}    尺寸变更 → ioctl(TIOCSWINSZ)
          非 JSON 的文本帧 → 原样当输入写进 PTY(参考实现的兜底,保留)
      **二进制**帧:原样当输入写进 PTY
  服务端 → 浏览器
      **二进制**帧:PTY 读出的原始字节

移植中唯一的实现差异(线上看不出来,见 `_serve` 里的注释):参考实现在 PTY 可读回调
里直接 `ensure_future(ws.send_bytes(...))`,并发任务多了理论上可能乱序;这里改成
"回调只 put 进队列 + 单个发送协程顺序出队",顺序有保证。

⚠️ 这是**一个真 shell**(与 `kubectl exec` 等价的权限)。路由只在 `--terminal`
打开时才注册;公网暴露前必须配 Basic 鉴权(CURATION_UI_HTPASSWD_FILE 或
CURATION_UI_USER/PASSWORD,见 auth.py)+ 网关鉴权。
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import signal
import struct
import termios

from starlette.websockets import WebSocket

log = logging.getLogger("curation.ui.terminal")

#: 终端里 bash 的落脚目录。缺省按用处找:挂载根 → TOS 缓存根 → 进程 cwd。
WORKDIR_ENV = "CURATION_TERMINAL_WORKDIR"
#: 覆盖 shell(缺省找 bash,没有就 /bin/sh)。
SHELL_ENV = "CURATION_TERMINAL_SHELL"

_READ_CHUNK = 65536


def resolve_workdir() -> str:
    """终端落脚目录,按用处排:环境变量点名 > 挂载根 > TOS 缓存根 > 进程 cwd。

    直连实例(零挂载)此前落在 /app —— 一个只有代码的目录,用户 ls 一眼空白
    (2026-08-28 去挂载依赖):缓存根是直连实例上唯一"肉眼可查有东西"的地方
    (报告轻镜像、跑批产出的本地缓存都在这棵树上)。
    """
    env = os.environ.get(WORKDIR_ENV)
    if env:
        return env
    mount = (os.environ.get("CURATION_TOS_MOUNT") or "/mnt/tos").rstrip("/")
    if os.path.isdir(mount):
        return mount
    try:
        from ..tos_store import cache_root
        cr = cache_root()
        if os.path.isdir(cr):
            return cr
    except Exception:  # noqa: BLE001 缓存根算不出来就退回 cwd,别拦终端
        pass
    return os.getcwd()


def resolve_shell() -> str:
    return os.environ.get(SHELL_ENV) or shutil.which("bash") or "/bin/sh"


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


#: 提示符配色:user@host 绿、路径蓝(仿 Ubuntu 默认;2026-08-26 用户要求
#: 终端里 root@pod 一眼可辨)。
PROMPT_PS1 = r"\[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ "

_rcfile_path: str | None = None


def _rcfile() -> str:
    """bash 的 --rcfile(进程内建一次,连接间复用):先照常吃系统/个人 rc,
    最后定住彩色 PS1 —— rc 文件会无条件重设 PS1,只 export 会被盖掉。
    /etc/profile 一并補上(不再用 -l 登录壳,由这里保平)。"""
    global _rcfile_path
    if _rcfile_path is None or not os.path.exists(_rcfile_path):
        import tempfile
        fd, path = tempfile.mkstemp(prefix="curation-term-rc.", suffix=".sh")
        with os.fdopen(fd, "w") as f:
            f.write("[ -f /etc/profile ] && . /etc/profile\n"
                    "[ -f ~/.bashrc ] && . ~/.bashrc\n"
                    f"PS1='{PROMPT_PS1}'\n")
        _rcfile_path = path
    return _rcfile_path


def _spawn_pty() -> tuple[int, int]:
    """forkpty 起一个交互式 shell,返回 (子进程 pid, PTY master fd)。"""
    shell, workdir = resolve_shell(), resolve_workdir()
    argv = ([shell, "--rcfile", _rcfile()] if shell.endswith("bash")
            else [shell])                           # rcfile 要在 fork 前写好
    pid, master_fd = os.forkpty()
    if pid == 0:                                    # 子进程:立刻 exec 掉,不碰父进程状态
        try:
            os.chdir(workdir)
        except OSError:
            pass
        os.environ.setdefault("TERM", "xterm-256color")
        try:
            os.execvp(shell, argv)
        except Exception:                           # noqa: BLE001 — exec 失败必须直接死
            pass
        os._exit(127)
    return pid, master_fd


async def term_endpoint(websocket: WebSocket) -> None:
    """`/ws/term` 的处理函数(starlette WebSocket)。一条连接 = 一个独立 shell。"""
    await websocket.accept()
    pid, master_fd = _spawn_pty()
    log.info("终端会话开启: pid=%s shell=%s cwd=%s", pid, resolve_shell(), resolve_workdir())
    try:
        await _serve(websocket, master_fd)
    finally:
        _reap(pid, master_fd)
        log.info("终端会话关闭: pid=%s", pid)


async def _serve(websocket: WebSocket, master_fd: int) -> None:
    loop = asyncio.get_running_loop()
    os.set_blocking(master_fd, False)
    outbox: asyncio.Queue = asyncio.Queue()

    def _pump_pty() -> None:
        """PTY 可读回调(loop.add_reader):只读+入队,发送交给 _sender 保序。"""
        try:
            data = os.read(master_fd, _READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:                             # shell 退出后 master 端会 EIO
            data = b""
        if not data:
            with contextlib.suppress(Exception):
                loop.remove_reader(master_fd)
            outbox.put_nowait(None)                 # None = EOF 哨兵
            return
        outbox.put_nowait(data)

    async def _sender() -> None:
        while True:
            chunk = await outbox.get()
            if chunk is None:                       # shell 退出(或 _receiver 收摊)
                with contextlib.suppress(Exception):
                    await websocket.close()         # 主动收线 → 叫醒还在 receive() 的 _receiver
                return
            with contextlib.suppress(Exception):    # 浏览器已经走了,别把异常抛给 finally
                await websocket.send_bytes(chunk)

    def _feed(data: bytes) -> bool:
        """写进 PTY;shell 已死则返回 False(EIO/EPIPE),让接收循环收摊。"""
        try:
            os.write(master_fd, data)
        except OSError:
            return False
        return True

    async def _receiver() -> None:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                if not _feed(msg["bytes"]):
                    return
                continue
            text = msg.get("text")
            if text is None:
                continue
            try:
                ctrl = json.loads(text)
            except json.JSONDecodeError:
                ctrl = None                         # 非 JSON 文本帧 = 原样输入(参考实现兜底)
            if not isinstance(ctrl, dict):
                if not _feed(text.encode()):
                    return
            elif ctrl.get("type") == "resize":
                _set_winsize(master_fd, int(ctrl.get("rows", 24)), int(ctrl.get("cols", 80)))
            elif ctrl.get("type") == "input":
                if not _feed(str(ctrl.get("data", "")).encode()):
                    return

    # 只起一个后台任务(发送),接收留在本协程里;结束时用哨兵叫它自己退,**不用
    # task.cancel()** —— 裸 asyncio 的 cancel 会把 anyio 的 cancel scope 记账搅乱
    # (starlette TestClient 就跑在 anyio 上,实测会在 __exit__ 抛 CancelledError)。
    loop.add_reader(master_fd, _pump_pty)
    sender = asyncio.create_task(_sender())
    try:
        with contextlib.suppress(Exception):        # 客户端断线的各种姿势都归到收摊
            await _receiver()
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await sender


def _reap(pid: int, master_fd: int) -> None:
    """收尸:SIGHUP 打给 shell(它再把整个会话的子进程带走)→ 关 fd → waitpid 防僵尸。"""
    with contextlib.suppress(Exception):
        os.kill(pid, signal.SIGHUP)
    with contextlib.suppress(Exception):
        os.close(master_fd)
    with contextlib.suppress(Exception):
        os.waitpid(pid, os.WNOHANG)
