"""File helpers of the result readers: identity-keyed caches, cached JSON, JSON-safe values.

A committed result revision never changes (the CLI refuses to write into one that
has ``commit.json``), so whatever is parsed out of it can be cached. Every cache key
still carries the file's identity (``st_mtime_ns``, ``st_size``): a run directory
restored from the delivery location gets new files, and the cache follows.
"""
from __future__ import annotations

import json
import math
import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Hashable


def identity(path: os.PathLike | str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of a file, None when it is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class LRU:
    """A small thread-safe least-recently-used cache, bounded by entries and optionally bytes.

    ``get_or_make`` builds a missing value outside the lock: two threads may build the
    same value at once, which is harmless for the pure readers that use it.
    """

    def __init__(self, max_items: int = 64, max_bytes: int | None = None):
        self.max_items = int(max_items)
        self.max_bytes = max_bytes
        self._data: OrderedDict[Hashable, tuple[Any, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return default
            self._data.move_to_end(key)
            return hit[0]

    def put(self, key: Hashable, value: Any, size: int = 0) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            if self.max_bytes is not None and size > self.max_bytes:
                return                              # too big to keep; the caller still has it
            self._data[key] = (value, size)
            self._bytes += size
            while self._data and (len(self._data) > self.max_items or (
                    self.max_bytes is not None and self._bytes > self.max_bytes)):
                _, (_, dropped) = self._data.popitem(last=False)
                self._bytes -= dropped

    def get_or_make(self, key: Hashable, make: Callable[[], Any],
                    size_of: Callable[[Any], int] | None = None) -> Any:
        missing = object()
        value = self.get(key, missing)
        if value is not missing:
            return value
        value = make()
        self.put(key, value, size_of(value) if size_of is not None else 0)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0


def read_json(path: os.PathLike | str) -> Any:
    """A JSON document as the CLI writes it (``allow_nan=True``: NaN / Infinity parse)."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cached_json(cache: LRU, path: os.PathLike | str) -> Any:
    """:func:`read_json` through ``cache``; FileNotFoundError when the file is missing.

    The parsed document is shared: callers must not change it.
    """
    ident = identity(path)
    if ident is None:
        raise FileNotFoundError(str(path))
    return cache.get_or_make(("json", str(path), ident), lambda: read_json(path))


def read_jsonl(path: os.PathLike | str) -> list[dict]:
    """Every complete JSON line; a line that does not parse (a torn write) is skipped."""
    out: list[dict] = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                out.append(value)
    return out


def json_safe(value: Any) -> Any:
    """``value`` with everything a strict JSON encoder refuses made representable.

    The CLI writes NaN into JSON (``allow_nan=True``) and Parquet cells may be NaN, numpy
    scalars or timestamps; the REST layer answers strict JSON (Starlette refuses NaN).
    Non-finite numbers become null, like a missing reading.
    """
    if isinstance(value, float):                        # numpy.float64 included
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    item = getattr(value, "item", None)
    if callable(item):                                  # numpy scalars
        try:
            return json_safe(item())
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):                                # numpy arrays
        return json_safe(tolist())
    iso = getattr(value, "isoformat", None)
    if callable(iso):                                   # datetime / date / time
        return iso()
    return str(value)
