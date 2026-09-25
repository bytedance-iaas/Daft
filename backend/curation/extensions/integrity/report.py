"""The data integrity section of the report (design doc 14 §5.1): statistics only.

Counts by outcome and by finding code, what was read (files, bytes, how many files a CRC
covered), which tiers ran, and the dataset-level findings of ``dataset.json`` - never a
list of episodes (06 §6.2); an episode's findings are shown on its Episode 明细 block.
"""
from __future__ import annotations

import json
import os

from .findings import CODES
from .judge import DATASET_FILE, MODULE_ID


def summary(results: dict[int, dict], run_dir: str) -> dict:
    by_outcome = {"pass": 0, "reject": 0, "suspect": 0}
    by_code: dict[str, int] = {}
    files = bytes_read = crc_files = 0
    tiers = {"L1": False, "L2": False, "L3": False}
    for rec in results.values():
        if rec.get("verdict") == "error":
            continue
        d = rec.get("details") or {}
        if d.get("outcome") in by_outcome:
            by_outcome[d["outcome"]] += 1
        seen = set()
        for f in d.get("findings") or []:
            code = f.get("code")
            if code in CODES and code not in seen:          # episodes per code
                seen.add(code)
                by_code[code] = by_code.get(code, 0) + 1
        for f in d.get("files") or []:
            files += 1
            bytes_read += int(f.get("size") or 0)
            crc_files += 1 if f.get("crc") else 0
        for k, v in (d.get("tiers") or {}).items():
            tiers[k] = tiers.get(k, False) or bool(v)
    dataset = []
    try:
        with open(os.path.join(run_dir, "checks", MODULE_ID, DATASET_FILE), encoding="utf-8") as fh:
            dataset = list((json.load(fh) or {}).get("findings") or [])
    except (OSError, ValueError):
        pass
    order = list(CODES)
    return {"integrity_outcomes": by_outcome,
            "integrity_codes": [{"code": c, "name": CODES[c][1], "level": CODES[c][0], "count": n}
                                for c, n in sorted(by_code.items(), key=lambda kv: order.index(kv[0]))],
            "integrity_files": {"files": files, "bytes": bytes_read, "crc_files": crc_files},
            "integrity_tiers": tiers,
            "integrity_dataset": [{"code": f.get("code"), "message": f.get("message")} for f in dataset][:20]}


def markdown(s: dict, counts: dict, adjudication: dict | None) -> list[str]:
    o = s.get("integrity_outcomes") or {}
    f = s.get("integrity_files") or {}
    t = s.get("integrity_tiers") or {}
    lines = [f"- 通过 {o.get('pass', 0)} · 判废 {o.get('reject', 0)} · 可疑 {o.get('suspect', 0)}"
             f" · 出错 {counts.get('error', 0)}",
             f"- 读过 {f.get('files', 0)} 个文件,共 {f.get('bytes', 0) / 1e6:.1f} MB;"
             f"CRC 覆盖 {f.get('crc_files', 0)} 个(mcap 数据块)",
             f"- 逐帧解码测试:{'开' if t.get('L3') else '关'}"]
    codes = "、".join(f"{c['name']} {c['count']}" for c in s.get("integrity_codes") or []) or "无"
    lines.append(f"- 发现(条数):{codes}")
    for item in s.get("integrity_dataset") or []:
        lines.append(f"  - 数据集:{item.get('message')}")
    if adjudication:
        lines.append(f"- 人工裁决:待裁 {adjudication.get('pending', 0)} 条(可疑而仍在通过清单里、还没人裁的)")
    return lines


def table_rows(results: dict[int, dict]) -> list[dict]:
    """``integrity_findings``: one row per finding of an episode (``GET .../report/tables``)."""
    out = []
    for ep, rec in sorted(results.items()):
        d = rec.get("details") or {}
        for f in d.get("findings") or []:
            out.append({"episode_index": int(ep), "verdict": rec["verdict"],
                        "level": str(f.get("level") or ""), "code": str(f.get("code") or ""),
                        "file": str(f.get("file") or ""), "camera": str(f.get("camera") or ""),
                        "tier": str(f.get("tier") or ""), "message": str(f.get("message") or "")})
    return out
