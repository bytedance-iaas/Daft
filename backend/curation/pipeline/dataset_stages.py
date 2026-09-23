"""autolabel, dedup and skill_profile as v2 stages (design doc 02 §3.4-3.5, 06 §1).

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
* **skill_profile** - ``run._skill_profile_stage`` on the kept-minus-duplicates set,
  reusing autolabel's captions like v1. ``--incremental`` keeps the taxonomy and
  only re-files what changed (v1's ``_sync_profile`` / ``reprofile``): episodes
  that left are removed, relabelled ones are re-filed under the human label, new
  ones are captioned and filed; nothing is re-induced.
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
from .records import (AUTOLABEL_FILE, AppendLog, PartWriter, compact, latest_results,
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
        log.write(autolabel_line(index_of(row["episode_id"]), caption, incidents))

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
    for m in modules:
        cur = latest_results(run_dir, m)
        counts = {"total": len(episodes), "pass": 0, "fail": 0, "abstain": 0, "scored": 0,
                  "error": 0}
        errors = []
        for e in episodes:
            rec = cur.get(e)
            verdict = rec["verdict"] if rec else "error"
            counts[verdict] += 1
            if verdict == "error":
                errors.append(e)
        entry = {"part": part, "input_digest": input_digest(episodes), "episodes": counts,
                 "error_episodes": errors}
        if skipped is not None:
            entry["skipped_existing"] = skipped
        if missing:
            entry["skipped_missing_source"] = as_list(missing)
        out[m] = entry
    return {"schema_version": "1.0", "modules": out}


# ---------------------------------------------------------------- skill_profile

PROFILE_DIR = "skill_profile"


def _profile_path(run_dir: str, name: str) -> str:
    return os.path.join(module_dir(run_dir, PROFILE_DIR), name)


class _PerEpisodeCaptions:
    """A ``caption_episodes`` drop-in that captions one row at a time on a pool, in
    order, charging each failure to its episode (D33)."""

    def __init__(self, logs: dict[str, IncidentLog], lock: threading.Lock):
        self.logs, self.lock = logs, lock

    def __call__(self, rows, captioner, n_frames=8, max_side=448, precomputed=None,
                 on_progress=None, max_concurrency=1, max_cams=4):
        from ..dataset_level.caption import caption_episodes

        def one(row):
            eid = row.get("episode_id")
            with self.lock:
                log = self.logs.setdefault(eid, IncidentLog())
            try:
                if precomputed and eid in precomputed:
                    return caption_episodes([row], captioner, n_frames=n_frames,
                                            max_side=max_side, precomputed=precomputed,
                                            max_concurrency=1, max_cams=max_cams)[0]
                wrapped = wrap_call(captioner, log, step="caption", call_kind="caption")
                with decode_watch(log, camera_names(row.get("video"))):
                    return caption_episodes([row], wrapped, n_frames=n_frames,
                                            max_side=max_side, max_concurrency=1,
                                            max_cams=max_cams)[0]
            finally:
                if on_progress is not None:
                    on_progress()

        rows = list(rows)
        if max_concurrency <= 1 or len(rows) <= 1:
            return [one(r) for r in rows]
        with cf.ThreadPoolExecutor(max_workers=min(int(max_concurrency), len(rows))) as ex:
            return list(ex.map(one, rows))                    # keeps the order


def _write_profile_outputs(run_dir: str, profile: dict, caption_of: dict, gtext_of: dict,
                           gsrc_of: dict, label_audit: dict | None) -> None:
    from ..dataset_level.profile import skill_assignment_rows

    write_json_atomic(_profile_path(run_dir, "profile.json"), profile)
    rows = skill_assignment_rows(profile, caption_of, gtext_of, gsrc_of)
    write_text_atomic(_profile_path(run_dir, "assignments.jsonl"), "".join(
        json.dumps({"episode_index": index_of(r["episode_id"]), **r}, ensure_ascii=False,
                   sort_keys=True) + "\n" for r in rows))
    write_text_atomic(_profile_path(run_dir, "captions.jsonl"), "".join(
        json.dumps({"episode_index": index_of(e), "caption": c}, ensure_ascii=False) + "\n"
        for e, c in sorted(caption_of.items())))
    write_json_atomic(_profile_path(run_dir, "label_audit.json"),
                      label_audit if label_audit is not None else {})


def load_profile(run_dir: str) -> dict | None:
    """The skill_profile outputs of a run directory (None when it never ran)."""
    path = _profile_path(run_dir, "profile.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        profile = json.load(fh)
    rows = read_jsonl(_profile_path(run_dir, "assignments.jsonl"))
    audit_path = _profile_path(run_dir, "label_audit.json")
    audit = {}
    if os.path.isfile(audit_path):
        with open(audit_path, encoding="utf-8") as fh:
            audit = json.load(fh) or {}
    captions = {int(r["episode_index"]): str(r.get("caption") or "")
                for r in read_jsonl(_profile_path(run_dir, "captions.jsonl"))}
    return {"profile": profile, "assignments": rows, "label_audit": audit,
            "captions": captions}


def _audit_ids(label_audit: dict | None) -> set[str]:
    out = set()
    for tier in ("high", "mid_for_review", "low_caption_unstable"):
        for e in (label_audit or {}).get(tier) or []:
            out.add(str(e.get("id")))
    return out


def run_skill_profile(ctx, run_dir: str, rows: list[dict], cfg: dict, captioner: Callable,
                      llm_ask: Callable, auto_caps: dict[str, str], part: str, *,
                      incremental: bool = False, relabels: dict[int, str] | None = None,
                      restored: set[int] | frozenset = frozenset()) -> dict:
    """Profile the kept, de-duplicated meta ``rows`` (ascending); returns ``check --json``.

    ``restored``: episodes a person brought into the delivery; ``--incremental``
    files them from their text, never captioning them (v1's ``_sync_profile``).
    """
    from .run import _skill_profile_stage

    episodes = [index_of(r["episode_id"]) for r in rows]
    logs: dict[str, IncidentLog] = {}
    lock = threading.Lock()
    llm_log = IncidentLog()
    ask = wrap_call(llm_ask, llm_log, step="llm", call_kind="llm")
    previous = load_profile(run_dir) if incremental else None
    if incremental and previous is None:
        ctx.log("info", "--incremental: no previous skill profile; profiling in full")
    try:
        with installed_decode_watch():
            if previous is None:
                caption_fn = _PerEpisodeCaptions(logs, lock)
                profile, caption_of, gtext_of, gsrc_of, label_audit = _skill_profile_stage(
                    rows, cfg, captioner, ask, auto_caps, caption_fn=caption_fn)
            else:
                profile, caption_of, gtext_of, gsrc_of, label_audit = _incremental_profile(
                    ctx, rows, cfg, captioner, ask, auto_caps, previous, logs, lock,
                    relabels or {}, restored)
    except Exception as e:
        # a text call the taxonomy needs failed for good: no episode can be filed, the
        # module as a whole failed (exit 4); anything else is a bug and stays one
        if not llm_log:
            raise
        from ..cli.errors import ModuleFailed

        raise ModuleFailed(f"skill_profile: a model call the taxonomy needs failed: "
                           f"{type(e).__name__}: {e}"[:600],
                           {"incidents": llm_log.items()[:5]}) from None
    from ..dataset_level.profile import skill_assignment_rows

    _write_profile_outputs(run_dir, profile, caption_of, gtext_of, gsrc_of, label_audit)
    flagged = _audit_ids(label_audit)
    filed = {r["episode_id"]: r for r in skill_assignment_rows(profile, caption_of,
                                                              gtext_of, gsrc_of)}
    dataset_incidents = llm_log.items()
    writer = PartWriter(run_dir, [PROFILE_DIR], part)
    try:
        for r in rows:
            eid = r["episode_id"]
            incidents = (logs.get(eid).items() if eid in logs else []) + dataset_incidents
            passed = None if eid in flagged else True
            row = filed.get(eid) or {}
            detail = {"family": row.get("family", ""), "subskill": row.get("subskill", ""),
                      "caption": caption_of.get(eid, ""),
                      "grouping_text": gtext_of.get(eid, ""),
                      "grouping_text_source": gsrc_of.get(eid, "")}
            writer.write(record_from_struct(
                PROFILE_DIR, index_of(eid),
                {"passed": passed, "score": None,
                 "detail": json.dumps(detail, ensure_ascii=False)},
                incidents=incidents))
    finally:
        writer.close()
    compact(run_dir, PROFILE_DIR)
    return summary(run_dir, [PROFILE_DIR], episodes, part)


def _incremental_profile(ctx, rows, cfg, captioner, llm_ask, auto_caps, previous, logs,
                         lock, relabels, restored=frozenset()):
    """Re-file what changed on the existing taxonomy (v1's ``_sync_profile``).

    Episodes no longer in ``rows`` leave the profile (discarded, judged failed,
    rejected again after a relabel); relabelled ones are filed again under the
    new label. An episode that joins is filed from its text without a model
    caption when a person brought it in (``restored``) or relabelled it - the
    human label, else the annotation, else the autolabel caption, else it stays
    unassigned, as v1 does; any other newcomer is captioned first.
    """
    from ..dataset_level.reassign import (NO_SUBSKILL, SRC_CAPTION, SRC_LABEL, SRC_NONE,
                                          UNASSIGNED, grouping_text_and_source,
                                          member_map_of, rebuild_profile, reassign_texts,
                                          taxonomy_from_profile)

    old_rows = {str(r["episode_id"]): r for r in previous["assignments"]}
    profile = previous["profile"]
    wanted = {r["episode_id"]: r for r in rows}
    removed = sorted(set(old_rows) - set(wanted))
    added = [r for r in rows if r["episode_id"] not in old_rows]
    assignment: dict = {}
    new_text: dict = {}
    new_src: dict = {}
    for eid, r in old_rows.items():
        if eid not in wanted:
            continue
        assignment[eid] = (str(r.get("family") or UNASSIGNED),
                           str(r.get("subskill") or NO_SUBSKILL))
        new_text[eid] = str(r.get("grouping_text") or r.get("caption") or "")
        new_src[eid] = str(r.get("grouping_text_source") or "")
    to_assign: dict = {}
    for eid in list(assignment):
        lab = str(relabels.get(index_of(eid)) or "").strip()
        if lab and new_text.get(eid) != lab:
            to_assign[eid] = lab
            new_text[eid], new_src[eid] = lab, SRC_LABEL
    caps = previous["captions"]
    by_person = [r for r in added if index_of(r["episode_id"]) in restored
                 or str(relabels.get(index_of(r["episode_id"])) or "").strip()]
    newcomers = [r for r in added if r not in by_person]
    for r in by_person:                     # v1's _sync_profile: no model call
        eid = r["episode_id"]
        lab = (str(relabels.get(index_of(eid)) or "").strip()
               or str(r.get("instruction") or "").strip())
        cap = str(auto_caps.get(eid) or "").strip()
        if lab:
            text, src = lab, SRC_LABEL
        elif cap:
            text, src = cap, SRC_CAPTION
        else:
            text, src = "", SRC_NONE
        new_text[eid], new_src[eid] = text, src
        assignment[eid] = (UNASSIGNED, NO_SUBSKILL)
        if text:
            to_assign[eid] = text
    if newcomers:
        fresh = _PerEpisodeCaptions(logs, lock)(
            newcomers, captioner,
            n_frames=int((cfg.get("skill_profile") or {}).get("n_frames", 8)),
            precomputed=auto_caps,
            max_concurrency=int((cfg.get("skill_profile") or {}).get("caption_concurrency", 8)))
        for r, cap in zip(newcomers, fresh):
            eid = r["episode_id"]
            caps[index_of(eid)] = cap
            lab = str(relabels.get(index_of(eid)) or "").strip() or r.get("instruction")
            text, src = grouping_text_and_source(lab, cap)
            new_text[eid], new_src[eid] = text, src
            assignment[eid] = (UNASSIGNED, NO_SUBSKILL)
            if text:
                to_assign[eid] = text
    member_map = member_map_of(list(old_rows.values()))
    assignment.update(reassign_texts(to_assign, member_map, taxonomy_from_profile(profile),
                                     llm_ask))
    cap_of = {eid: str(caps.get(index_of(eid)) or (old_rows.get(eid) or {}).get("caption") or "")
              for eid in assignment}
    instr_of = {eid: new_text[eid] for eid in assignment if new_src.get(eid) == SRC_LABEL}
    new_profile = rebuild_profile(assignment, profile, cap_of, instr_of)
    audit = {tier: [e for e in entries if e.get("id") in wanted]
             for tier, entries in (previous["label_audit"] or {}).items()
             if isinstance(entries, list)}
    ctx.log("info", f"skill_profile --incremental: {len(removed)} removed, {len(added)} added "
                    f"({len(by_person)} brought in by a person, filed from their text), "
                    f"{len(to_assign)} re-filed; the taxonomy is kept")
    return new_profile, cap_of, new_text, new_src, audit
