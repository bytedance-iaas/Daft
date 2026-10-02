"""Read an episode as soon as the funnel writes its SQLite result."""
from __future__ import annotations

from curation.pipeline import aggregate as agg
from curation.pipeline import policy as policy_mod
from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.records import is_v2
from curation.pipeline.timing import processing_times

from ..errors import ApiError
from ..orchestr.workdir import read_json
from .files import json_safe
from .store import store_of


def _context(runtime, task):
    run_dir = store_of(runtime).task_dir(task.id)
    path = state_path(run_dir)
    if not path.is_file():
        return run_dir, None, [], None
    plan = read_json(run_dir / "plan.json", {}) or {}
    modules = [m for st in plan.get("stages") or []
               if st.get("id") in agg.FUNNEL_STAGES for m in st.get("modules") or []]
    # the task's policy, frozen at start (design doc 17 §4.1): the verdict so far is graded with it
    policy = policy_mod.load(str(run_dir))
    return run_dir, EpisodeState(path, policy), modules, policy


def _item(run_dir, store, modules, policy, row, *, include_records=False):
    ep = row["episode_index"]
    records = store.episode_records(ep)
    verdict = reason = None
    if row["next_stage"] == "done" and row["reason"] != "missing" and policy is not None \
            and all(is_v2(r) for r in records.values()):           # a task made before C2 2.0: no live verdict
        state = agg.RunState(str(run_dir), modules, [ep], policy,
                             results={m: ({ep: records[m]} if m in records else {}) for m in modules},
                             autolabel={})
        v = agg.machine(state, ep)
        verdict, reason = v.verdict, v.reason()
    item = {k: v for k, v in row.items() if k != "updated_seq"}
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
        return {"items": [_item(run_dir, store, modules, policy, row) for row in rows],
                "next_cursor": rows[-1]["updated_seq"] if more and rows else None,
                **totals}
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
        return _item(run_dir, store, modules, policy, row, include_records=True)
    finally:
        store.close()
