"""Orchestration settings, read once from the environment (design doc 04 §2.3, 09 §2).

| Variable | Default | Meaning |
|---|---|---|
| ``CURATOR_MAX_RUNNING_TASKS`` | 1 | tasks (main runs and subtasks together) running at once (P1) |
| ``CURATOR_CLI`` | ``<python> -m curation.cli`` | the command line of the CLI (shell-split) |
| ``CURATOR_SITE_CONFIG`` | ``$CURATION_CONFIG`` | planner site settings, YAML or JSON: the ``concurrency`` and ``vlm`` blocks of the site.yaml the chart writes (the CLI reads the same file through ``CURATION_CONFIG``) |
| ``CURATOR_CPU_CORES`` | the container's CPU quota, else ``os.cpu_count()`` | cores the planner plans for |
| ``CURATOR_MEMORY_ADMISSION`` | 0.8 | a frame stage waits while memory use is above this share (0 = off) |
| ``CURATOR_TERM_GRACE_S`` / ``CURATOR_INT_GRACE_S`` | 90 / 10 | SIGTERM -> SIGKILL, SIGINT -> SIGKILL (02 §4) |
| ``CURATOR_VERIFY_VISIBILITY_S`` | 60 | ``curation verify --visibility-timeout`` |
| ``CURATOR_LOCAL_DELIVERY_ROOT`` | empty | **experimental, debugging only**: deliveries ``tos://bucket/prefix`` are written to ``<root>/bucket/prefix`` on the local disk instead of TOS |
| ``CURATOR_ORCHESTRATOR`` | on | ``off`` keeps the worker pool from running anything (maintenance) |
| ``CURATOR_WORK_RETENTION_DAYS`` | 7 | a finished task's local work directory is removed this long after its last run ended (00 §4.2; 0 = never) |
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..exec.runner import default_program

log = logging.getLogger("daemon.orchestr")


class OrchestratorConfigError(RuntimeError):
    pass


def _number(env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0,
            integer: bool = False):
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise OrchestratorConfigError(f"{name} 应该是数字：{raw!r}") from None
    if value < minimum:
        raise OrchestratorConfigError(f"{name} 不能小于 {minimum}：{raw!r}")
    return value


def container_cpu_cores() -> int | None:
    """The cgroup v2 CPU quota (``cpu.max``), rounded down, at least 1; None when unlimited."""
    try:
        text = pathlib.Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except OSError:
        return None
    if len(text) != 2 or text[0] == "max":
        return None
    try:
        return max(1, int(int(text[0]) / int(text[1])))
    except (ValueError, ZeroDivisionError):
        return None


def load_site_config(path: str | None) -> dict:
    """The planner's site settings (``concurrency`` / ``vlm`` blocks); {} without a file."""
    if not path:
        return {}
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as err:
        raise OrchestratorConfigError(f"站点配置读不了：{p}（{err}）") from None
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            import yaml

            data = yaml.safe_load(text) or {}
    except Exception as err:  # noqa: BLE001 - yaml and json raise different things
        raise OrchestratorConfigError(f"站点配置 {p} 不是合法的 YAML / JSON：{err}") from None
    if not isinstance(data, dict):
        raise OrchestratorConfigError(f"站点配置 {p} 的顶层应该是一个对象")
    return {k: data[k] for k in ("concurrency", "vlm") if k in data}


@dataclass(frozen=True)
class OrchestratorConfig:
    enabled: bool = True
    max_running: int = 1
    program: tuple[str, ...] = field(default_factory=lambda: tuple(default_program()))
    site_config: dict = field(default_factory=dict)
    cpu_cores: int | None = None
    memory_admission: float = 0.8
    term_grace_s: float = 90.0
    int_grace_s: float = 10.0
    verify_visibility_s: float = 60.0
    local_delivery_root: pathlib.Path | None = None
    #: CLI calls the API waits for (metadata only: seconds, a listing can take longer)
    preflight_timeout_s: float = 300.0
    snapshot_timeout_s: float = 1800.0
    usage_flush_s: float = 5.0
    progress_write_s: float = 2.0
    #: relaunches of a crashed command that named no episode in flight, before giving up
    blank_crash_limit: int = 3
    #: how long a graceful shutdown waits for the running commands to wind down
    shutdown_wait_s: float = 100.0
    #: a finished task's work directory is cleaned this long after its last run (0 = never)
    work_retention_s: float = 7 * 86400.0
    #: how often the janitor looks for work directories to clean
    janitor_interval_s: float = 3600.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "OrchestratorConfig":
        env = os.environ if environ is None else environ
        raw_cli = str(env.get("CURATOR_CLI", "") or "").strip()
        import shlex

        program = tuple(shlex.split(raw_cli)) if raw_cli else tuple(default_program())
        local = str(env.get("CURATOR_LOCAL_DELIVERY_ROOT", "") or "").strip()
        enabled = str(env.get("CURATOR_ORCHESTRATOR", "on") or "on").strip().lower() \
            not in ("off", "0", "false", "no")
        cores = _number(env, "CURATOR_CPU_CORES", 0, minimum=0, integer=True)
        max_running = _number(env, "CURATOR_MAX_RUNNING_TASKS", 1, minimum=1, integer=True)
        term = _number(env, "CURATOR_TERM_GRACE_S", 90.0)
        site = (str(env.get("CURATOR_SITE_CONFIG", "") or "").strip()
                or str(env.get("CURATION_CONFIG", "") or "").strip())
        return cls(
            enabled=enabled,
            max_running=int(max_running),
            program=program,
            site_config=load_site_config(site or None),
            cpu_cores=int(cores) or container_cpu_cores(),
            memory_admission=float(_number(env, "CURATOR_MEMORY_ADMISSION", 0.8)),
            term_grace_s=float(term),
            int_grace_s=float(_number(env, "CURATOR_INT_GRACE_S", 10.0)),
            verify_visibility_s=float(_number(env, "CURATOR_VERIFY_VISIBILITY_S", 60.0)),
            local_delivery_root=pathlib.Path(local) if local else None,
            shutdown_wait_s=max(float(term) + 10.0, 30.0),
            work_retention_s=float(_number(env, "CURATOR_WORK_RETENTION_DAYS", 7.0)) * 86400.0,
        )

    def with_(self, **changes: Any) -> "OrchestratorConfig":
        from dataclasses import replace

        return replace(self, **changes)
