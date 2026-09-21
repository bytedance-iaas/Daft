"""Small helpers shared across the Daemon: wall-clock time, sortable ids, canonical JSON."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_id_lock = threading.Lock()
_last_ms = 0
_last_rand = 0


def now_ms() -> int:
    """Epoch milliseconds, the only time unit stored anywhere (design doc 01, section 2)."""
    return time.time_ns() // 1_000_000


def uuid7_int() -> int:
    """A 128-bit RFC 9562 UUIDv7, strictly increasing within this process.

    Python 3.10 has no ``uuid.uuid7``. The 74 random bits are incremented when two ids
    fall in the same millisecond, so ids sort in creation order (ties in list ordering
    are broken by id).
    """
    global _last_ms, _last_rand
    with _id_lock:
        ms = now_ms()
        if ms <= _last_ms:
            ms, rand = _last_ms, _last_rand + 1
            if rand >= 1 << 74:
                ms, rand = ms + 1, int.from_bytes(os.urandom(10), "big") >> 6
        else:
            rand = int.from_bytes(os.urandom(10), "big") >> 6
        _last_ms, _last_rand = ms, rand
    rand_a, rand_b = rand >> 62, rand & ((1 << 62) - 1)
    return ((ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b


def new_id(prefix: str) -> str:
    """``<prefix>_<26 Crockford base32 chars>`` - a UUIDv7 that sorts by time (like ``task_01HX...``)."""
    value = uuid7_int()
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def canonical_json(value: Any) -> str:
    """Stable JSON text (sorted keys, no whitespace) for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
