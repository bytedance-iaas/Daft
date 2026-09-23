"""Invocation-scoped executors addressable by serializable Daft closures."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

_POOLS: dict[str, ThreadPoolExecutor] = {}
_LOCK = threading.Lock()


@contextmanager
def _executor_scope(workers: int):
    key = uuid.uuid4().hex
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='curation-vlm')
    with _LOCK:
        _POOLS[key] = pool
    try:
        yield key
    finally:
        # Running synchronous calls cannot be forcibly cancelled, as with to_thread.
        pool.shutdown(wait=True, cancel_futures=True)
        with _LOCK:
            _POOLS.pop(key, None)


async def _to_thread(key: str | None, fn, *args):
    if key is None:
        return await asyncio.to_thread(fn, *args)
    with _LOCK:
        pool = _POOLS[key]
    call = functools.partial(contextvars.copy_context().run, fn, *args)
    return await asyncio.get_running_loop().run_in_executor(pool, call)
