"""One episode across every module of a revision: the report's drill-down (design doc 03 §6).

C4 ``EpisodeView`` - v1's trajectory page: which final list the episode is in and
why (``reasons`` from the list entry), what a person is asked about it (``review``),
the task text it was judged and delivered with, every module's record as the
revision saw it (C2 ``result-record``), the evidence frames the records point at
(paths in the run directory: sign with ``scope=delivery``) and the camera videos
(:mod:`.videos`).

``review`` items are written the way ``reasons`` are - ``{module, kind, text,
priority}`` - with the questions a person can answer on the adjudication page first
(an abstention of another module is shown, v1 does not put it in the queue).
"""
from __future__ import annotations

from curation.contracts import modules as registry

from ..errors import ApiError
from .files import cached_json, json_safe
from .revision import Revision
from .videos import SOURCE_MANIFEST, episode_videos

#: the review.json kinds a person answers on the adjudication page (C1 review lines); a task
#: verdict only where task_success abstained
_ADJUDICABLE = tuple(ln.review_kind for ln in registry.REVIEW_LINES if ln.review_kind != "task_verdict")


def _review_items(entry: dict | None) -> list[dict]:
    items = []
    for it in (entry or {}).get("review") or []:
        if not isinstance(it, dict):
            continue
        item = {"module": it.get("source_module"), "kind": it.get("kind"),
                "text": it.get("reason") or ""}
        if it.get("priority"):
            item["priority"] = it["priority"]
        items.append(item)

    def asked(item: dict) -> bool:
        return item["kind"] in _ADJUDICABLE or (item["kind"] == "task_verdict"
                                                and item["module"] == "task_success")
    return [i for i in items if asked(i)] + [i for i in items if not asked(i)]


def _evidence_kind(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("jpg", "jpeg", "png", "webp"):
        return "frame"
    if ext in ("mp4", "webm"):
        return "video"
    return "file"


def _task_text(entry: dict, records: dict[str, dict]) -> dict | None:
    tt = entry.get("task_text")
    if isinstance(tt, dict) and isinstance(tt.get("text"), str):
        return {"text": tt["text"], "source": str(tt.get("source") or "")}
    details = (records.get("task_success") or {}).get("details") or {}
    if isinstance(details.get("task_desc"), str) and details["task_desc"]:
        return {"text": details["task_desc"], "source": str(details.get("task_desc_source") or "")}
    return None


def skipped_episodes(rev: Revision) -> dict[int, list[str]]:
    """Episodes left out for missing source files (D40): the report's
    ``integrity.skipped_episodes``, else the task's source manifest's list."""
    items = (rev.report().get("integrity") or {}).get("skipped_episodes")
    if not isinstance(items, list):
        try:
            manifest = cached_json(rev.store.docs, rev.run_dir / SOURCE_MANIFEST)
        except (FileNotFoundError, ValueError):
            manifest = {}
        items = (manifest or {}).get("skipped_episodes") or []
    out: dict[int, list[str]] = {}
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and isinstance(it.get("episode_index"), int):
            out[int(it["episode_index"])] = [str(k) for k in it.get("missing") or []]
    return out


def episode_view(rev: Revision, episode: int) -> dict:
    hit = rev.entries().get(int(episode))
    if hit is None:
        name = f"ep{int(episode):06d}"
        missing = skipped_episodes(rev).get(int(episode))
        if missing is not None:
            shown = "、".join(missing[:3]) + ("等" if len(missing) > 3 else "")
            raise ApiError("not_found", f"{name} 没有参与质检：它的源文件缺失（{shown}），照 v1 的做法剔除，"
                                        f"不在任何清单里；补齐文件后另建任务",
                           details={"episode_index": int(episode), "revision": rev.number,
                                    "reason": "source_missing", "missing": missing})
        raise ApiError("not_found", f"结果版本 r{rev.number} 里没有 {name}（不在这个任务的质检范围里）",
                       details={"episode_index": int(episode), "revision": rev.number})
    list_name, entry = hit
    records: dict[str, dict] = {}
    for module in rev.modules():
        rec = rev.record(module, episode)
        if rec is not None:
            records[module] = json_safe(rec)
    evidence = []
    for module, rec in records.items():
        for path in rec.get("evidence") or []:
            if isinstance(path, str) and path:
                evidence.append({"module": module, "path": path, "kind": _evidence_kind(path)})
    view = {"episode_index": int(episode), "revision": rev.number, "list": list_name,
            "reasons": json_safe([r for r in entry.get("reasons") or [] if isinstance(r, dict)]),
            "review": json_safe(_review_items(rev.review().get(int(episode)))),
            "modules": records, "evidence": evidence,
            "videos": episode_videos(rev, int(episode))}
    tt = _task_text(entry, records)
    if tt is not None:
        view["task_text"] = tt
    return view
