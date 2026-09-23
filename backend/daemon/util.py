"""Small helpers shared across the Daemon: wall-clock time, record ids, canonical JSON."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import string
import time
from typing import Any

#: Record ids (D45): ``<prefix>-<9 lowercase letters>``, e.g. ``task-kqzmrtbwe``.
ID_LETTERS = string.ascii_lowercase
ID_LENGTH = 9
#: How many ids a caller draws before it gives up on a collision. 26**9 is about 5.4e12
#: ids per prefix, so even a second draw is vanishingly rare; the bound only stops a loop.
ID_ATTEMPTS = 8


def now_ms() -> int:
    """Epoch milliseconds, the only time unit stored anywhere (design doc 01, section 2)."""
    return time.time_ns() // 1_000_000


def new_id(prefix: str) -> str:
    """``<prefix>-<9 random lowercase letters>`` (D45), from the system's CSPRNG.

    Ids carry no time: lists sort by ``created_at``. Random ids can collide, so whoever
    stores one draws again when it is taken (the repository does it for the ids it
    makes; see ``daemon.repo.protocol.IdTaken`` for ids a caller chose). Records made
    before D45 keep their ``<prefix>_<26 Crockford base32 characters>`` ids.
    """
    return f"{prefix}-{''.join(secrets.choice(ID_LETTERS) for _ in range(ID_LENGTH))}"


def id_regex(prefix: str, legacy: str) -> str:
    """Regex source, without anchors, that accepts both id formats of ``prefix``: the one
    records made before D45 kept (``legacy``, itself a regex) and ``<prefix>-<9 letters>``."""
    return f"(?:{legacy}|{re.escape(prefix)}-[a-z]{{{ID_LENGTH}}})"


def canonical_json(value: Any) -> str:
    """Stable JSON text (sorted keys, no whitespace) for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
