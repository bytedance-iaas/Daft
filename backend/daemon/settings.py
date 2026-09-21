"""Daemon settings, read once from the environment (design doc 09).

| Variable | Default | Meaning |
|---|---|---|
| ``CURATOR_BASE_PATH`` (old name ``CURATION_UI_ROOT_PATH``) | empty | mount prefix; ``curation`` and ``/curation/`` both become ``/curation`` |
| ``CURATOR_DATA_DIR`` | ``/data`` | data volume: the database and the task work directories |
| ``CURATOR_DB_PATH`` | ``<data>/curator.db`` | SQLite file (block storage only, never NAS / FSX) |
| ``CURATOR_WORK_DIR`` | ``<data>/runs`` | ``<work>/<task_id>/`` is a task's work directory (00 §4.2) |
| ``CURATOR_SCRATCH_DIR`` | ``$TMPDIR/curator-scratch`` | disposable temp space (export videos) |
| ``CURATOR_STATIC_DIR`` | ``/app/web`` or ``frontend/dist`` if present | built frontend |
| ``CURATOR_PUBLIC_BASE_URL`` | empty | absolute links for agents; empty = relative links |
| ``CURATOR_LOCAL_DATA_ROOT`` | empty | enables the experimental local-path input under this root |
| ``CURATOR_MASTER_KEY`` (+ ``_NEXT``, ``_VERSION``) | required | see :mod:`daemon.masterkey` |
| auth variables | - | see :mod:`daemon.auth` |
| ``CURATOR_SSE_HEARTBEAT_S`` | 15 | idle SSE streams get a comment line this often |
| ``CURATOR_HOST`` / ``CURATOR_PORT`` | ``0.0.0.0`` / 8080 | listen address |
| ``CURATOR_LOG_LEVEL`` / ``CURATOR_LOG_FORMAT`` | ``INFO`` / ``json`` | logging (``json`` or ``text``) |
"""
from __future__ import annotations

import os
import pathlib
import tempfile
from dataclasses import dataclass, field, replace
from typing import Mapping

from . import auth as auth_mod
from .masterkey import MasterKey
from .masterkey import load as load_master_key

BASE_PATH_ENVS = ("CURATOR_BASE_PATH", "CURATION_UI_ROOT_PATH")


class ConfigError(RuntimeError):
    """The environment does not describe a runnable Daemon (message is for operators)."""


def normalize_base_path(raw: str | None) -> str:
    """``""`` or ``"/x"`` without a trailing slash (v1 ``normalize_root_path``)."""
    s = (raw or "").strip().strip("/")
    if not s:
        return ""
    if any(part in ("", ".", "..") for part in s.split("/")) or any(ch in s for ch in "?#%\\ "):
        raise ConfigError(f"挂载前缀写法不对：{raw!r}（形如 /curation）")
    return "/" + s


def _default_static_dir() -> pathlib.Path | None:
    repo_dist = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "dist"
    for cand in (pathlib.Path("/app/web"), repo_dist):
        if (cand / "index.html").is_file():
            return cand
    return None


@dataclass(frozen=True)
class Settings:
    master_key: MasterKey
    base_path: str = ""
    data_dir: pathlib.Path = pathlib.Path("/data")
    db_path: pathlib.Path | None = None
    work_dir: pathlib.Path | None = None
    scratch_dir: pathlib.Path | None = None
    static_dir: pathlib.Path | None = None
    public_base_url: str = ""
    local_data_root: pathlib.Path | None = None
    auth: auth_mod.AuthConfig = field(default_factory=auth_mod.AuthConfig)
    sse_heartbeat_s: float = 15.0
    sse_buffer: int = 200
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    log_format: str = "json"

    def __post_init__(self) -> None:
        # frozen dataclass: fill derived paths through object.__setattr__
        data = pathlib.Path(self.data_dir)
        object.__setattr__(self, "data_dir", data)
        object.__setattr__(self, "base_path", normalize_base_path(self.base_path))
        object.__setattr__(self, "db_path", pathlib.Path(self.db_path or data / "curator.db"))
        object.__setattr__(self, "work_dir", pathlib.Path(self.work_dir or data / "runs"))
        object.__setattr__(self, "scratch_dir", pathlib.Path(
            self.scratch_dir or pathlib.Path(tempfile.gettempdir()) / "curator-scratch"))
        object.__setattr__(self, "public_base_url", (self.public_base_url or "").strip().rstrip("/"))
        if self.public_base_url and not self.public_base_url.startswith(("http://", "https://")):
            raise ConfigError(f"CURATOR_PUBLIC_BASE_URL 必须以 http:// 或 https:// 开头："
                              f"{self.public_base_url!r}")
        if self.sse_heartbeat_s <= 0:
            raise ConfigError("CURATOR_SSE_HEARTBEAT_S 必须大于 0")

    def with_(self, **changes) -> "Settings":
        return replace(self, **changes)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Raises :class:`ConfigError` / :class:`~daemon.masterkey.MasterKeyError`."""
        env = os.environ if environ is None else environ

        def get(name: str, default: str = "") -> str:
            return str(env.get(name, default) or "").strip()

        base = next((env[k] for k in BASE_PATH_ENVS if str(env.get(k, "") or "").strip()), "")
        data_dir = pathlib.Path(get("CURATOR_DATA_DIR", "/data"))

        def path(name: str) -> pathlib.Path | None:
            value = get(name)
            return pathlib.Path(value) if value else None

        static = path("CURATOR_STATIC_DIR")
        try:
            heartbeat = float(get("CURATOR_SSE_HEARTBEAT_S", "15"))
            port = int(get("CURATOR_PORT", "8080"))
        except ValueError as err:
            raise ConfigError(f"数值型配置写法不对：{err}") from None
        log_format = get("CURATOR_LOG_FORMAT", "json").lower()
        if log_format not in ("json", "text"):
            raise ConfigError("CURATOR_LOG_FORMAT 只能是 json 或 text")
        return cls(
            master_key=load_master_key(env),
            base_path=base,
            data_dir=data_dir,
            db_path=path("CURATOR_DB_PATH"),
            work_dir=path("CURATOR_WORK_DIR"),
            scratch_dir=path("CURATOR_SCRATCH_DIR"),
            static_dir=static if static is not None else _default_static_dir(),
            public_base_url=get("CURATOR_PUBLIC_BASE_URL"),
            local_data_root=path("CURATOR_LOCAL_DATA_ROOT"),
            auth=auth_mod.AuthConfig.from_env(env),
            sse_heartbeat_s=heartbeat,
            host=get("CURATOR_HOST", "0.0.0.0"),
            port=port,
            log_level=get("CURATOR_LOG_LEVEL", "INFO").upper(),
            log_format=log_format,
        )
