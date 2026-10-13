"""camera_defects: glitch, shake, lens contamination, occlusion and the camera's own identity,
reported by the model in the per-camera answers it already gives for task_success (no request of
its own).

The one judgement request asks the model for a ``camera_check`` per camera next to its verdict
(``adapters.video_vlm.CAMERAS_PROMPT``; an episode without a task text gets the same question
alone, ``CAMERAS_ONLY_PROMPT``, D73); this module reads those answers back out of
task_success's per-camera answers (``cameras``) and turns them into one advisory record per episode. The
record never votes: ``passed`` and ``score`` stay None, the four defect items and the identity
live in ``details`` and are always present - ``unknown`` when the model did not answer, when the
review failed, or when task_success produced nothing at all.
"""
from __future__ import annotations

import json
from collections import Counter

from ...adapters.video_vlm import (CAMERA_CHECK_ITEMS, CAMERA_CHECK_LEVELS,
                                   CAMERA_CHECK_PROTOCOL, VIEW_FIELDS, empty_view)
from ...pipeline.records import legacy_verdict

MODULE_ID = "camera_defects"
HOST = "task_success"
UNKNOWN = "unknown"
_RANK = {level: i for i, level in enumerate(CAMERA_CHECK_LEVELS)}   # none < minor < severe

NAME_ZH = {"glitch": "花屏", "shake": "抖动", "contamination": "镜头污染", "occlusion": "视觉遮挡"}
LEVEL_ZH = {"none": "无", "minor": "轻微", "severe": "严重", UNKNOWN: "未知"}
#: the episode's camera identity (MV-2): every answered camera matches its name, or one does not
IDENTITY = ("consistent", "inconsistent", UNKNOWN)
IDENTITY_ZH = {"consistent": "一致", "inconsistent": "不符", UNKNOWN: "未知"}
#: where in the picture the occluding thing sits, for the report and the console
REGION_ZH = {"left": "画面左侧", "right": "画面右侧", "top": "画面上方", "bottom": "画面下方",
             "center": "画面中央", "whole": "整幅画面"}


def struct_from_task(task_struct: dict | None, *, undecodable: bool = False,
                     arms: dict | None = None) -> dict:
    """v1-shaped ``{passed, score, detail}`` for one episode, from task_success's struct.

    ``undecodable``: the episode's video is corrupt, so the request was never sent (D93) - every
    item is unassessable for that. ``arms``: which arm each camera measurably rides
    (:func:`curation.pipeline.arm_pairing.pair`), the half of MV-2 that is measured on the CPU
    rather than asked of the model; None when the dataset gives nothing to measure against.
    """
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
            if got.get("whole"):
                entry[item]["whole"] = True
            if item == "contamination":
                entry[item]["kind"] = str(got.get("kind") or "none")
            if item == "occlusion":
                entry[item]["source"] = str(got.get("source") or "none")
                entry[item]["region"] = str(got.get("region") or UNKNOWN)
                for key in ("coverage", "duration_s", "share"):      # the share of the picture, the
                    value = got.get(key)                             # seconds, the share of the clip
                    entry[item][key] = value if isinstance(value, (int, float)) else None
        entry["view"] = _view_of(check, cam, cams)
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

    if undecodable:
        reason = "视频解不开（文件损坏），判定请求没有发出"             # D93 (design doc 28 §2.3)
    elif task_struct is None:
        reason = "任务成败判定没有产生结果，无法读取逐机位复核"
    elif reviews is None and detail.get("skipped") == "no_task_text":
        reason = "没有任务标注，画面缺陷请求没有得到结果"             # D73: it gets that request alone
    elif reviews is None:
        reason = "判定请求没有按相机作答（非视频模式或请求失败）"
    elif per_camera and not any(e["answered"] for e in per_camera.values()):
        reason = "模型未回答 camera_check"
    else:
        reason = ""
    arms = _arms_of(arms)
    out = {"protocol": CAMERA_CHECK_PROTOCOL, "source": f"{HOST}.cameras", "cams": cams,
           "known": known, "clean_ratio": (clean / known) if known else None, "reason": reason,
           "items": items, "identity": identity_of(per_camera, arms), "arms": arms,
           "per_camera": per_camera}
    if undecodable:
        out["undecodable"] = True
    return {"passed": None, "score": None, "detail": json.dumps(out, ensure_ascii=False, default=str)}


def _view_of(check, cam: str, cams: list[str]) -> dict:
    """This camera's ``view`` as the model gave it, ``same_as`` settled against the supplied
    cameras: it must name another one of them (an exact name or a unique dotted suffix), else
    it reads empty and the slip is kept in the entry's ``problems`` by the caller's record."""
    raw = check.get("view") if isinstance(check, dict) else None
    view = {k: str((raw or {}).get(k) or empty_view()[k]) for k in VIEW_FIELDS} if isinstance(raw, dict) else empty_view()
    same = view.get("same_as") or ""
    if same:
        matches = [c for c in cams if c != cam and (c == same or c.endswith("." + same))]
        view["same_as"] = matches[0] if len(matches) == 1 else ""
    return view


