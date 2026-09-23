import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from curation.pipeline.execution import _POOLS, _executor_scope, _to_thread


def test_pool_exceeds_default_limit_and_preserves_context():
    value = contextvars.ContextVar('value', default='missing')
    async def run(key):
        loop = asyncio.get_running_loop()
        default = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(default)
        token = value.set('episode')
        barrier = threading.Barrier(40)
        def work():
            barrier.wait(timeout=10)
            return value.get()
        try:
            result = await asyncio.gather(*(_to_thread(key, work) for _ in range(40)))
            assert result == ['episode'] * 40
            assert loop._default_executor is default
        finally:
            value.reset(token)
    with _executor_scope(40) as key:
        asyncio.run(run(key))
    assert key not in _POOLS


def test_disabled_path_and_error_cleanup():
    async def run(key):
        assert await _to_thread(None, lambda: 3) == 3
        def fail():
            raise ValueError('boom')
        await _to_thread(key, fail)
    with pytest.raises(ValueError, match='boom'):
        with _executor_scope(2) as key:
            asyncio.run(run(key))
    assert key not in _POOLS
