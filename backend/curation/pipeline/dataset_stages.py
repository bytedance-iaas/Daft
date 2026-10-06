"""autolabel and dedup as v2 stages (design doc 02 §3.4-3.5, 06 §1).

All three reuse v1's code path verbatim; what differs is where the inputs come
from (files of the run directory instead of variables of ``run_pipeline``) and
the per-episode incident recording (D33):

* **autolabel** - v1's pre-funnel caption for episodes without a task text
  (``run.py``: ``caption_episodes`` over the unlabeled meta rows with
  ``make_vlm_captioner``). One line per episode in ``autolabel/captions.jsonl``:
  ``ok`` / ``unclear`` (the model's honest "cannot tell", v1 turns it into an empty
  caption) / ``error`` (the call failed for good or a camera did not decode).
* **dedup** - v1's two-pass exact dedup over the kept set in ascending episode
  order (action hash on everything, video fingerprint only inside a collision
  group; never concurrent, the traversal order decides who is the duplicate).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import threading
import time
from collections.abc import Callable

from .check_stage import input_digest
from .incidents import (IncidentLog, camera_names, decode_watch, installed_decode_watch,
                        wrap_call)
from .records import (AUTOLABEL_FILE, AppendLog, PartWriter, check_counts, compact, latest_results,
                      module_dir, read_jsonl, record_from_struct, write_json_atomic,
                      write_text_atomic)
from .rows import index_of, meta_rows

SRC_CAPTION_TAG = "自产caption"


def _eid(i: int) -> str:
    return f"ep{int(i):06d}"


def _drive(ctx, items: list, work: Callable, on_done: Callable, concurrency: int,
           label: str, total: int, done0: int = 0) -> int:
    """Run ``work(item)`` on a pool; ``on_done(item, result)`` on the main thread.

    SIGTERM stops new work and drains what is in flight; returns how many finished.
    """
    done = done0
    queue = list(items)
    pending: dict[cf.Future, object] = {}
    pool = cf.ThreadPoolExecutor(max_workers=max(1, int(concurrency)),
                                 thread_name_prefix=label.replace(":", "-"))
    started = time.monotonic()
    try:
        while queue or pending:
            while queue and len(pending) < max(1, int(concurrency)) and not ctx.stop_requested:
                item = queue.pop(0)
                pending[pool.submit(work, item)] = item
            if not pending:
                break
            finished, _ = cf.wait(list(pending), timeout=0.5, return_when=cf.FIRST_COMPLETED)
            for fut in finished:
                item = pending.pop(fut)
                on_done(item, fut.result())
                done += 1
                rate = (time.monotonic() - started) / max(1, done - done0)
                ctx.progress(label, done, total, eta_s=rate * (total - done))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return done


# ---------------------------------------------------------------- autolabel

def load_autolabel_lines(run_dir: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for line in read_jsonl(os.path.join(run_dir, AUTOLABEL_FILE)):
        out[int(line["episode_index"])] = line
    return out


def autolabel_line(ep: int, caption: str, incidents: list[dict]) -> dict:
    if incidents:
        return {"episode_index": int(ep), "status": "error", "caption": None,
                "source": SRC_CAPTION_TAG,
                "error": {"kind": "execution", "incidents": incidents}}
    if caption:
        return {"episode_index": int(ep), "status": "ok", "caption": caption,
                "source": SRC_CAPTION_TAG, "error": None}
    return {"episode_index": int(ep), "status": "unclear", "caption": None,
            "source": SRC_CAPTION_TAG, "error": None}


def caption_one(row: dict, captioner: Callable, n_frames: int) -> tuple[str, list[dict]]:
    """v1's ``caption_episodes`` for one meta row, with its incidents (D33)."""
    from ..dataset_level.caption import caption_episodes

    log = IncidentLog()
    wrapped = wrap_call(captioner, log, step="caption", call_kind="caption")
    with decode_watch(log, camera_names(row.get("video"))):
        caption = caption_episodes([row], wrapped, n_frames=n_frames, max_concurrency=1)[0]
    return str(caption or ""), log.items()


