"""Bounded, ordered execution around the unchanged fingerprint algorithms."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor


def _ordered_fingerprints(rows, workers):
    from ..dataset_level.dedup import episode_fingerprint
    iterator = iter(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for _ in range(workers):
            row = next(iterator, None)
            if row is None:
                break
            pending.append((row, pool.submit(episode_fingerprint, row)))
        while pending:
            row, future = pending.popleft()
            yield row, future.result()
            row = next(iterator, None)
            if row is not None:
                pending.append((row, pool.submit(episode_fingerprint, row)))
