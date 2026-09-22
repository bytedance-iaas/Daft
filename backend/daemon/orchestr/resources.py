"""Resource guards the Daemon owns (design doc 04 §7, 09 §2.1).

* **Memory admission** for the frame stage: full-rate decoding is what eats memory,
  so a frame stage does not start while the container uses more than
  ``CURATOR_MEMORY_ADMISSION`` (80 %) of its memory; it waits (a pause or stop
  still gets through). Pausing the dispatch of single episodes inside a running
  command is the CLI's side of the same rule.
* **oom_score_adj**: the Daemon lowers its own score (-500, needs CAP_SYS_RESOURCE;
  best effort) and raises every child's (+500, :mod:`daemon.exec.runner`), so the
  kernel picks a child when the container is out of memory.
"""
from __future__ import annotations

import logging
import os
import pathlib
import time

from ..exec.runner import DAEMON_OOM_SCORE_ADJ, set_oom_score_adj

log = logging.getLogger("daemon.orchestr")

POLL_S = 5.0


def memory_use() -> float | None:
    """Share of the container's memory in use (cgroup v2), else of the host; None if unknown."""
    try:
        current = int(pathlib.Path("/sys/fs/cgroup/memory.current").read_text())
        limit_raw = pathlib.Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit_raw != "max":
            return current / int(limit_raw)
    except (OSError, ValueError):
        pass
    try:
        info = {}
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            info[key] = int(value.split()[0])
        total, available = info["MemTotal"], info["MemAvailable"]
        return (total - available) / total if total else None
    except (OSError, ValueError, KeyError):
        return None


def admit_memory(run, stage: str, *, probe=memory_use, sleep=time.sleep) -> None:
    limit = float(run.cfg.memory_admission or 0)
    if limit <= 0:
        return
    warned = False
    while True:
        use = probe()
        if use is None or use <= limit:
            if warned:
                run.log(stage, "info", f"内存占用降到 {use:.0%}，开始这一档" if use is not None
                        else "开始这一档")
            return
        if not warned:
            run.log(stage, "warn", f"内存占用 {use:.0%}，超过 {limit:.0%}：等内存降下来再开始帧档")
            run.progress(stage, note="等内存降下来", force=True)
            warned = True
        run.check_intent()
        sleep(POLL_S)


def lower_daemon_oom_score() -> bool:
    ok = set_oom_score_adj(os.getpid(), DAEMON_OOM_SCORE_ADJ)
    if not ok and os.path.exists("/proc/self/oom_score_adj"):
        log.info("could not lower the Daemon's oom_score_adj (needs CAP_SYS_RESOURCE); "
                 "children still get +500")
    return ok
