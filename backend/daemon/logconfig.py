"""Structured logs on stdout with secrets masked (design doc 09 §4, 08 §3).

One JSON object per line: ``ts`` (epoch ms), ``level``, ``logger``, ``msg`` and,
when the call passes them as ``extra``, ``task_id`` / ``subtask_id`` / ``module``.
Anything that looks like a credential - ``Authorization`` headers, the
environment variables that carry keys, ``password=`` / ``api_key=`` pairs - is
replaced by ``***`` in messages and in exception tracebacks alike.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time

_SECRET_NAMES = (r"tos_secret_key|tos_access_key|tos_session_token|ark_api_key|api_key|apikey|"
                 r"secret_access_key|access_key_id|password|passwd|master_key|curator_master_key\w*|"
                 r"curation_ui_password|curator_auth_password|token")
_PATTERNS = (
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer)\s+)[A-Za-z0-9+/=._~-]+"),
    re.compile(rf"(?i)(\b(?:{_SECRET_NAMES})[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}}&]+)"),
)
_EXTRA_FIELDS = ("task_id", "subtask_id", "module")


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": int(record.created * 1000), "level": record.levelname.lower(),
                 "logger": record.name, "msg": redact(record.getMessage())}
        for key in _EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))


def configure(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
