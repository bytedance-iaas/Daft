"""camera_defects: glitch, shake and lens contamination, reported by the model in the
per-camera review it already makes for task_success (no request of its own).

Every endstate review asks the model for an extra ``camera_check`` field next to its verdict
(``adapters.video_vlm.CAMERA_CHECK_PROMPT``); this module reads those answers back out of
task_success's per-camera answers (``cameras``) and turns them into one advisory record per episode. The
record never votes: ``passed`` and ``score`` stay None, the three items live in ``details``
and are always present - ``unknown`` when the model did not answer, when the review failed,
or when task_success produced nothing at all.
"""
from __future__ import annotations

import json
from collections import Counter

from ...adapters.video_vlm import (CAMERA_CHECK_ITEMS, CAMERA_CHECK_LEVELS,
                                   CAMERA_CHECK_PROTOCOL)
from ...pipeline.records import legacy_verdict

MODULE_ID = "camera_defects"
HOST = "task_success"
UNKNOWN = "unknown"
_RANK = {level: i for i, level in enumerate(CAMERA_CHECK_LEVELS)}   # none < minor < severe

NAME_ZH = {"glitch": "花屏", "shake": "抖动", "contamination": "镜头污染"}
LEVEL_ZH = {"none": "无", "minor": "轻微", "severe": "严重", UNKNOWN: "未知"}


def struct_from_task(task_struct: dict | None) -> dict:
    """v1-shaped ``{passed, score, detail}`` for one episode, from task_success's struct."""
    detail = _parse_detail(task_struct)
    # the per-camera answers of the one judgement request (D71: ``cameras``, one entry per
    # supplied camera, each with its ``camera_check``)
    reviews = detail.get("cameras") if isinstance(detail.get("cameras"), dict) else None
    cams = list(detail.get("cams") or (sorted(reviews) if reviews else []))
    per_camera: dict[str, dict] = {}
    for cam in cams:
        review = (reviews or {}).get(cam)
        entry: dict = {"answered": False, "error": None, "problems": []}
        if not isinstance(review, dict):
            entry["error"] = "这路相机没有画面缺陷结果"
        elif review.get("error"):
            entry["error"] = str(review["error"])[:300]
        check = review.get("camera_check") if isinstance(review, dict) else None
        if isinstance(check, dict):
            entry["answered"] = True
            entry["problems"] = list(check.get("problems") or [])
        for item in CAMERA_CHECK_ITEMS:
            got = check.get(item) if isinstance(check, dict) else None
            got = got if isinstance(got, dict) else {}
            level = str(got.get("level", UNKNOWN))
            entry[item] = {"level": level if level in _RANK or level == UNKNOWN else UNKNOWN,
                           "times": [list(t) for t in (got.get("times") or []) if isinstance(t, (list, tuple))],
                           "note": str(got.get("note") or "")}
            if item == "contamination":
                entry[item]["kind"] = str(got.get("kind") or "none")
        per_camera[cam] = entry

    # One status per item: the worst level over the cameras that answered, unknown when none
    # did. The per-camera levels, time spans and kinds live in ``per_camera`` only - the report
    # and the console read them there, so nothing is said twice.
    items: dict[str, str] = {}
    known = clean = 0
    for item in CAMERA_CHECK_ITEMS:
        seen = [e[item]["level"] for e in per_camera.values() if e[item]["level"] in _RANK]
        known += len(seen)
        clean += sum(1 for lv in seen if lv == "none")
        items[item] = max(seen, key=_RANK.__getitem__) if seen else UNKNOWN

    if task_struct is None:
        reason = "任务成败判定没有产生结果，无法读取逐机位复核"
    elif detail.get("skipped") == "no_task_text":
        reason = "没有任务标注，没有发判定请求"                       # D72
    elif reviews is None:
        reason = "判定请求没有按相机作答（非视频模式或请求失败）"
    elif per_camera and not any(e["answered"] for e in per_camera.values()):
        reason = "模型未回答 camera_check"
    else:
        reason = ""
    out = {"protocol": CAMERA_CHECK_PROTOCOL, "source": f"{HOST}.cameras", "cams": cams,
           "known": known, "clean_ratio": (clean / known) if known else None, "reason": reason,
           "items": items, "per_camera": per_camera}
    return {"passed": None, "score": None, "detail": json.dumps(out, ensure_ascii=False, default=str)}


def _parse_detail(task_struct: dict | None) -> dict:
    if not task_struct:
        return {}
    detail = task_struct.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return {}
    return detail if isinstance(detail, dict) else {}


# ---------------------------------------------------------------- report

def summary(results: dict[int, dict]) -> dict:
    """The report section's statistics over this module's records (``episode_index -> record``)."""
    by_item = {item: Counter() for item in CAMERA_CHECK_ITEMS}
    severe = minor_or_worse = cameras = unanswered = 0
    ratios = []
    for rec in results.values():
        d = rec.get("details") or {}
        worst = 0
        for item in CAMERA_CHECK_ITEMS:
            status = str((d.get("items") or {}).get(item) or UNKNOWN)
            by_item[item][status] += 1
            worst = max(worst, _RANK.get(status, 0))
        severe += worst >= _RANK["severe"]
        minor_or_worse += worst >= _RANK["minor"]
        for e in (d.get("per_camera") or {}).values():
            cameras += 1
            unanswered += not e.get("answered")
        if d.get("clean_ratio") is not None:
            ratios.append(float(d["clean_ratio"]))
    # one flat dict of counts per item: the console charts each of them, no view of its own
    out = {item: {lv: c.get(lv, 0) for lv in (*CAMERA_CHECK_LEVELS, UNKNOWN)}
           for item, c in by_item.items()}
    out.update(episodes_with_severe=severe, episodes_with_minor_or_worse=minor_or_worse,
               cameras=cameras, cameras_unanswered=unanswered,
               clean_ratio_mean=(sum(ratios) / len(ratios)) if ratios else None)
    return out


def table_rows(results: dict[int, dict]) -> list[dict]:
    """One row per episode and camera for the ``camera_defects`` table."""
    rows = []
    for ep in sorted(results):
        rec = results[ep]
        d = rec.get("details") or {}
        per_camera = d.get("per_camera") or {}
        if not per_camera:
            rows.append({"episode_index": ep, "verdict": legacy_verdict(rec), "camera": "", "answered": False,
                         **{item: UNKNOWN for item in CAMERA_CHECK_ITEMS}, "contamination_kind": "none",
                         **{f"{item}_times": "[]" for item in CAMERA_CHECK_ITEMS}, "note": d.get("reason") or ""})
            continue
        for cam in sorted(per_camera):
            e = per_camera[cam]
            rows.append({"episode_index": ep, "verdict": legacy_verdict(rec), "camera": cam,
                         "answered": bool(e.get("answered")),
                         **{item: (e.get(item) or {}).get("level", UNKNOWN) for item in CAMERA_CHECK_ITEMS},
                         "contamination_kind": (e.get("contamination") or {}).get("kind", "none"),
                         **{f"{item}_times": json.dumps((e.get(item) or {}).get("times") or [])
                            for item in CAMERA_CHECK_ITEMS},
                         "note": " / ".join(n for n in ((e.get(item) or {}).get("note") for item in CAMERA_CHECK_ITEMS) if n)
                                 or (e.get("error") or "")})
    return rows
