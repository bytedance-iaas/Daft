"""One Daemon per data volume (D2; design doc 09 §2).

SQLite takes one writer, and two Daemons on one database would both run the
queue. A StatefulSet stops the old pod before starting the new one, but a
Deployment (or a second ``python -m daemon`` by hand) would overlap - and a
ReadWriteOnce volume can still be mounted by two pods on the same node. So the
Daemon holds an exclusive ``flock`` on ``<db>.lock`` for its lifetime; the kernel
drops it when the process dies, however it dies.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time


class AlreadyRunning(RuntimeError):
    """Another Daemon holds the lock on this data volume."""


class InstanceLock:
    def __init__(self, path: os.PathLike | str):
        self.path = os.fspath(path)
        self._fd: int | None = None

    def acquire(self, *, wait_s: float = 10.0) -> "InstanceLock":
        """Take the lock, waiting a little for a Daemon that is just shutting down."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as err:
                if err.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise AlreadyRunning(
                        f"另一个 Daemon 进程正在使用这个数据卷（{self.path} 已被锁住）；"
                        "SQLite 只能有一个写者，请确认只部署了一个副本") from None
                time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