def run_autolabel(ctx, run_dir: str, rows: list[dict], captioner: Callable, *,
                  n_frames: int, concurrency: int, resume: bool) -> dict:
    """Caption the unlabeled ``rows`` (meta rows); returns ``autolabel --json``."""
    unlabeled = [r for r in rows if not (r.get("instruction") or "").strip()]
    wanted = [index_of(r["episode_id"]) for r in unlabeled]
    existing = load_autolabel_lines(run_dir) if resume else {}
    video_mode = getattr(captioner, "media_input", None) == "video"
    from ..dataset_level.caption import VIDEO_CAPTION_PROTOCOL
    if video_mode:
        existing = {i: line for i, line in existing.items()
                    if line.get("media_protocol") == VIDEO_CAPTION_PROTOCOL}
    todo = [r for r in unlabeled
            if not (resume and index_of(r["episode_id"]) in existing
                    and existing[index_of(r["episode_id"])]["status"] != "error")]
    if len(todo) < len(unlabeled):
        ctx.log("info", f"--resume: {len(unlabeled) - len(todo)} episode(s) already captioned")
    path = os.path.join(run_dir, AUTOLABEL_FILE)
    log = AppendLog(path)

    def work(row):
        return caption_one(row, captioner, n_frames)

    def on_done(row, result):
        caption, incidents = result
        line = autolabel_line(index_of(row["episode_id"]), caption, incidents)
        if video_mode:
            line["media_protocol"] = VIDEO_CAPTION_PROTOCOL
        log.write(line)

    try:
        with installed_decode_watch():
            _drive(ctx, todo, work, on_done, concurrency, "autolabel", len(unlabeled),
                   len(unlabeled) - len(todo))
    finally:
        log.close()
        lines = load_autolabel_lines(run_dir)
        write_text_atomic(path, "".join(json.dumps(lines[i], ensure_ascii=False,
                                                   sort_keys=True) + "\n"
                                        for i in sorted(lines)))
    ctx.check_stop("autolabel")
    lines = load_autolabel_lines(run_dir)
    counts = {"total": len(wanted), "ok": 0, "unclear": 0, "error": 0}
    errors = []
    for e in wanted:
        status = (lines.get(e) or {}).get("status", "error")
        counts[status] += 1
        if status == "error":
            errors.append(e)
    return {"schema_version": "1.0", "counts": counts, "error_episodes": sorted(errors),
            "captions_file": AUTOLABEL_FILE}


# ---------------------------------------------------------------- dedup

