"""The camera bytes the Daemon serves itself (design doc 18 §4.1, §4.2, D60).

* :class:`DiskCache` - products on the local disk (``CURATOR_VIZ_CACHE_DIR``, by default the
  scratch volume's ``viz-cache``; never TOS), named by what they are made from, the total kept
  under ``CURATOR_VIZ_CACHE_GB`` by evicting the least recently used. A product is written next to
  its name and renamed onto it, so a reader never sees half a file; a product whose source
  fingerprint changed is simply never asked for again and ages out.
* :class:`Transcoder` - H.264 copies of cameras a browser cannot play, made by
  ``python -m curation.viz.transcode`` in a subprocess (a codec crash takes only that process) on
  a small pool of its own (``CURATOR_VIZ_TRANSCODE_WORKERS``), outside the checks' CPU pool. A
  request for a copy that is not ready answers 202 with its progress; the copy is made once,
  whoever asks again.
* :func:`file_response` - a local file with ``Range`` (Starlette's ``FileResponse``).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("daemon.viz")

#: a failed transcode is not retried for this long (whoever asks gets the failure)
FAILED_TTL_S = 300.0


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


class DiskCache:
    def __init__(self, root: os.PathLike | str, max_bytes: int):
        self.root = pathlib.Path(root)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._sizes: dict[pathlib.Path, int] | None = None

    def path(self, *parts: str) -> pathlib.Path:
        """Where a product lives (the directories are made)."""
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def get(self, path: pathlib.Path) -> pathlib.Path | None:
        if path.is_file():
            try:
                os.utime(path)                       # recently used
            except OSError:
                pass
            return path
        return None

    def _scan(self) -> dict[pathlib.Path, int]:
        if self._sizes is None:
            sizes = {}
            if self.root.is_dir():
                for dirpath, _, files in os.walk(self.root):
                    for f in files:
                        if f.endswith(".part"):
                            continue
                        p = pathlib.Path(dirpath) / f
                        try:
                            sizes[p] = p.stat().st_size
                        except OSError:
                            continue
            self._sizes = sizes
        return self._sizes

    def added(self, path: pathlib.Path, keep: tuple[pathlib.Path, ...] = ()) -> None:
        """Account for a product just renamed into place; evict the oldest beyond the cap."""
        with self._lock:
            sizes = self._scan()
            try:
                sizes[path] = path.stat().st_size
            except OSError:
                return
            total = sum(sizes.values())
            if total <= self.max_bytes:
                return
            by_age = []
            for p in list(sizes):
                try:
                    by_age.append((p.stat().st_atime, p))
                except OSError:
                    sizes.pop(p, None)
            for _, p in sorted(by_age):
                if total <= self.max_bytes:
                    break
                if p == path or p in keep:
                    continue
                try:
                    p.unlink()
                except OSError:
                    pass
                total -= sizes.pop(p, 0)

    def used(self) -> int:
        with self._lock:
            return sum(self._scan().values())


@dataclass
class Job:
    key: str
    out: pathlib.Path
    state: str = "pending"                 # pending | done | failed
    progress: float | None = None
    message: str = "平台转码中"
    finished_at: float | None = None
    future: cf.Future | None = field(default=None, repr=False)


class Transcoder:
    """One H.264 copy per (source fingerprint, episode, camera, window)."""

    def __init__(self, cache: DiskCache, workers: int = 2, *, python: str | None = None,
                 timeout_s: float = 1800.0):
        self.cache = cache
        self.workers = max(1, int(workers))
        self.python = python or sys.executable
        self.timeout_s = timeout_s
        self._pool = cf.ThreadPoolExecutor(self.workers, thread_name_prefix="viz-transcode")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def status(self, key: str, out: pathlib.Path) -> Job | None:
        with self._lock:
            job = self._jobs.get(key)
        if job is not None:
            if job.state == "failed" and job.finished_at and time.monotonic() - job.finished_at > FAILED_TTL_S:
                with self._lock:
                    self._jobs.pop(key, None)
                return None
            if job.state == "done" and not out.is_file():   # evicted since: make it again
                with self._lock:
                    self._jobs.pop(key, None)
                return None
            return job
        if self.cache.get(out) is not None:
            return Job(key, out, state="done", progress=1.0)
        return None

    def ensure(self, key: str, out: pathlib.Path, prepare: Callable[[], pathlib.Path],
               start: float | None = None, end: float | None = None,
               keep: tuple[pathlib.Path, ...] = ()) -> Job:
        """The job for ``key``: done, running, or started now. ``prepare`` returns the local input
        file (it may fetch it), on the pool's thread."""
        job = self.status(key, out)
        if job is not None:
            return job
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                return job
            job = Job(key, out, progress=0.0)
            self._jobs[key] = job
        job.future = self._pool.submit(self._run, job, prepare, start, end, keep)
        return job

    def _run(self, job: Job, prepare: Callable[[], pathlib.Path], start, end, keep) -> None:
        try:
            src = prepare()
            argv = [self.python, "-m", "curation.viz.transcode", "--in", str(src), "--out", str(job.out)]
            if start is not None:
                argv += ["--from", f"{float(start):.6f}"]
            if end is not None:
                argv += ["--to", f"{float(end):.6f}"]
            job.out.parent.mkdir(parents=True, exist_ok=True)
            from ..exec.runner import child_env, set_oom_score_adj

            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    stdin=subprocess.DEVNULL, env=child_env(dict(os.environ)))
            set_oom_score_adj(proc.pid, 500)          # the Daemon goes last when memory runs out
            error = None
            deadline = time.monotonic() + self.timeout_s
            assert proc.stderr is not None
            for line in proc.stderr:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg.get("progress"), (int, float)):
                    job.progress = float(msg["progress"])
                if isinstance(msg.get("error"), str):
                    error = msg["error"]
                if time.monotonic() > deadline:
                    proc.kill()
                    error = "转码超时"
                    break
            code = proc.wait()
            if code != 0 or not job.out.is_file():
                raise RuntimeError(error or f"转码进程退出码 {code}")
            self.cache.added(job.out, keep)
            job.state, job.progress, job.message = "done", 1.0, "完成"
        except Exception as exc:  # noqa: BLE001 - the page says why; the next ask after the TTL tries again
            log.info("viz transcode %s failed: %s", job.key, exc)
            job.state, job.message = "failed", f"平台转码失败：{exc}"[:300]
        finally:
            job.finished_at = time.monotonic()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def pending_body(job: Job) -> dict:
    """C4 ``VizMediaPending``."""
    return {"state": "failed" if job.state == "failed" else "pending",
            "progress": None if job.progress is None else round(float(job.progress), 3),
            "message": job.message}


def file_response(path: pathlib.Path, media_type: str, request_headers) -> object:
    """A local file with Range (Starlette's FileResponse answers 206 / 416 itself)."""
    from starlette.responses import FileResponse

    resp = FileResponse(str(path), media_type=media_type)
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp
