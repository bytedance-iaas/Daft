"""C3 - the progress protocol on a command's stderr (``docs/contracts/progress.schema.json``).

Each line is one JSON object: ``progress`` (cumulative done / total), ``log``,
``usage`` (an increment) or ``throttle`` (the VLM client halved its gates). The
Daemon is tolerant: a line that is not a C3 object (a stray print from a native
library, a Python warning) becomes a ``log`` event at ``warn`` level carrying the
text, so nothing a command says is lost and nothing it says can break the run.
"""
from __future__ import annotations

import json
import time
from typing import Any

KINDS = ("progress", "log", "usage", "throttle")
LEVELS = ("error", "warn", "info", "debug")
USAGE_COUNTERS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
                  "requests", "requests_unknown_usage")
_MAX_MSG = 8000


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def parse(line: str) -> dict:
    """One stderr line -> a C3 event dict (always with ``ts`` and ``kind``)."""
    text = line.strip()
    obj: Any = None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
    if not isinstance(obj, dict) or obj.get("kind") not in KINDS:
        return {"ts": _now_ms(), "kind": "log", "level": "warn", "msg": text[:_MAX_MSG],
                "raw": True}
    ev = dict(obj)
    if not isinstance(ev.get("ts"), int) or isinstance(ev.get("ts"), bool) or ev["ts"] < 0:
        ev["ts"] = _now_ms()
    kind = ev["kind"]
    if kind == "log":
        if ev.get("level") not in LEVELS:
            ev["level"] = "info"
        ev["msg"] = str(ev.get("msg", ""))[:_MAX_MSG]
    elif kind == "progress":
        ev["done"] = _count(ev.get("done"))
        ev["total"] = _count(ev.get("total"))
        ev["stage"] = str(ev.get("stage") or "")
    elif kind == "usage":
        for key in USAGE_COUNTERS:
            ev[key] = _count(ev.get(key))
        ev["ledger"] = ev.get("ledger") if ev.get("ledger") in ("actual", "attributed") \
            else "actual"
        for key in ("model", "module", "call_kind"):
            ev[key] = str(ev.get(key) or "unknown")
    elif kind == "throttle":
        ev["backend"] = str(ev.get("backend") or "")
        ev["limit"] = _count(ev.get("limit"))
        ev["reason"] = str(ev.get("reason") or "")
    return ev


def log_line(level: str, msg: str, **extra: Any) -> dict:
    """A C3 ``log`` object the Daemon writes itself (stage logs, the ``system`` log)."""
    out = {"ts": _now_ms(), "kind": "log", "level": level if level in LEVELS else "info",
           "msg": str(msg)[:_MAX_MSG]}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out