def run_dedup(ctx, run_dir: str, input_dir: str, episodes: list[int], part: str, *,
              embodiment_id: str | None = None) -> dict:
    """v1's two-pass exact dedup (``run.py``) on ``episodes``; returns ``check --json``."""
    from ..dataset_level.dedup import action_hash, episode_fingerprint
    # v1 reads dedup's chunks with the reader of the input's format (run.py: read_rows,
    # bound to mcap's topic mapping for mcap)
    from .rows import read_rows

    keep_ids = [_eid(e) for e in sorted(set(episodes))]
    order: list[str] = []
    first: dict[str, str] = {}
    collide: dict[str, list[str]] = {}
    hashes: dict[str, str] = {}
    unread: dict[str, str] = {}
    label = "check:dedup"
    for i0 in range(0, len(keep_ids), 200):
        ctx.check_stop("dedup")
        chunk = {int(e[2:]) for e in keep_ids[i0:i0 + 200]}
        try:
            rows = read_rows(input_dir, episode_indices=chunk, embodiment_id=embodiment_id,
                             validate=False, skip_missing=True)
        except Exception as e:  # noqa: BLE001 - this chunk cannot be read
            for idx in chunk:
                unread[_eid(idx)] = f"{type(e).__name__}: {e}"
            continue
        for row in rows:
            ah = action_hash(row)
            eid = row["episode_id"]
            order.append(eid)
            hashes[eid] = str(ah)
            if ah in first:
                collide.setdefault(ah, [first[ah]]).append(eid)
            else:
                first[ah] = eid
        ctx.progress(label, min(len(keep_ids), i0 + 200), len(keep_ids))
        for idx in chunk:
            if _eid(idx) not in hashes and _eid(idx) not in unread:
                unread[_eid(idx)] = "missing source file(s)"
    dropped: list[dict] = []
    dup_of: dict[str, str] = {}
    fingerprints: dict[str, str] = {}
    for ah, eps in collide.items():
        idxs = {int(e[2:]) for e in eps}
        seen: dict[str, str] = {}
        try:
            rows = read_rows(input_dir, episode_indices=idxs, embodiment_id=embodiment_id,
                             validate=False, skip_missing=True)
        except Exception as e:  # noqa: BLE001
            for eid in eps:
                unread[eid] = f"{type(e).__name__}: {e}"
            continue
        for row in rows:
            fp = episode_fingerprint(row)
            fingerprints[row["episode_id"]] = str(fp)
            if fp in seen:
                dropped.append({"episode_id": row["episode_id"], "duplicate_of": seen[fp],
                                "fingerprint": fp[:16]})
                dup_of[row["episode_id"]] = seen[fp]
            else:
                seen[fp] = row["episode_id"]
    groups = {"order": [index_of(e) for e in order],
              "action_collisions": sorted(sorted(index_of(e) for e in g)
                                          for g in collide.values()),
              "fingerprints": {str(index_of(e)): fp for e, fp in sorted(fingerprints.items())},
              "dropped": sorted(({"episode_index": index_of(d["episode_id"]),
                                  "duplicate_of": index_of(d["duplicate_of"])}
                                 for d in dropped), key=lambda d: d["episode_index"])}
    write_json_atomic(os.path.join(module_dir(run_dir, "dedup"), "groups.json"), groups)
    # the reader left out an episode whose source files are missing: so does v2 (D40)
    missing = leave_out_missing_source(ctx, run_dir, input_dir,
                              [index_of(e) for e, why in unread.items()
                               if why == "missing source file(s)"])
    writer = PartWriter(run_dir, ["dedup"], part)
    try:
        for eid in keep_ids:
            ep = index_of(eid)
            if ep in missing:
                continue
            if eid in unread:
                log = IncidentLog()
                log.add("read", cause=unread[eid])
                rec = record_from_struct("dedup", ep, None, incidents=log.items())
            elif eid in dup_of:
                rec = record_from_struct("dedup", ep, {
                    "passed": False, "score": None,
                    "detail": json.dumps({"duplicate_of": index_of(dup_of[eid]),
                                          "reason": f"与 {dup_of[eid]} 字节级完全重复"},
                                         ensure_ascii=False)})
            else:
                rec = record_from_struct("dedup", ep, {"passed": True, "score": None,
                                                       "detail": "{}"})
            writer.write(rec)
    finally:
        writer.close()
    compact(run_dir, "dedup")
    ctx.progress(label, len(keep_ids), len(keep_ids))
    return summary(run_dir, ["dedup"], [e for e in episodes if e not in missing], part,
                   missing=missing)


def leave_out_missing_source(ctx, run_dir: str, input_dir: str, episodes) -> dict[int, list[str]]:
    """Of ``episodes`` the reader did not return, the ones missing source files (D40):
    recorded for the run directory, left out of this call."""
    from .skipped import missing_source, record

    found = missing_source(input_dir, episodes) if episodes else {}
    if found:
        record(run_dir, found)
        ctx.log("warn", f"{len(found)} episode(s) have missing source files and are left "
                        f"out like v1 does: {sorted(found)[:10]}")
    return found


def summary(run_dir: str, modules: list[str], episodes: list[int], part: str,
            skipped: int | None = None, missing: dict[int, list[str]] | None = None) -> dict:
    from .skipped import as_list

    out = {}
    version = "1.0"
    for m in modules:
        version, counts, errors, found = check_counts(latest_results(run_dir, m), episodes)
        entry = {"part": part, "input_digest": input_digest(episodes), "episodes": counts,
                 "error_episodes": errors}
        if version != "1.0":
            entry["findings"] = found
        if skipped is not None:
            entry["skipped_existing"] = skipped
        if missing:
            entry["skipped_missing_source"] = as_list(missing)
        out[m] = entry
    return {"schema_version": version, "modules": out}
