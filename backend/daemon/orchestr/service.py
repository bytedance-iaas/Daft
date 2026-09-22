"""``Orchestrator`` - one per Daemon (``runtime.orchestrator``): what the routes call.

It owns the executor (every CLI child), the worker pool, the per-delivery publish
locks, the work-directory janitor and restorer (7 days after the end, back from the
delivery on demand - also for W5b's readers), and the lifecycle hooks of the runtime:

* ``on_ready`` (after the startup reconciliation): reap children a killed Daemon left
  behind, lower the Daemon's oom_score_adj, rebuild the queue and start the pool -
  system-paused work that reconciliation re-queued resumes by itself (D26) - and the
  janitor;
* ``on_stopping`` (SIGTERM): take nothing new, move every running task / subtask to
  ``pausing`` (system) and SIGTERM its command; they end ``paused`` (system), never
  ``failed``, even when SIGKILL has to follow after 90 s (09 §2.3);
* ``on_shutdown``: wait for the runs to wind down, kill whatever is left - no child
  outlives the Daemon.

Actions follow the state machine of C5 and answer an illegal transition with 409
``task_state_conflict`` and the current state. A task with a subtask in progress:
pause / resume / stop act on that subtask (the task itself is finished).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from typing import Any

from curation.contracts import modules as registry

from .. import taskspec, transitions
from ..auth import Principal
from ..errors import ApiError
from ..exec import Executor
from ..repo import protocol as P
from ..secrets import service_of
from ..secrets.effort import EffortNotAllowed
from ..util import canonical_json, sha256_hex
from . import rules
from .backfill import Restorer
from .browse import Browser
from .config import OrchestratorConfig
from .datasets import DatasetOps, Source, format_supported
from .delivery import DeliveryError, DeliveryLocks, forget_sync, open_delivery, read_latest
from .janitor import Janitor
from .resources import lower_daemon_oom_score
from .runbase import PROC_FILE, SHUTDOWN_REASON
from .scheduler import Scheduler
from .start import Draft, StartChecks
from .workdir import WorkDir, read_json, write_json_atomic

log = logging.getLogger("daemon.orchestr")

PREFLIGHT_TTL_MS = taskspec.PREFLIGHT_MAX_AGE_MS
_UNFINISHED_SUB = ("queued", "running", "pausing", "paused", "stopping")
#: which pre-start checks each subtask kind runs (03 §3: "与它相关的检查")
SUBTASK_CHECKS = {"retry": ("input", "output", "vlm"), "resume": ("input", "output", "vlm"),
                  "apply_adjudication": ("input", "output", "vlm"),
                  "reexport": ("input", "output")}


def conflict(task_or_state, message: str, **details: Any) -> ApiError:
    state = task_or_state if isinstance(task_or_state, str) else task_or_state.state
    return ApiError("task_state_conflict", message, details={"state": state, **details})


class Orchestrator:
    def __init__(self, rt, cfg: OrchestratorConfig | None = None,
                 executor: Executor | None = None):
        self.rt = rt
        self.repo: P.Repository = rt.repo
        self.hub = rt.hub
        self.logs = rt.logs
        self.clock = rt.clock
        self.settings = rt.settings
        self.cfg = cfg or OrchestratorConfig.from_env()
        self.work_root = self.settings.work_dir
        self.executor = executor or Executor(self.cfg.program, term_grace_s=self.cfg.term_grace_s,
                                             int_grace_s=self.cfg.int_grace_s)
        self.locks = DeliveryLocks()
        self.scheduler = Scheduler(self)
        self.checks = StartChecks(self)
        self.datasets = DatasetOps(self)
        self.browser = Browser(self)
        self.restorer = Restorer(self)
        self.janitor = Janitor(self)
        self._preflight_inputs: dict[str, tuple] = {}
        self._shutting_down = threading.Event()

    @property
    def svc(self):
        return service_of(self.rt)

    # ================================================================== lifecycle
    def on_ready(self, rt) -> None:
        lower_daemon_oom_score()
        self.reap_orphans()
        if self.cfg.enabled:
            self.scheduler.start()
            self.janitor.start()
        else:
            log.warning("CURATOR_ORCHESTRATOR=off: queued tasks are not run")

    def on_stopping(self, rt) -> None:
        self.janitor.stop()
        self.system_pause_all()

    def on_shutdown(self, rt) -> None:
        self.janitor.stop()
        self.system_pause_all()
        if not self.scheduler.wait_idle(self.cfg.shutdown_wait_s):
            n = self.executor.kill_all()
            log.warning("shutdown: %d command(s) still running after %.0fs; killed", n,
                        self.cfg.shutdown_wait_s)
            self.scheduler.wait_idle(15.0)
        self.scheduler.stop()

    def system_pause_all(self) -> None:
        """Graceful shutdown (09 §2.3): nothing new starts, running work is paused (system)."""
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()
        self.scheduler.stop_accepting()
        now = self.clock()
        for job in self.scheduler.jobs():
            reason = SHUTDOWN_REASON
            if job.sub_id is None:
                transitions.change_task_state(self.repo, self.hub, job.task_id, {"running"},
                                              "pausing", at=now, pause_reason="system",
                                              reason=reason, owner=job.owner)
            else:
                transitions.change_subtask_state(self.repo, self.hub, job.sub_id, {"running"},
                                                 "pausing", at=now, pause_reason="system",
                                                 reason=reason, owner=job.owner)
            job.request("shutdown")
        log.info("shutdown: %d running job(s) asked to pause", len(self.scheduler.jobs()))

    def reap_orphans(self) -> int:
        """Children a killed Daemon left running (outside a container nobody else reaps them):
        found through ``.orchestr/proc.json`` of unfinished work, stopped before re-running."""
        seen = 0
        task_ids = {t.id for t in self.repo.tasks_in_states(["queued", "paused"])}
        task_ids |= {s.task_id for s in self.repo.subtasks_in_states(["queued", "paused"])}
        for task_id in task_ids:
            path = WorkDir(self.work_root, task_id).private / PROC_FILE
            doc = read_json(path, None)
            if not isinstance(doc, dict) or not isinstance(doc.get("pid"), int):
                continue
            pid = doc["pid"]
            if _is_curation_process(pid):
                seen += 1
                log.warning("task %s: a %s command (pid %d) outlived the last Daemon; stopping it",
                            task_id, doc.get("stage"), pid)
                _stop_group(pid, self.cfg.term_grace_s)
            try:
                path.unlink()
            except OSError:
                pass
        return seen

    # ================================================================== helpers
    def task(self, task_id: str, who: Principal) -> P.Task:
        return self.repo.get_task(task_id, owner=who.owner_id)

    def _audit(self, action: str, task_id: str, who: Principal, detail: dict | None = None):
        transitions.record(self.repo, action=action, task_id=task_id, actor=who.display_name,
                           at=self.clock(), detail=detail, owner=who.owner_id)

    def _needs_vlm(self, module_ids) -> bool:
        from ..secrets import prechecks

        return prechecks.needs_vlm(module_ids)

    # ================================================================== create
    def _resolve(self, body: dict, who: Principal) -> tuple[taskspec.Resolved, dict]:
        """``TaskCreate`` -> task fields, module rows and the preflight (as PATCH resolves)."""
        placeholder = P.Task(id="", name=body.get("name") or "", state="created",
                             input_source="", input_uri="", output_uri="", delivery_key="",
                             episode_selector={"mode": "all"}, params={}, owner_id=who.owner_id)
        resolved = taskspec.resolve_config(self.repo, self.settings, placeholder, body,
                                           now=self.clock(), owner=who.owner_id)
        fields = resolved.fields
        if fields.get("vlm_model_id"):
            try:
                self.svc.check_task_effort(fields["vlm_model_id"],
                                           fields.get("vlm_reasoning_effort"), owner=who.owner_id)
            except EffortNotAllowed as err:
                raise ApiError("validation_failed", err.message_zh, details={"errors": [
                    {"field": "vlm.reasoning_effort", "problem": str(err)}]}) from None
        known = self._preflight_inputs.get(body.get("preflight_id"))
        here = (fields.get("input_source"), fields.get("input_uri"), fields.get("input_region"))
        if known is not None and known != here:
            raise ApiError("validation_failed", "这个预检结果不是这个数据集的：请对当前的数据集重新预检",
                           details={"errors": [{"field": "preflight_id",
                                                "problem": "preflight of another input"}]})
        return resolved, fields

    def _draft(self, fields: dict, modules: list[P.TaskModule], owner: str) -> Draft:
        selected = [m.module_id for m in modules if m.selected]
        return Draft(fields["input_source"], fields["input_uri"], fields.get("input_region"),
                     fields.get("input_cred_id"), fields["output_uri"], fields.get("output_region"),
                     fields.get("output_cred_id"), fields.get("vlm_model_id"),
                     fields.get("vlm_reasoning_effort"), self._needs_vlm(selected),
                     dataset_id=fields.get("dataset_id"), owner=owner)

    def _warnings(self, fields: dict, owner: str) -> list[str]:
        """03 §3 step 7: another dataset's batches in the same delivery - said, not refused."""
        out = []
        page = self.repo.list_tasks(owner=owner, page=1, page_size=100,
                                    delivery_key=fields.get("delivery_key"))
        others = sorted({t.input_uri for t in page.items if t.input_uri != fields["input_uri"]})
        if others:
            out.append(f"交付目录 {fields['output_uri']} 下已经有别的数据集的批次（"
                       f"{'、'.join(others[:3])}{' 等' if len(others) > 3 else ''}），"
                       "每个任务写自己的批次目录，互不覆盖")
        return out

    def _insert(self, body: dict, resolved: taskspec.Resolved, fields: dict, who: Principal,
                dataset: P.Dataset | None) -> P.Task:
        params = {k: v for k, v in (body.get("params") or {}).items() if k != "start_now"}
        spec = P.TaskCreate(
            name=body["name"], note=body.get("note"), state="created",
            input_source=fields["input_source"], input_uri=fields["input_uri"],
            input_region=fields.get("input_region"), input_cred_id=fields.get("input_cred_id"),
            dataset_id=dataset.id if dataset is not None else fields.get("dataset_id"),
            output_uri=fields["output_uri"], output_region=fields.get("output_region"),
            output_cred_id=fields.get("output_cred_id"), delivery_key=fields["delivery_key"],
            episode_selector=fields["episode_selector"], params=params,
            modules=resolved.modules or [], embodiment_id=fields.get("embodiment_id"),
            vlm_model_id=fields.get("vlm_model_id"),
            vlm_reasoning_effort=fields.get("vlm_reasoning_effort"), owner_id=who.owner_id)
        with self.repo.transaction():
            task = self.repo.create_task(spec)
            task = self.repo.update_task_fields(task.id, if_updated_at=None, owner=who.owner_id,
                                                preflight=fields.get("preflight"))
            self._audit("task.create", task.id, who, {"name": task.name})
        return task

    def _register_quietly(self, fields: dict, owner: str) -> P.Dataset | None:
        """A full address without a registration is registered (03 §12); a failure here only
        delays it to the start, where the fingerprint check needs it anyway."""
        if fields.get("dataset_id") or not format_supported(fields.get("preflight")):
            return None
        try:
            ds, _created, _ = self.datasets.register(
                Source(fields["input_source"], fields["input_uri"], fields.get("input_region"),
                       fields.get("input_cred_id")), owner)
            return ds
        except ApiError as err:
            log.info("could not register %s at task creation: %s", fields["input_uri"],
                     err.message)
            return None

    def create_task(self, body: dict, who: Principal) -> tuple[P.Task, list[str]]:
        resolved, fields = self._resolve(body, who)
        warnings = self._warnings(fields, who.owner_id)
        start_now = (body.get("params") or {}).get("start_now", True)
        if not start_now:
            ds = self._register_quietly(fields, who.owner_id)
            return self._insert(body, resolved, fields, who, ds), warnings
        draft = self._draft(fields, resolved.modules or [], who.owner_id)
        self.checks.require(draft)
        ds, listing = self.checks.fingerprints(draft)
        task = self._insert(body, resolved, fields, who, ds)
        task = self.checks.freeze_and_queue(task, listing, ds, actor=who.display_name)
        return task, warnings

    def create_batch(self, body: dict, who: Principal) -> list[tuple[P.Task, list[str]]]:
        shared = dict(body["shared"])
        start_now = (shared.get("params") or {}).get("start_now", True)
        prepared = []
        for i, item in enumerate(body["items"]):
            one = {**shared, **item}
            one["params"] = {**(shared.get("params") or {}), "start_now": False}
            try:
                resolved, fields = self._resolve(one, who)
            except ApiError as err:
                if err.details is None:
                    err.details = {}
                err.details["item"] = i
                raise
            prepared.append((one, resolved, fields))
        out = []
        for one, resolved, fields in prepared:
            warnings = self._warnings(fields, who.owner_id)
            ds = self._register_quietly(fields, who.owner_id)
            task = self._insert(one, resolved, fields, who, ds)
            if start_now:
                try:
                    task = self.start(task.id, who)
                except ApiError as err:
                    warnings.append(f"已创建，但没有开始：{err.message}")
            out.append((task, warnings))
        return out

    # ================================================================== actions
    def start(self, task_id: str, who: Principal) -> P.Task:
        task = self.task(task_id, who)
        if task.state != "created":
            raise conflict(task, "只有待启动的任务可以开始")
        draft = Draft.of_task(self.repo, task)
        self.checks.require(draft)
        ds, listing = self.checks.fingerprints(draft)
        return self.checks.freeze_and_queue(self.task(task_id, who), listing, ds,
                                            actor=who.display_name)

    def pause(self, task_id: str, who: Principal) -> P.Task:
        task = self.task(task_id, who)
        sub = self.repo.active_subtask(task_id)
        now = self.clock()
        if sub is not None and sub.state in _UNFINISHED_SUB:
            if not transitions.change_subtask_state(self.repo, self.hub, sub.id, {"running"},
                                                    "pausing", at=now, pause_reason="user",
                                                    actor=who.display_name, owner=task.owner_id):
                raise conflict(sub.state, "只有运行中的子任务可以暂停", subtask_id=sub.id)
            job = self.scheduler.job_for(task_id, sub.id)
            if job is not None:
                job.request("pause")
            return self.task(task_id, who)
        if not transitions.change_task_state(self.repo, self.hub, task_id, {"running"}, "pausing",
                                             at=now, pause_reason="user", actor=who.display_name,
                                             owner=task.owner_id):
            raise conflict(self.task(task_id, who), "只有运行中的任务可以暂停")
        job = self.scheduler.job_for(task_id)
        if job is not None:
            job.request("pause")
        return self.task(task_id, who)

    def resume(self, task_id: str, who: Principal) -> P.Task:
        task = self.task(task_id, who)
        sub = self.repo.active_subtask(task_id)
        now = self.clock()
        if sub is not None and sub.state == "paused":
            if transitions.change_subtask_state(self.repo, self.hub, sub.id, {"paused"}, "queued",
                                                at=now, actor=who.display_name,
                                                owner=task.owner_id):
                self.scheduler.enqueue(task_id, sub.id, owner=task.owner_id)
                return self.task(task_id, who)
        if not transitions.change_task_state(self.repo, self.hub, task_id, {"paused"}, "queued",
                                             at=now, actor=who.display_name, owner=task.owner_id):
            raise conflict(self.task(task_id, who), "只有已暂停的任务可以恢复")
        self.scheduler.enqueue(task_id, owner=task.owner_id)
        return self.task(task_id, who)

    def stop(self, task_id: str, who: Principal) -> P.Task:
        task = self.task(task_id, who)
        sub = self.repo.active_subtask(task_id)
        if sub is not None and sub.state in _UNFINISHED_SUB:
            self._stop_one(task, sub, who)
            return self.task(task_id, who)
        self._stop_one(task, None, who)
        return self.task(task_id, who)

    def _stop_one(self, task: P.Task, sub: P.Subtask | None, who: Principal) -> None:
        now, actor, owner = self.clock(), who.display_name, task.owner_id
        reason = "用户停止"
        if sub is None:
            step = lambda frm, to, **kw: transitions.change_task_state(  # noqa: E731
                self.repo, self.hub, task.id, frm, to, at=now, actor=actor, owner=owner, **kw)
            key = (task.id, None)
        else:
            step = lambda frm, to, **kw: transitions.change_subtask_state(  # noqa: E731
                self.repo, self.hub, sub.id, frm, to, at=now, actor=actor, owner=owner, **kw)
            key = (task.id, sub.id)
        if step({"queued"}, "stopped", reason=reason):
            self.scheduler.discard(*key)
            return
        if step({"paused"}, "stopping", reason=reason):
            job = self.scheduler.job_for(*key)
            if job is None:
                step({"stopping"}, "stopped", reason=reason)
            else:
                job.request("stop")
            return
        if step({"running", "pausing"}, "stopping", reason=reason):
            job = self.scheduler.job_for(*key)
            if job is not None:
                job.request("stop")
            else:                                  # nobody is running it (should not happen)
                step({"stopping"}, "stopped", reason=reason)
            return
        state = (self.repo.get_subtask(sub.id).state if sub is not None
                 else self.task(task.id, who).state)
        raise conflict(state, "这个状态的任务不能停止")

    # ================================================================== subtasks
    def _journal_failure(self, task_id: str) -> dict:
        doc = read_json(WorkDir(self.work_root, task_id).journal("main"), {}) or {}
        return doc.get("failure") or {}

    def create_subtask(self, task_id: str, kind: str, who: Principal, *,
                       scope: dict | None = None) -> P.Subtask:
        task = self.task(task_id, who)
        active = self.repo.active_subtask(task_id)
        if active is not None:
            raise ApiError("subtask_active", "这个任务还有子任务没结束，等它结束后再试",
                           details={"state": task.state, "active_subtask": active.id})
        allowed = P.SUBTASK_PARENT_STATES[kind]
        if task.state not in allowed:
            what = {"retry": "只有「错误」状态的任务可以重试",
                    "resume": "只有已停止或失败的任务可以继续运行",
                    "apply_adjudication": "只有已完成（含错误）的任务可以执行裁决",
                    "reexport": "只有已完成（含错误）的任务可以导出"}[kind]
            raise conflict(task, what)
        if not WorkDir(self.work_root, task_id).started():
            raise conflict(task, "这个任务的工作目录不完整，没法接着做：请复制为新任务")
        scope = dict(scope or {})
        if kind == "resume":
            if self._journal_failure(task_id).get("code") == "source_changed":
                raise conflict(task, "源数据在运行中变了的任务不能继续运行：请重新预检后复制为新任务")
            scope.setdefault("episodes", "all")
        elif kind == "retry":
            rows = [m for m in self.repo.get_task_modules(task_id) if m.selected]
            erred = [m.module_id for m in rows if m.episodes_error > 0 or m.state == "failed"]
            wanted = scope.get("modules") or erred
            unknown = [m for m in wanted if m not in {r.module_id for r in rows}]
            if unknown:
                raise ApiError("validation_failed", f"这些模块不在这个任务里：{', '.join(unknown)}",
                               details={"errors": [{"field": "modules", "problem": "not selected"}]})
            if not wanted:
                raise conflict(task, "没有出错的条目或整体失败的模块，不需要重试")
            scope = {"modules": [m for m in registry.ids() if m in wanted], "episodes": "errors"}
        elif kind == "apply_adjudication":
            if not self.repo.latest_adjudications(task_id, unapplied_only=True):
                raise conflict(task, "没有待执行的裁决：先在裁决页做出判断")
            scope = {"relabel_rerun": scope.get("relabel_rerun") or "v1"}
        elif kind == "reexport":
            if int(task.result_rev or 0) < 1:
                raise conflict(task, "这个任务还没有结果，没什么可导出的")
        self.checks.require(Draft.of_task(self.repo, task), SUBTASK_CHECKS[kind])
        try:
            sub = self.repo.create_subtask(P.Subtask(id="", task_id=task_id, kind=kind,
                                                     scope=scope, state="queued"))
        except P.Conflict as err:
            if err.code == "subtask_active":
                raise ApiError("subtask_active", "这个任务还有子任务没结束，等它结束后再试") from None
            raise
        except P.StateConflict:
            raise conflict(self.task(task_id, who), "任务的状态变了，没有建子任务") from None
        self._audit(f"task.{kind}", task_id, who, {"subtask_id": sub.id, "scope": scope})
        self.hub.publish_state(task_id, "queued", subtask_id=sub.id, at=self.clock())
        self.scheduler.enqueue(task_id, sub.id, owner=task.owner_id)
        return sub

    # ================================================================== D37 again
    def compatibility(self, task: P.Task, preflight: dict) -> list[dict]:
        """What stops the task from running on the re-preflighted dataset (03 §12)."""
        out = []
        availability = {m["id"]: m for m in preflight.get("modules") or []}
        fmt = preflight.get("format") or {}
        if not fmt.get("supported"):
            out.append({"field": "input", "reason_code": "format_unsupported",
                        "reason": f"数据集现在不是受支持的格式：{fmt.get('detail') or fmt.get('kind')}"})
        for m in self.repo.get_task_modules(task.id):
            if not m.selected:
                continue
            entry = availability.get(m.module_id) or {}
            state = entry.get("availability", "available")
            name = registry.get(m.module_id).name_zh if m.module_id in registry.ids() else m.module_id
            if state == "unsupported":
                out.append({"field": "modules", "module": m.module_id,
                            "reason_code": entry.get("reason_code") or "unsupported",
                            "reason": f"「{name}」在这个数据集上不可用了：{entry.get('reason', '')}"})
            elif state == "needs_input":
                field = (entry.get("input_hint") or {}).get("field")
                if field == "embodiment_id" and not task.embodiment_id:
                    out.append({"field": "embodiment_id", "module": m.module_id,
                                "reason_code": entry.get("reason_code") or "robot_type_unknown",
                                "reason": f"「{name}」需要补充机器人型号"})
                elif field == "vlm" and not task.vlm_model_id:
                    out.append({"field": "vlm", "module": m.module_id,
                                "reason_code": entry.get("reason_code") or "vlm_backend_missing",
                                "reason": f"「{name}」需要选择模型服务和模型"})
        count = int(((preflight.get("dataset") or {}).get("episode_count")) or 0)
        sel = task.episode_selector or {}
        if sel.get("mode") == "explicit":
            beyond = [i for i in sel.get("indices") or [] if int(i) >= count]
            if beyond:
                out.append({"field": "episodes", "reason_code": "episodes_out_of_range",
                            "reason": f"自选的 episode 超出了范围：数据集现在只有 {count} 条"
                                      f"（{', '.join(map(str, beyond[:5]))} 不存在）"})
        return out

    def repreflight_task(self, task_id: str, who: Principal) -> dict:
        task = self.task(task_id, who)
        if task.state != "created":
            raise conflict(task, "只有待启动的任务需要重新预检")
        src = Source.of_task(task)
        ds = self.repo.get_dataset(task.dataset_id, owner=who.owner_id) if task.dataset_id \
            else None
        if ds is None:
            ds, _, _ = self.datasets.register(src, who.owner_id)
        ds, listing = self.datasets.repreflight(ds, who.owner_id)
        backend = None
        if task.vlm_model_id:
            for b in self.repo.list_vlm_backends(owner=who.owner_id):
                if any(m.id == task.vlm_model_id for m in b.models):
                    backend = b.name
        preflight = self.datasets.preflight(src, who.owner_id, vlm_backend=backend,
                                            embodiment_id=task.embodiment_id)
        incompatible = self.compatibility(task, preflight)
        with self.repo.transaction():
            self.repo.update_task_fields(task.id, if_updated_at=None, owner=who.owner_id,
                                         preflight=preflight, dataset_id=ds.id)
            rows = self.repo.get_task_modules(task.id)
            choices = [(m.module_id, m.params) for m in rows if m.selected]
            availability = {m["id"]: m for m in preflight.get("modules") or []}
            self.repo.upsert_task_modules(task.id,
                                          taskspec.module_rows(task.id, choices, availability))
        task = self.task(task_id, who)
        if incompatible or listing is None:
            return {"compatible": False, "incompatibilities": incompatible, "task": task}
        self.checks.require(Draft.of_task(self.repo, task))
        task = self.checks.freeze_and_queue(task, listing, ds, actor=who.display_name)
        return {"compatible": True, "incompatibilities": [], "task": task}

    # ================================================================== purge
    def purge(self, task_id: str, confirm_path: str, who: Principal) -> dict:
        task = self.task(task_id, who)
        if task.state not in P.TERMINAL_STATES:
            raise conflict(task, "任务结束后才能清理交付产物")
        if self.repo.active_subtask(task_id) is not None:
            raise ApiError("subtask_active", "这个任务还有子任务没结束，等它结束后再清理")
        if not task.run_id:
            raise conflict(task, "这个任务还没有写过交付产物")
        path = f"{task.output_uri.rstrip('/')}/{task.run_id}/"
        if str(confirm_path or "").strip() != path:
            raise ApiError("confirm_path_mismatch", f"确认的路径和要清理的不一致，应为 {path}",
                           details={"expected": path})
        key = None
        if self.cfg.local_delivery_root is None:
            key = self.svc.tos_key(task.output_cred_id, owner=task.owner_id, role="output")
        region = task.output_region or (key.region if key is not None else None)
        lock = self.locks.lock(task.delivery_key)
        lock.acquire()
        try:
            with open_delivery(task.output_uri, local_root=self.cfg.local_delivery_root,
                               svc=self.svc, key=key, region=region) as d:
                listing = d.list(task.run_id)
                latest_removed = read_latest(d) == task.run_id
        except DeliveryError as err:
            lock.release()
            raise ApiError("validation_failed", f"{err.message_zh}：{err.cause}") from None
        except BaseException:
            lock.release()
            raise
        total = sum(listing.values())

        def work() -> None:
            try:
                with open_delivery(task.output_uri, local_root=self.cfg.local_delivery_root,
                                   svc=self.svc, key=key, region=region) as d:
                    for rel in sorted(listing):
                        d.delete(rel)
                    if latest_removed:
                        d.delete("latest")
                wd = WorkDir(self.work_root, task_id)
                forget_sync(wd.sync_state)
                wd.ensure()
                write_json_atomic(wd.purged_mark, {"at": self.clock(), "path": path})
                self.repo.set_export_fingerprint(task_id, None, True)
                log.info("purged %s (%d objects, %d bytes)", path, len(listing), total)
            except Exception:  # noqa: BLE001
                log.exception("purging %s failed", path)
            finally:
                lock.release()

        self._audit("task.purge_artifacts", task_id, who,
                    {"path": path, "objects": len(listing), "bytes": total,
                     "latest_removed": latest_removed})
        threading.Thread(target=work, name=f"purge-{task_id}", daemon=True).start()
        return {"path": path, "bytes": total, "latest_removed": latest_removed}

    # ================================================================== reads
    def plan(self, task_id: str, who: Principal) -> dict:
        self.task(task_id, who)
        doc = read_json(WorkDir(self.work_root, task_id).plan, None)
        if not isinstance(doc, dict):
            raise ApiError("not_found", "任务还没开始运行，还没有执行计划")
        return doc

    # ================================================================== preflight
    def preflight(self, body: dict, who: Principal) -> dict:
        fields = taskspec.resolve_input(self.repo, self.settings, body["input"], who.owner_id)
        backend = body.get("vlm_backend")
        if backend:
            try:
                self.repo.get_vlm_backend_by_name(backend, owner=who.owner_id)
            except P.NotFound:
                raise ApiError("validation_failed", f"模型服务「{backend}」不存在", details={
                    "errors": [{"field": "vlm_backend", "problem": "unknown backend"}]}) from None
        src = Source(fields["input_source"], fields["input_uri"], fields.get("input_region"),
                     fields.get("input_cred_id"))
        result = self.datasets.preflight(src, who.owner_id, vlm_backend=backend,
                                         embodiment_id=body.get("embodiment_id"))
        now = self.clock()
        request_hash = sha256_hex(canonical_json({"input": [src.source, src.uri, src.region,
                                                            src.credential_id],
                                                  "vlm_backend": backend,
                                                  "embodiment_id": body.get("embodiment_id")}))
        pid = self.repo.put_preflight(request_hash=request_hash, result=result, at=now,
                                      owner=who.owner_id)
        if len(self._preflight_inputs) > 2048:
            self._preflight_inputs.clear()
        self._preflight_inputs[pid] = (src.source, src.uri, src.region)
        return {"preflight_id": pid, "expires_at": now + PREFLIGHT_TTL_MS, "result": result}


def _is_curation_process(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "curation" in out


def _stop_group(pid: int, grace_s: float) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def install(app) -> Orchestrator:
    """Create the orchestrator of an app and hook it into the runtime's lifecycle."""
    from ..results import store_of

    rt = app.state.runtime
    orch = Orchestrator(rt)
    rt.orchestrator = orch
    rt.on_ready.append(orch.on_ready)
    rt.on_stopping.append(orch.on_stopping)
    rt.on_shutdown.append(orch.on_shutdown)
    store_of(rt).backfill = orch.restorer.hook       # W5b's readers restore cleaned run dirs
    return orch


def orchestrator_of(rt) -> Orchestrator:
    orch = getattr(rt, "orchestrator", None)
    if orch is None:
        raise RuntimeError("the orchestrator is not installed")
    return orch
