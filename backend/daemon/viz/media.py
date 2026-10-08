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
import contextlib
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
#: a ``*.part`` file older than this is a write a crash left behind: the first scan deletes it
STALE_PART_S = 3600.0


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
            stale = time.time() - STALE_PART_S
            if self.root.is_dir():
                for dirpath, _, files in os.walk(self.root):
                    for f in files:
                        p = pathlib.Path(dirpath) / f
                        if f.endswith(".part"):
                            # a write a crash left behind (one still being written is younger)
                            try:
                                if p.stat().st_mtime < stale:
                                    p.unlink()
                            except OSError:
                                pass
                            continue
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

    def drop(self, *dirs: pathlib.Path) -> int:
        """Delete whole product directories (a deleted registration's); bytes freed."""
        import shutil

        freed = 0
        with self._lock:
            sizes = self._scan()
            for d in dirs:
                if not d.is_dir():
                    continue
                for p in [p for p in sizes if d in p.parents]:
                    freed += sizes.pop(p, 0)
                shutil.rmtree(d, ignore_errors=True)
        return freed


@dataclass
class Job:
    key: str
    out: pathlib.Path
    state: str = "pending"                 # pending | done | failed
    progress: float | None = None
    message: str = "平台转码中"
    finished_at: float | None = None
    future: cf.Future | None = field(default=None, repr=False)


class JobPool:
    """Products made once on a small pool of their own (outside the checks' CPU pool), whoever asks again
    in the meantime getting the same job: done, running (202 + progress), or failed (said for
    :data:`FAILED_TTL_S`, then tried again)."""

    message = "处理中"

    def __init__(self, cache: DiskCache, workers: int, name: str):
        self.cache = cache
        self.workers = max(1, int(workers))
        self._pool = cf.ThreadPoolExecutor(self.workers, thread_name_prefix=name)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._closed = False

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
            if job.state == "done":
                self.cache.get(out)                     # recently used
            return job
        if self.cache.get(out) is not None:
            return Job(key, out, state="done", progress=1.0, message="完成")
        return None

    def _start(self, key: str, out: pathlib.Path, fn: Callable, *args, message: str | None = None) -> Job:
        job = self.status(key, out)
        if job is not None:
            return job
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                return job
            job = Job(key, out, progress=0.0, message=message or self.message)
            self._jobs[key] = job
        job.future = self._pool.submit(fn, job, *args)
        return job

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


class Builder(JobPool):
    """Products the Daemon makes in its own process with progress - depth frame packs (design doc 21 §5.3):
    ``build(progress)`` writes ``out`` (and whatever goes with it) or raises."""

    message = "生成中"

    def __init__(self, cache: DiskCache, workers: int = 2):
        super().__init__(cache, workers, "viz-build")

    def ensure(self, key: str, out: pathlib.Path, build: Callable[[Callable[[float], None]], None],
               keep: tuple[pathlib.Path, ...] = (), message: str | None = None) -> Job:
        return self._start(key, out, self._run, build, keep, message=message)

    def _run(self, job: Job, build: Callable[[Callable[[float], None]], None], keep) -> None:
        try:
            if self._closed:
                raise RuntimeError("Daemon 正在关停")

            def progress(done: float) -> None:
                job.progress = max(0.0, min(1.0, float(done)))

            build(progress)
            if not job.out.is_file():
                raise RuntimeError("没有生成出文件")
            self.cache.added(job.out, keep)
            for k in keep:
                if k.is_file():
                    self.cache.added(k, (job.out,))
            job.state, job.progress, job.message = "done", 1.0, "完成"
        except Exception as exc:  # noqa: BLE001 - the page says why; the next ask after the TTL tries again
            log.info("viz build %s failed: %s", job.key, exc)
            job.state, job.message = "failed", f"{job.message}失败：{exc}"[:300]
        finally:
            job.finished_at = time.monotonic()


