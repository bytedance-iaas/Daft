"""The plan of a task: W6's planner, called once when the task first runs (design doc 04 §3).

``plan.json`` is data: generated here, kept in the run directory (and so in the
delivery), read back by every stage (``--plan-stage``), served read-only by
``GET /tasks/{id}/plan``. Nobody submits one (D5). Its inputs:

* the preflight frozen at start and the selected modules and episodes;
* the upper bounds (D31): the task's ``params.limits``, the model's and the
  backend's parallelism from the VLM snapshot (P17), the node's CPU cores, and the
  number of tasks running when this one starts (P1: the VLM budget is shared);
* the site settings (``CURATOR_SITE_CONFIG``: ``concurrency`` and ``vlm`` blocks).
"""
from __future__ import annotations

import logging

from ..repo import protocol as P
from .runbase import TaskFailure
from .workdir import read_json, write_json_atomic

log = logging.getLogger("daemon.orchestr")


def plan_limits(run) -> dict:
    task = run.task
    limits = ((task.params or {}).get("limits") or {})
    snap = task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else {}
    out = {"cpu_concurrency": limits.get("cpu_concurrency"),
           "vlm_parallelism": limits.get("vlm_parallelism"),
           "cpu_cores": run.cfg.cpu_cores,
           "running_tasks": max(1, run.orch.scheduler.running_count())}
    if snap.get("max_concurrency"):
        out["model_parallelism"] = int(snap["max_concurrency"])
    if snap.get("backend_id"):
        try:
            backend = run.repo.get_vlm_backend(str(snap["backend_id"]), owner=task.owner_id)
            out["backend_parallelism"] = int(backend.max_concurrency)
        except P.NotFound:
            pass
    return {k: v for k, v in out.items() if v is not None}


def ensure_plan(run) -> dict:
    doc = read_json(run.wd.plan, None)
    if isinstance(doc, dict) and doc.get("stages"):
        return doc
    from curation.planner import PlanError, build_plan

    selected = [m.module_id for m in run.repo.get_task_modules(run.task_id) if m.selected]
    try:
        plan = build_plan(run.preflight, selected, run.selection(), plan_limits(run),
                          run.cfg.site_config or None)
    except (PlanError, ValueError) as err:
        raise TaskFailure("plan_failed", f"生成执行计划失败：{err}") from None
    write_json_atomic(run.wd.plan, plan)
    n = plan["limits"]["vlm_parallelism"]
    cpu = plan["limits"]["cpu_concurrency"]
    run.log("system", "info", f"执行计划：{len(plan['stages'])} 档，CPU 并发 {cpu['value']}"
                              f"（卡在 {cpu['bound_by']}），VLM 并行度 {n['value']}（卡在 {n['bound_by']}）")
    return plan


def mark_skipped_modules(run, plan: dict) -> None:
    """Selected modules the plan left out (unsupported on this dataset) are ``skipped``."""
    planned = set(run.plan_modules(plan))
    for m in run.repo.get_task_modules(run.task_id):
        if m.selected and m.module_id not in planned and m.state == "pending":
            run.repo.update_module_state(run.task_id, m.module_id, {"pending"}, "skipped",
                                         error=m.unavailable_reason)
