"""Read an episode as soon as the pipeline writes its SQLite result.

Every episode gets its per-stage state over both blocks (design doc 17 §3.3) and a provisional verdict: the
task's policy applied to the findings so far - the final one is only aggregate's (the human decisions). Once the
run is over (a terminal task with a result revision, no subtask running) the row carries that final verdict
instead, from the current revision's lists (C4 5.3.2).
"""
from __future__ import annotations

from curation.contracts import modules as registry
from curation.pipeline import aggregate as agg
from curation.pipeline import policy as policy_mod
from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.timing import processing_times
from curation.pipeline.verdicts import judge

from ..errors import ApiError
from ..orchestr.workdir import read_json
from ..repo import protocol as P
from .files import json_safe
from .store import store_of


def _context(runtime, task):
    run_dir = store_of(runtime).task_dir(task.id)
    path = state_path(run_dir)
    if not path.is_file():
        return run_dir, None, [], None
    plan = read_json(run_dir / "plan.json", {}) or {}
    # a module of two halves is in its CPU half's stage too (registry 5.3): listed once
    modules = list(dict.fromkeys(m for st in plan.get("stages") or []
                                 if st.get("id") in agg.EPISODE_STAGES or st.get("id") in registry.PREP_STAGES
                                 for m in st.get("modules") or []))
    # the task's policy, frozen at start (design doc 17 §4.1): the verdict so far is graded with it
    policy = policy_mod.load(str(run_dir))
    return run_dir, EpisodeState(path), modules, policy


#: a final list -> the verdict it stands for (C2 final-list)
_LIST_VERDICT = {"passed": "keep", "reject": "drop", "held": "held"}


def _final(runtime, task) -> dict[int, tuple[str, str | None]] | None:
    """episode -> (verdict, reason) of the current result revision, once nothing runs on the task any more:
    terminal, with a revision, no subtask (a retry or an applied decision reruns episodes: their rows are live
    again). None while the run is on, or when the revision cannot be read here - the rows stay provisional."""
    if int(task.result_rev or 0) < 1 or task.state not in P.TERMINAL_STATES \
            or runtime.repo.active_subtask(task.id) is not None:
        return None
    try:
        rev = store_of(runtime).current(task, backfill=False)
        entries = rev.entries() if rev is not None else None
    except ApiError:
        return None
    if entries is None:
        return None
    out = {}
    for ep, (name, entry) in entries.items():
        texts = [r["text"] for r in entry.get("reasons") or [] if isinstance(r, dict) and r.get("text")]
        out[ep] = (_LIST_VERDICT[name], "；".join(texts) or None)
    return out


def _provisional(ep: int, records: dict, modules: list[str], policy):
    """The policy on the findings so far: a blocking finding drops it, a module that failed on it holds it,
    else it stays in (design doc 17 §3.3) - a module with no record yet holds nothing."""
    have = {m: records[m] for m in modules if m in records}
    v = judge(ep, have, [m for m in have if not registry.get(m).rides_on], policy)
    return v.verdict, v.reason()


def _item(run_dir, store, modules, policy, row, *, final=None, include_records=False):
    ep = row["episode_index"]
    records = store.episode_records(ep)
    verdict = reason = None
    item = {"episode_index": ep, "reason": row["reason"], "stages": store.stage_states(ep), "provisional": True}
    if final is not None and ep in final:                       # the run is over: aggregate's verdict
        item["provisional"] = False
        verdict, reason = final[ep]
    elif records and policy is not None and row["reason"] != "missing":
        verdict, reason = _provisional(ep, records, modules, policy)
    item.update(verdict=verdict, verdict_reason=reason)
    times = processing_times(records)
    item.update(processing_s=round(sum(times.values()), 3) if times else None,
                stage_processing_s=times)
    if include_records:
        item["modules"] = json_safe(records)
    return item


def page(runtime, task, *, before: int | None, limit: int) -> dict:
    run_dir, store, modules, policy = _context(runtime, task)
    if store is None:
        return {"items": [], "next_cursor": None, "started": 0, "finished": 0}
    try:
        rows = store.recent(before=before, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        totals = store.totals()
        final = _final(runtime, task)
        out = {"items": [_item(run_dir, store, modules, policy, row, final=final) for row in rows],
               "next_cursor": rows[-1]["updated_seq"] if more and rows else None,
               **totals}
        out["modules"] = [{"id": m, **c} for m, c in store.module_counts(modules).items()]   # design doc 17 §5.3
        return out
    finally:
        store.close()


def episode(runtime, task, index: int) -> dict:
    run_dir, store, modules, policy = _context(runtime, task)
    if store is None:
        raise ApiError("not_found", "流水线尚未产生 episode 结果")
    try:
        row = store.episode(index)
        if row is None:
            raise ApiError("not_found", f"episode {index} 尚未完成任何验证阶段")
        final = _final(runtime, task)
        return _item(run_dir, store, modules, policy, row, final=final, include_records=True)
    finally:
        store.close()