class Transcoder(JobPool):
    """One H.264 copy per (source fingerprint, episode, camera, window)."""

    message = "平台转码中"

    def __init__(self, cache: DiskCache, workers: int = 2, *, python: str | None = None,
                 timeout_s: float = 1800.0):
        super().__init__(cache, workers, "viz-transcode")
        self.python = python or sys.executable
        self.timeout_s = timeout_s
        self._procs: set[subprocess.Popen] = set()

    def ensure(self, key: str, out: pathlib.Path, prepare: Callable[[], object],
               start: float | None = None, end: float | None = None,
               keep: tuple[pathlib.Path, ...] = ()) -> Job:
        """The job for ``key``: done, running, or started now. ``prepare`` returns the local input
        file (it may fetch it), on the pool's thread - or ``(file, start, end)`` when the window moved
        with it (an episode's slice, design doc 21 §4.3)."""
        return self._start(key, out, self._run, prepare, start, end, keep)

    def _run(self, job: Job, prepare: Callable[[], object], start, end, keep) -> None:
        try:
            src = prepare()
            if isinstance(src, tuple):
                src, start, end = src
            argv = [self.python, "-m", "curation.viz.transcode", "--in", str(src), "--out", str(job.out)]
            if start is not None:
                argv += ["--from", f"{float(start):.6f}"]
            if end is not None:
                argv += ["--to", f"{float(end):.6f}"]
            job.out.parent.mkdir(parents=True, exist_ok=True)
            from ..exec.runner import child_env, set_oom_score_adj

            if self._closed:
                raise RuntimeError("Daemon 正在关停")
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    stdin=subprocess.DEVNULL, env=child_env(dict(os.environ)))
            set_oom_score_adj(proc.pid, 500)          # the Daemon goes last when memory runs out
            with self._lock:
                self._procs.add(proc)
            error = None
            timed_out = threading.Event()

            def kill() -> None:                       # a child that hangs without a word is killed too
                timed_out.set()
                with contextlib.suppress(OSError):
                    proc.kill()

            timer = threading.Timer(self.timeout_s, kill)
            timer.daemon = True
            timer.start()
            try:
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
                code = proc.wait()
            finally:
                timer.cancel()
                with self._lock:
                    self._procs.discard(proc)
            if timed_out.is_set():
                error = f"转码超时（{int(self.timeout_s)} 秒）"
            if code != 0 or not job.out.is_file():
                raise RuntimeError(error or (f"转码进程退出码 {code}" if not self._closed else "Daemon 正在关停"))
            self.cache.added(job.out, keep)
            job.state, job.progress, job.message = "done", 1.0, "完成"
        except Exception as exc:  # noqa: BLE001 - the page says why; the next ask after the TTL tries again
            log.info("viz transcode %s failed: %s", job.key, exc)
            job.state, job.message = "failed", f"平台转码失败：{exc}"[:300]
        finally:
            job.finished_at = time.monotonic()

    def shutdown(self) -> None:
        """Daemon shutdown: queued jobs are dropped, running children killed (their ``.part`` stays
        and is cleared later; the next Daemon makes the copy again when it is asked for)."""
        super().shutdown()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.kill()


def pending_body(job: Job) -> dict:
    """C4 ``VizMediaPending``."""
    return {"state": "failed" if job.state == "failed" else "pending",
            "progress": None if job.progress is None else round(float(job.progress), 3),
            "message": job.message}


#: bytes one read of a blob range brings in (a response streams the range in reads this big)
BLOB_CHUNK = 1 << 20


def _etag_matches(request_headers, etag: str) -> bool:
    """Whether the request's ``If-None-Match`` lists ``etag`` (``*`` matches anything; weak ``W/``
    tags compare on the opaque value)."""
    if request_headers is None or not etag:
        return False
    inm = request_headers.get("if-none-match")
    if not inm:
        return False
    for tag in inm.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag == etag:
            return True
    return False


def _cache_headers(etag: str | None, immutable: bool) -> dict:
    """``Cache-Control`` (and ``ETag`` when there is one) for a camera byte response. Without an
    ETag the old short private cache stands; with one the browser may revalidate with the ETag, and
    ``immutable`` lets it reuse the bytes without even that - safe only where the URL's bytes never
    change (design doc 18 §5.8)."""
    if not etag:
        return {"Cache-Control": "private, max-age=600"}
    cc = "private, max-age=31536000, immutable" if immutable else "private, max-age=600"
    return {"ETag": etag, "Cache-Control": cc}


def ranged_response(read_range: Callable[[int, int], bytes], size: int, media_type: str, request_headers,
                    etag: str | None = None, immutable: bool = True) -> object:
    """Bytes that live somewhere a ``FileResponse`` cannot serve from - a Lance blob (design doc 19 §4.3) -
    with ``Range``: one ``bytes=a-b`` / ``a-`` / ``-n`` range answers 206 with the range streamed in
    :data:`BLOB_CHUNK` reads, a range past the end 416, anything else the whole body (200). A request
    whose ``If-None-Match`` matches ``etag`` answers 304 (before any range)."""
    import re as _re

    from starlette.responses import Response, StreamingResponse

    cache = _cache_headers(etag, immutable)
    if etag and _etag_matches(request_headers, etag):
        return Response(status_code=304, headers={**cache, "Accept-Ranges": "bytes"})
    headers = {"Accept-Ranges": "bytes", **cache}
    raw = (request_headers.get("range") if request_headers is not None else None) or ""
    m = _re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", raw)
    start, end, status = 0, size - 1, 200
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:
            start = max(0, size - int(m.group(2)))
        if start >= size or start > end:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(0, end - start + 1))

    def body():
        pos = start
        while pos <= end:
            n = min(BLOB_CHUNK, end - pos + 1)
            data = read_range(pos, n)
            if not data:
                break
            yield data
            pos += len(data)

    return StreamingResponse(body(), status_code=status, media_type=media_type, headers=headers)


def file_response(path: pathlib.Path, media_type: str, request_headers, etag: str | None = None,
                  immutable: bool = True) -> object:
    """A local file with Range (Starlette's FileResponse answers 206 / 416 itself). A request whose
    ``If-None-Match`` matches ``etag`` answers 304 (before any range); otherwise the ETag replaces
    the stat-based one FileResponse sets so the validator follows the content, not the mtime."""
    from starlette.responses import FileResponse, Response

    cache = _cache_headers(etag, immutable)
    if etag and _etag_matches(request_headers, etag):
        return Response(status_code=304, headers=cache)
    resp = FileResponse(str(path), media_type=media_type)
    for k, v in cache.items():
        resp.headers[k] = v
    return resp
