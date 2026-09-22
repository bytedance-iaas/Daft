"""The plan of a task: W6's planner, called once when the task first runs (design doc 04 §3).

``plan.json`` is data: generated here, kept in the run directory (and so in the
delivery), read back by every stage (``--plan-stage``), served read-only by
``GET /tasks/{id}/plan``. Nobody submits one (D5). Its inputs:

* the preflight frozen at start and the selected modules and episodes;
* the upper bounds (D31): the task's ``params.limits``, the model's and the
  backend's parallelism from the VLM snapshot (P17), the node's CPU cores, and the
  number of tasks running when this one starts (P1: the VLM budget is shared);
* the site settings (the ``concurrency`` and ``vlm`` blocks of the site.yaml at
  ``CURATOR_SITE_CONFIG``, else ``CURATION_CONFIG`` - the file the chart writes).
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
    write_run_json(run, plan)
    n = plan["limits"]["vlm_parallelism"]
    cpu = plan["limits"]["cpu_concurrency"]
    run.log("system", "info", f"执行计划：{len(plan['stages'])} 档，CPU 并发 {cpu['value']}"
                              f"（卡在 {cpu['bound_by']}），VLM 并行度 {n['value']}（卡在 {n['bound_by']}）")
    return plan


def code_version() -> str:
    """``CURATOR_CODE_VERSION`` (the image sets it), else the installed package's version."""
    import os

    env = os.environ.get("CURATOR_CODE_VERSION", "").strip()
    if env:
        return env
    try:
        from importlib.metadata import version

        return f"curation {version('curation')}"
    except Exception:  # noqa: BLE001 - a development tree
        return "unknown"


def write_run_json(run, plan: dict) -> None:
    """``run.json`` of the batch (06 §1-§2): what ran, on what, with which versions."""
    task = run.task
    snap = task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else None
    pf = run.preflight
    modules = [{"id": m.module_id, **({"params": m.params} if m.params else {})}
               for m in run.repo.get_task_modules(run.task_id) if m.selected]
    doc = {"schema": "curator.run/1", "task_id": task.id, "run_id": task.run_id,
           "name": task.name, "created_at": task.created_at, "started_at": task.started_at,
           "input": {"source": task.input_source, "uri": task.input_uri,
                     "region": task.input_region},
           "output": {"uri": task.output_uri, "region": task.output_region},
           "episodes": task.episode_selector, "modules": modules, "params": task.params,
           "preflight": {"format": pf.get("format"), "dataset": pf.get("dataset"),
                         "meta_fingerprint": pf.get("meta_fingerprint")},
           "source": task.source_fingerprint, "planned_modules": run.plan_modules(plan),
           "fingerprints": {"code": code_version(),
                            "vlm": ({"model": snap.get("model"),
                                     "reasoning_effort": snap.get("reasoning_effort")}
                                    if snap else None)}}
    write_json_atomic(run.wd.run_json, doc)


def mark_skipped_modules(run, plan: dict) -> None:
    """Selected modules the plan left out (unsupported on this dataset) are ``skipped``."""
    planned = set(run.plan_modules(plan))
    for m in run.repo.get_task_modules(run.task_id):
        if m.selected and m.module_id not in planned and m.state == "pending":
            run.repo.update_module_state(run.task_id, m.module_id, {"pending"}, "skipped",
                                         error=m.unavailable_reason)
