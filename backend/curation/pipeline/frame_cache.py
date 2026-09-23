"""Bounded lossless frame reuse local to one funnel invocation."""
from __future__ import annotations

import copy
import os
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager

_CACHES = {}


class _FrameCache:
    def __init__(self, budget=256 * 1024 * 1024):
        self.budget, self.size = budget, 0
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def decode(self, path, start, end, **kwargs):
        from ..adapters.decode import decode_window
        # Remote paths lack a stable version here; do not cache them by URI alone.
        if str(path).startswith('tos://'):
            return decode_window(path, start, end, **kwargs)
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_size, st.st_mtime_ns, start, end,
               tuple(sorted(kwargs.items())))
        with self.lock:
            if key in self.entries:
                value, _ = self.entries[key]
                self.entries.move_to_end(key)
                return copy.deepcopy(value)
        value = decode_window(path, start, end, **kwargs)
        size = sum(frame.nbytes for frame in value[0]) + value[1].nbytes
        if size <= self.budget:
            saved = copy.deepcopy(value)
            with self.lock:
                previous = self.entries.pop(key, None)
                if previous:
                    self.size -= previous[1]
                while self.entries and self.size + size > self.budget:
                    _, (_, evicted) = self.entries.popitem(last=False)
                    self.size -= evicted
                self.entries[key] = (saved, size)
                self.size += size
        return value


@contextmanager
def _cache_scope():
    key = uuid.uuid4().hex
    _CACHES[key] = _FrameCache()
    try:
        yield key
    finally:
        _CACHES.pop(key, None)


def _decode_cached(key, path, start, end, **kwargs):
    return _CACHES[key].decode(path, start, end, **kwargs)