def _arms_of(raw: dict | None) -> dict:
    """The episode's measured arm pairing, kept to the fields the report and the console read.

    The measurement is the module's own (``basis``, with its numbers in ``readings``), not an
    answer from the model, and an episode where nothing could be measured reads ``unknown`` here.
    """
    out = {"identity": UNKNOWN, "crossed": [], "note": "", "basis": "", "reason": "", "readings": {}}
    if not isinstance(raw, dict):
        return out
    identity = str(raw.get("identity") or UNKNOWN)
    out["identity"] = identity if identity in ("consistent", "crossed") else UNKNOWN
    out["crossed"] = [str(c) for c in (raw.get("crossed") or []) if isinstance(c, str)]
    out["note"] = str(raw.get("note") or "")[:300]
    out["basis"] = str(raw.get("basis") or "")
    out["reason"] = str(raw.get("reason") or "")
    out["readings"] = raw.get("readings") if isinstance(raw.get("readings"), dict) else {}
    return out


def identity_of(per_camera: dict[str, dict], arms: dict | None = None) -> str:
    """The episode's camera identity (MV-2): ``inconsistent`` as soon as one camera plainly
    contradicts the mount the dataset declares for it, duplicates another, or measurably rides the
    other arm (``arms``, which no single camera's picture can give); ``consistent`` when something
    was checked and nothing objected, ``unknown`` when nothing was said."""
    arms = arms or {}
    views = [e.get("view") or {} for e in per_camera.values() if e.get("answered")]
    if arms.get("identity") == "crossed" or any(v.get("matches_name") == "no" or v.get("same_as") for v in views):
        return "inconsistent"
    if arms.get("identity") == "consistent" or any(v.get("matches_name") == "yes" for v in views):
        return "consistent"
    return UNKNOWN


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
    identity: Counter = Counter()
    severe = minor_or_worse = cameras = unanswered = 0
    seconds = coverage = 0.0
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
        status = str(d.get("identity") or UNKNOWN)
        identity[status if status in IDENTITY else UNKNOWN] += 1
        for e in (d.get("per_camera") or {}).values():
            cameras += 1
            unanswered += not e.get("answered")
            occl = e.get("occlusion") or {}
            if occl.get("level") in ("minor", "severe"):      # how much and how long, over the set
                if isinstance(occl.get("duration_s"), (int, float)):
                    seconds += float(occl["duration_s"])
                if isinstance(occl.get("coverage"), (int, float)):
                    coverage = max(coverage, float(occl["coverage"]))
        if d.get("clean_ratio") is not None:
            ratios.append(float(d["clean_ratio"]))
    # one flat dict of counts per item: the console charts each of them, no view of its own
    out = {item: {lv: c.get(lv, 0) for lv in (*CAMERA_CHECK_LEVELS, UNKNOWN)}
           for item, c in by_item.items()}
    out["identity"] = {k: identity.get(k, 0) for k in IDENTITY}
    out.update(episodes_with_severe=severe, episodes_with_minor_or_worse=minor_or_worse,
               cameras=cameras, cameras_unanswered=unanswered,
               occlusion_seconds_total=round(seconds, 1), occlusion_coverage_max=round(coverage, 3),
               clean_ratio_mean=(sum(ratios) / len(ratios)) if ratios else None)
    return out


def table_rows(results: dict[int, dict]) -> list[dict]:
    """One row per episode and camera for the ``camera_defects`` table."""
    rows = []
    for ep in sorted(results):
        rec = results[ep]
        d = rec.get("details") or {}
        per_camera = d.get("per_camera") or {}
        identity = str(d.get("identity") or UNKNOWN)
        if not per_camera:
            rows.append({"episode_index": ep, "verdict": legacy_verdict(rec), "camera": "", "answered": False,
                         **{item: UNKNOWN for item in CAMERA_CHECK_ITEMS}, "contamination_kind": "none",
                         "occlusion_source": "none", "occlusion_region": "", "occlusion_coverage": None,
                         "occlusion_duration_s": None,
                         **{f"{item}_times": "[]" for item in CAMERA_CHECK_ITEMS},
                         "identity": identity, **{f"view_{k}": "" for k in VIEW_FIELDS if k != "note"},
                         "note": d.get("reason") or ""})
            continue
        for cam in sorted(per_camera):
            e = per_camera[cam]
            view = e.get("view") or {}
            rows.append({"episode_index": ep, "verdict": legacy_verdict(rec), "camera": cam,
                         "answered": bool(e.get("answered")),
                         **{item: (e.get(item) or {}).get("level", UNKNOWN) for item in CAMERA_CHECK_ITEMS},
                         "contamination_kind": (e.get("contamination") or {}).get("kind", "none"),
                         "occlusion_source": (e.get("occlusion") or {}).get("source", "none"),
                         "occlusion_region": (e.get("occlusion") or {}).get("region", ""),
                         "occlusion_coverage": (e.get("occlusion") or {}).get("coverage"),
                         "occlusion_duration_s": (e.get("occlusion") or {}).get("duration_s"),
                         **{f"{item}_times": json.dumps((e.get(item) or {}).get("times") or [])
                            for item in CAMERA_CHECK_ITEMS},
                         "identity": identity,
                         **{f"view_{k}": str(view.get(k) or "") for k in VIEW_FIELDS if k != "note"},
                         "note": " / ".join(n for n in ((e.get(item) or {}).get("note") for item in CAMERA_CHECK_ITEMS)
                                            if n) or (view.get("note") or "") or (e.get("error") or "")})
    return rows
