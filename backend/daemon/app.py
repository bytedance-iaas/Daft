"""The Daemon application: ``create_app(settings)`` (design docs 00 §3, 03, 09).

Routes live under the mount prefix ``{base}`` - ``{base}/api/v1/**``,
``{base}/events/**`` and the frontend - because the gateway does not strip it;
the probes answer at the root and under ``{base}``, without authentication.

Start-up order (``create_app`` then the lifespan):

1. the master key must load (else :class:`~daemon.masterkey.MasterKeyError`, no start);
2. the database opens and migrates;
3. a ``daemon.start`` audit event is written - its id is the SSE epoch;
4. lifespan start: the SSE flusher starts, startup reconciliation runs (retried
   in the background when it fails; ``/readyz`` says ``reconciled: false`` until it
   succeeds), then the ``on_ready`` hooks (W5 starts its worker pool there), then
   a maintenance thread (expired preflight / idempotency rows, deleted tasks after
   30 days, audit events and the token timeline after 90 days).

Lifespan end runs the ``on_shutdown`` hooks (W5 pauses running tasks there,
design doc 09 §2.3), stops the threads and closes the database.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import tempfile
import threading
from typing import Callable

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from . import errors
from .auth import AuthMiddleware, build_provider
from .events import EventHub
from .idempotency import Idempotency
from .instance import InstanceLock
from .logs import TaskLogs
from .masterkey import MasterKey, MasterKeyError
from .reconcile import reconcile
from .repo import protocol as P
from .routes import api, datasets, overview, sse, static, system
from .settings import Settings
from .util import now_ms
from .views import Links

log = logging.getLogger("daemon")

DAY_MS = 24 * 60 * 60 * 1000
DELETED_TASK_RETENTION_MS = 30 * DAY_MS        # P12
EVENT_RETENTION_MS = 90 * DAY_MS               # 08 §7
MAINTENANCE_INTERVAL_S = 3600.0
RECONCILE_RETRY_S = 10.0
PROBE_TIMEOUT_S = 2.0

Hook = Callable[["Runtime"], None]


def _writable(directory) -> bool:
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-"):
            pass
        return True
    except OSError:
        return False


class Runtime:
    """Everything a request or a background worker needs, one per app (``app.state.runtime``)."""

    def __init__(self, settings: Settings, repo: P.Repository, clock: Callable[[], int]):
        self.settings = settings
        self.repo = repo
        self.clock = clock
        self.master_key: MasterKey = settings.master_key
        self.epoch = repo.append_event(
            actor="system", action="daemon.start", resource="daemon", at=clock(),
            detail={"base_path": settings.base_path, "schema_version": repo.schema_version(),
                    "master_key": self.master_key.fingerprint()}).id
        self.hub = EventHub(self.epoch, buffer_size=settings.sse_buffer)
        self.auth = build_provider(settings.auth)
        self.logs = TaskLogs(settings.work_dir)
        self.idempotency = Idempotency(repo, clock)
        self.links = Links(settings.base_path, settings.public_base_url)
        self.reconciled = threading.Event()
        self.reconcile_counts: dict | None = None
        self.stopping = threading.Event()
        self.on_ready: list[Hook] = []
        self.on_stopping: list[Hook] = []
        self.on_shutdown: list[Hook] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._probe_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="readyz")
        self._probe_future: concurrent.futures.Future | None = None
        self.instance_lock: InstanceLock | None = None
        self.frontend = None

    # -- lifecycle ---------------------------------------------------------------
    def startup(self) -> None:
        self.hub.start()
        for directory in (self.settings.work_dir, self.settings.scratch_dir):
            with contextlib.suppress(OSError):
                os.makedirs(directory, exist_ok=True)
        if not self._reconcile_once():
            self._spawn("reconcile-retry", self._reconcile_until_done)
        self._spawn("maintenance", self._maintenance_loop)

    def begin_shutdown(self) -> None:
        """SIGTERM arrived: end SSE streams now, start ``on_stopping`` hooks (W5 pauses tasks).

        Called from the signal handler, so the hooks run on their own thread; the
        lifespan end (``shutdown``) comes after uvicorn has drained the connections.
        """
        if self.stopping.is_set():
            return
        self.stopping.set()
        ended = self.hub.end_streams()
        if ended:
            log.info("shutting down: ended %d SSE stream(s)", ended)

        def run_hooks() -> None:
            for hook in self.on_stopping:
                try:
                    hook(self)
                except Exception:
                    log.exception("stopping hook %r failed", hook)

        if self.on_stopping:
            self._spawn("stopping-hooks", run_hooks)

    def shutdown(self) -> None:
        self.begin_shutdown()
        for hook in reversed(self.on_shutdown):
            try:
                hook(self)
            except Exception:
                log.exception("shutdown hook %r failed", hook)
        self._stop.set()
        for th in self._threads:
            th.join(timeout=10)
        self.hub.stop()
        self._probe_pool.shutdown(wait=False)
        close = getattr(self.repo, "close", None)
        if close is not None:
            close()
        if self.instance_lock is not None:
            self.instance_lock.release()
            self.instance_lock = None

    def _spawn(self, name: str, target) -> None:
        th = threading.Thread(target=target, name=name, daemon=True)
        th.start()
        self._threads.append(th)

    def _reconcile_once(self) -> bool:
        try:
            self.reconcile_counts = reconcile(self.repo, self.hub, self.clock, logs=self.logs)
        except Exception:
            log.exception("startup reconciliation failed; retrying in %ss", RECONCILE_RETRY_S)
            return False
        self.reconciled.set()
        for hook in self.on_ready:
            try:
                hook(self)
            except Exception:
                log.exception("ready hook %r failed", hook)
        return True

    def _reconcile_until_done(self) -> None:
        while not self._stop.wait(RECONCILE_RETRY_S):
            if self._reconcile_once():
                return

    def maintenance(self) -> dict:
        now = self.clock()
        return {"expired": self.repo.purge_expired(now=now),
                "tasks": self.repo.purge_deleted_tasks(before=now - DELETED_TASK_RETENTION_MS),
                "events": self.repo.purge_events(before=now - EVENT_RETENTION_MS)}

    def _maintenance_loop(self) -> None:
        while True:
            try:
                purged = self.maintenance()
                if any(purged.values()):
                    log.info("maintenance purged %s", purged)
            except Exception:
                log.exception("maintenance failed")
            if self._stop.wait(MAINTENANCE_INTERVAL_S):
                return

    # -- readiness ---------------------------------------------------------------
    def _db_writable(self) -> bool:
        """A real write (purge of expired rows) through the single writer, bounded in time."""
        if self._probe_future is not None and not self._probe_future.done():
            return False                      # the previous probe is still stuck behind the writer
        self._probe_future = self._probe_pool.submit(self.repo.purge_expired, now=self.clock())
        try:
            self._probe_future.result(timeout=PROBE_TIMEOUT_S)
            return True
        except Exception:
            return False

    def readiness(self) -> dict[str, bool]:
        return {
            "db_writable": self._db_writable(),
            "master_key": isinstance(self.master_key, MasterKey) and len(self.master_key.key) == 32,
            "workdir_writable": _writable(self.settings.work_dir),
            "scratch_writable": _writable(self.settings.scratch_dir),
            "reconciled": self.reconciled.is_set(),
        }


def create_app(settings: Settings, *, repo: P.Repository | None = None,
               clock: Callable[[], int] = now_ms, lock_wait_s: float = 10.0) -> FastAPI:
    """Build the ASGI app.

    Raises :class:`MasterKeyError` without a valid master key and
    :class:`~daemon.instance.AlreadyRunning` when another Daemon owns the database.
    """
    if not isinstance(getattr(settings, "master_key", None), MasterKey):
        raise MasterKeyError("没有加载主密钥（CURATOR_MASTER_KEY），Daemon 拒绝启动")
    lock = None
    if repo is None:
        from .repo.sqlite import SqliteRepository

        lock = InstanceLock(settings.db_path.with_name(settings.db_path.name + ".lock"))
        lock.acquire(wait_s=lock_wait_s)
        try:
            repo = SqliteRepository(settings.db_path, clock=clock)
        except BaseException:
            lock.release()
            raise
    rt = Runtime(settings, repo, clock)
    rt.instance_lock = lock

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        import anyio

        await anyio.to_thread.run_sync(rt.startup)
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(rt.shutdown)

    app = FastAPI(title="Curator v2 Daemon", openapi_url=None, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.runtime = rt
    errors.install(app)
    base = settings.base_path
    system.install(app, base)
    for router in (api.router, datasets.router, overview.router):     # new routers go here,
        app.include_router(router, prefix=f"{base}/api/v1")              # before the fallback
    app.include_router(api.fallback, prefix=f"{base}/api/v1")
    app.include_router(sse.router, prefix=f"{base}/events")
    rt.frontend = static.install(app, settings.static_dir, base)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(AuthMiddleware, provider=rt.auth, exempt=system.probe_paths(base))
    log.info("daemon ready to start: base_path=%r db=%s auth=%s epoch=%s", base or "/",
             settings.db_path, getattr(rt.auth, "description", "?"), rt.epoch)
    return app
