"""autolabel as a v2 stage (design doc 02 §3.4, 06 §1).

It reuses v1's code path verbatim; what differs is where the inputs come from
(files of the run directory instead of variables of ``run_pipeline``) and the
per-episode incident recording (D33): v1's pre-funnel caption for episodes
without a task text (``run.py``: ``caption_episodes`` over the unlabeled meta
rows with ``make_vlm_captioner``). One line per episode in
``autolabel/captions.jsonl``: ``ok`` / ``unclear`` (the model's honest "cannot
tell", v1 turns it into an empty caption) / ``error`` (the call failed for good
or a camera did not decode).

Exact dedup used to live here as a whole-set pass; since D70 it is a streaming
segment like the other checks (``check_stage.StageRun._dedup``).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time
from collections.abc import Callable

from .incidents import (IncidentLog, camera_names, decode_watch, installed_decode_watch,
                        wrap_call)
from .records import AUTOLABEL_FILE, AppendLog, read_jsonl, write_text_atomic
from .rows import index_of

SRC_CAPTION_TAG = "自产caption"


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
