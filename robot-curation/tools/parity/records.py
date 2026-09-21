"""Normalized per-episode result records.

This is the row format of ``checks/<module>/results.jsonl`` in v2 and of the
``records/<module>.jsonl`` files the v1 dump writes, so both sides can be
compared line by line. The JSON Schema lives in
``docs/contracts/cli/result-record.schema.json``.

``verdict`` keeps v1's tri-state and adds two values:

* ``pass`` / ``fail`` - ``passed`` is True / False (hard gates);
* ``abstain``        - ``passed`` is None and there is no score (a normal
                        "cannot tell", which goes to human review);
* ``scored``         - ``passed`` is None and there is a score (soft modules
                        never vote; v1 counts these apart from abstentions);
* ``error``          - the module could not judge this episode properly (D33:
                        any model call that failed after all retries, or any
                        camera that failed to decode), whatever v1 concluded.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

RECORD_SCHEMA_VERSION = "1.0"

#: The six checks of the v1 funnel, in funnel order.
FUNNEL_MODULES = ("timestamp_check", "kinematic_limits", "motion_quality",
                  "visual_quality", "video_action_sync", "task_success")

MODULE_GATE = {
    "timestamp_check": "hard", "kinematic_limits": "hard", "motion_quality": "soft",
    "visual_quality": "soft", "video_action_sync": "hard", "task_success": "hard",
    "dedup": "dedup", "skill_profile": "none",
}

_EP_RE = re.compile(r"(\d+)$")


def episode_index(episode_id: Any) -> int:
    """``"ep000034"`` / ``34`` -> ``34``."""
    if isinstance(episode_id, bool):
        raise ValueError(f"not an episode id: {episode_id!r}")
    if isinstance(episode_id, int):
        return episode_id
    m = _EP_RE.search(str(episode_id))
    if not m:
        raise ValueError(f"not an episode id: {episode_id!r}")
    return int(m.group(1))


def derive_verdict(passed: bool | None, score: float | None, error: dict | None) -> str:
    if error:
        return "error"
    if passed is True:
        return "pass"
    if passed is False:
        return "fail"
    return "scored" if score is not None else "abstain"


# ---------------------------------------------------------------------------
# D33 incidents visible in v1 details
# ---------------------------------------------------------------------------

_ALL_CAMS_DECODE_FAILED = "所有相机解码失败"


def _lists_with(obj: Any, needle: str, path: str = "") -> list[str]:
    """Paths of lists under ``obj`` that contain the string ``needle``."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += _lists_with(v, needle, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        if any(isinstance(x, str) and x == needle for x in obj):
            found.append(path)
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                found += _lists_with(v, needle, f"{path}[{i}]")
    return found


def v1_incidents(module: str, details: dict) -> list[dict]:
    """Execution failures that v1 absorbed into a degraded result.

    Only what v1 leaves in the result details can be seen here; calls that
    failed without a trace are caught by the tape (see ``vlm_tape``).
    """
    if module != "task_success" or not isinstance(details, dict):
        return []
    out: list[dict] = []
    rules = details.get("rules") or []
    reason = str(details.get("reason") or "")
    if "vlm_call_failed" in rules:
        out.append({"step": "probe", "cause": reason[:200]})
    if "internal_error" in rules or details.get("internal_error"):
        out.append({"step": "internal", "cause": reason[:200]})
    if reason.startswith(_ALL_CAMS_DECODE_FAILED):
        out.append({"step": "decode", "cause": reason[:200]})
    for cam, vote in (details.get("cam_votes") or {}).items():
        if vote == "unavail":
            out.append({"step": "endstate", "camera": cam, "cause": "vote unavailable"})
    if isinstance(details.get("endstate_review"), dict) and \
            details["endstate_review"].get("error"):
        out.append({"step": "endstate", "cause": str(details["endstate_review"]["error"])[:200]})
    lc = details.get("label_check")
    if isinstance(lc, dict) and str(lc.get("outcome") or "").startswith("error"):
        out.append({"step": "label_guard", "cause": str(lc.get("outcome"))[:200]})
    arb = details.get("arbitration")
    if isinstance(arb, dict):
        if arb.get("error"):
            out.append({"step": "arbitration", "cause": str(arb["error"])[:200]})
        for where in _lists_with(arb, "error"):
            out.append({"step": "arbitration", "cause": f"failed vote at {where}"})
    return out


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def parse_detail(detail: Any) -> dict:
    if isinstance(detail, dict):
        return detail
    if detail in (None, ""):
        return {}
    try:
        out = json.loads(detail)
    except (TypeError, ValueError):
        return {"_unparsed": str(detail)}
    return out if isinstance(out, dict) else {"_value": out}


def make_record(module: str, episode: Any, *, passed: bool | None, score: float | None,
                details: dict, incidents: list[dict] | None = None,
                evidence: list[str] | None = None, elapsed_s: float | None = None) -> dict:
    error = {"kind": "execution", "incidents": incidents} if incidents else None
    return {"episode_index": episode_index(episode), "module": module,
            "verdict": derive_verdict(passed, score, error),
            "passed": passed, "score": score, "gate": MODULE_GATE.get(module, "none"),
            "details": details, "evidence": list(evidence or []),
            "elapsed_s": elapsed_s, "error": error}


def record_from_v1_struct(module: str, episode_id: Any, struct: dict) -> dict:
    """v1 ``check_<module>`` column value ``{passed, score, detail}`` -> record."""
    details = parse_detail(struct.get("detail"))
    return make_record(module, episode_id, passed=struct.get("passed"),
                       score=struct.get("score"), details=details,
                       incidents=v1_incidents(module, details))


def sort_records(records: Iterable[dict]) -> list[dict]:
    return sorted(records, key=lambda r: (r["episode_index"], r.get("module", "")))


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


def comparable(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in VOLATILE_FIELDS}
