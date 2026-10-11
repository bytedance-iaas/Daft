"""Reading and comparing result records (``checks/<module>/results.jsonl``, C2 2.0).

The JSON Schema lives in ``docs/contracts/cli/result-record.schema.json``.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable


def write_jsonl(path: str, rows: Iterable[dict]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, allow_nan=True, sort_keys=True) + "\n")
            n += 1
    return n


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# Exact comparison
# ---------------------------------------------------------------------------

def _same_scalar(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return repr(a) == repr(b)
    return a == b


def diff_values(a: Any, b: Any, path: str = "", limit: int = 50) -> list[dict]:
    """Differences between two JSON-like values, compared exactly.

    Floats compare by ``repr`` (so ``0.1`` vs ``0.1000000001`` differs and NaN
    equals NaN); an int never equals a float; dict key order does not matter,
    list order does.
    """
    out: list[dict] = []

    def walk(x, y, p):
        if len(out) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y), key=str):
                sub = f"{p}.{k}" if p else str(k)
                if k not in x:
                    out.append({"path": sub, "golden": "<missing>", "candidate": y[k]})
                elif k not in y:
                    out.append({"path": sub, "golden": x[k], "candidate": "<missing>"})
                else:
                    walk(x[k], y[k], sub)
                if len(out) >= limit:
                    return
            return
        if isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                out.append({"path": f"{p}.length", "golden": len(x), "candidate": len(y)})
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(xi, yi, f"{p}[{i}]")
                if len(out) >= limit:
                    return
            return
        if not _same_scalar(x, y):
            out.append({"path": p or "<root>", "golden": x, "candidate": y})

    walk(a, b, path)
    return out


#: Record fields that are expected to differ between runs.
VOLATILE_FIELDS = ("elapsed_s", "evidence")


def _platform():
    """v2's record helpers (``curation.pipeline.records``), importable from the repo's backend."""
    import os
    import sys

    backend = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from curation.pipeline import records as v2_records

    return v2_records


def is_error(record: dict) -> bool:
    return record.get("status") == "error"


def verdict_of(record: dict) -> str | None:
    """The record's verdict word (pass / fail / abstain / scored / error) from its findings' default levels."""
    return _platform().legacy_verdict(record)


def comparable(record: dict) -> dict:
    """The record as compared: everything but what differs between runs anyway (``VOLATILE_FIELDS``)."""
    return {k: v for k, v in record.items() if k not in VOLATILE_FIELDS}
