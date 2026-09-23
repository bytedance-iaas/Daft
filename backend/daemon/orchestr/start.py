"""Before a task (or a subtask) starts (design doc 03 §3 steps 4-7, §3.2, §12; D30, D37).

1. **The three hard checks** (W8's library, D30): the input readable with the input
   key, the delivery writable (a probe object written and deleted), one minimal call
   to the model when a selected module needs one. Any failure: 422 ``precheck_failed``
   with ``PrecheckDetails``. Retry, continue, re-export and applying an adjudication
   run the checks that concern them.
2. **The dataset fingerprints** (D37, starts only): the dataset's current full
   listing is compared with its registration (registering the address first when it
   has none). Same: that listing is the task's ``source_manifest.json``. Different:
   the change goes into the registration's history, ``check_state`` becomes
   ``changed`` and nothing starts - 409 ``source_changed`` with a ``SourceChange``.
3. **Freezing** (D27, P17): the batch name ``run_id`` (claimed in the delivery at
   once), the preflight, the source listing, the VLM settings (no key), then
   ``created -> queued`` and into the queue.
"""
from __future__ import annotations

import dataclasses
import logging

from .. import transitions
from ..errors import ApiError
from ..repo import protocol as P
from ..secrets import (CheckResult, InputTarget, OutputTarget, PrecheckReport, Unavailable,
                       VlmSelection, run_prechecks, task_needs_vlm)
from ..secrets.tos import PROBE_OBJECT
from . import rules
from .datasets import DatasetOps, Source
from .delivery import DeliveryError, LocalDelivery, allocate_run_id, local_root_for, open_delivery
from .workdir import WorkDir, write_json_atomic

log = logging.getLogger("daemon.orchestr")

ALL_CHECKS = ("input", "output", "vlm")


@dataclasses.dataclass
class Draft:
    """A task configuration, stored or not: what the checks and the D37 flow need."""

    input_source: str
    input_uri: str
    input_region: str | None
    input_cred_id: str | None
    output_uri: str
    output_region: str | None
    output_cred_id: str | None
    vlm_model_id: str | None
    vlm_reasoning_effort: str | None
    needs_vlm: bool
    dataset_id: str | None = None
    vlm_snapshot: dict | None = None
    owner: str = P.DEFAULT_OWNER

    @classmethod
    def of_task(cls, repo: P.Repository, task: P.Task) -> "Draft":
        return cls(task.input_source, task.input_uri, task.input_region, task.input_cred_id,
                   task.output_uri, task.output_region, task.output_cred_id, task.vlm_model_id,
                   task.vlm_reasoning_effort, task_needs_vlm(repo, task.id),
                   dataset_id=task.dataset_id,
                   vlm_snapshot=task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else None,
                   owner=task.owner_id)


class StartChecks:
    def __init__(self, orch):
        self.orch = orch
        self.datasets = DatasetOps(orch)

    # -- D30 ------------------------------------------------------------------------------
    def local_output_check(self, uri: str) -> CheckResult:
        """The write probe against the local stand-in of the delivery (experimental mode)."""
        root = local_root_for(self.orch.cfg.local_delivery_root, uri)
        d = LocalDelivery(root, uri)
        try:
            d.put_bytes(PROBE_OBJECT, b"")
            d.delete(PROBE_OBJECT)
        except DeliveryError as err:
            return CheckResult("output", False, "forbidden", f"{err.message_zh}：{err.cause}", uri)
        return CheckResult("output", True, "ok", f"在本地交付目录 {root} 写入并删除了探针对象", uri)

    def prechecks(self, draft: Draft, checks=ALL_CHECKS) -> PrecheckReport:
        wanted = set(checks)
        svc = self.orch.svc
        local_out = self.orch.cfg.local_delivery_root is not None
        inp = InputTarget(draft.input_source, draft.input_uri, draft.input_region,
                          draft.input_cred_id) if "input" in wanted else None
        out = OutputTarget(draft.output_uri, draft.output_region, draft.output_cred_id) \
            if "output" in wanted and not local_out else None
        sel = VlmSelection(draft.vlm_model_id, draft.vlm_reasoning_effort, draft.vlm_snapshot) \
            if "vlm" in wanted and draft.needs_vlm else None
        report = run_prechecks(svc, input=inp, output=out, vlm=sel, owner=draft.owner)
        if "output" in wanted and local_out:
            results = list(report.results)
            local = self.local_output_check(draft.output_uri)
            try:                                  # the output key must still exist
                svc.tos_key(draft.output_cred_id, owner=draft.owner, role="output")
            except Unavailable as err:
                local = CheckResult("output", False, err.code, err.message_zh, draft.output_uri)
            pos = 1 if inp is not None else 0
            results.insert(pos, local)
            report = PrecheckReport(tuple(results))
        return report

    def require(self, draft: Draft, checks=ALL_CHECKS) -> None:
        self.prechecks(draft, checks).raise_for_failure()

    # -- D37 ------------------------------------------------------------------------------
    def fingerprints(self, draft: Draft) -> tuple[P.Dataset, dict]:
        """(the registration, the listing to freeze); raises 409 ``source_changed``."""
        repo = self.orch.repo
        src = Source(draft.input_source, draft.input_uri, draft.input_region, draft.input_cred_id)
        ds = None
        if draft.dataset_id:
            try:
                ds = repo.get_dataset(draft.dataset_id, owner=draft.owner)
            except P.NotFound:
                ds = None
        listing = None
        if ds is None:
            ds, created, listing = self.datasets.register(src, draft.owner)
            if created and listing is not None:
                return ds, listing                  # registered this instant: nothing to compare
        record, listing = self.datasets.check(ds, draft.owner, trigger="task_start")
        if record.result == "changed":
            raise ApiError("source_changed",
                           "数据集和登记时（或上次预检时）不一样了：确认后重新预检，相容就接着开始",
                           details=record.change)
        if listing is None:
            raise ApiError("validation_failed", "这个数据集的格式不受支持，没法开始质检")
        return ds, listing

    # -- freezing ---------------------------------------------------------------------------
    def claim_run_id(self, task: P.Task) -> str:
        orch = self.orch
        base = rules.run_id_for(orch.clock(), orch.settings.tz_offset_minutes)
        key = None
        if orch.cfg.local_delivery_root is None:
            key = orch.svc.tos_key(task.output_cred_id, owner=task.owner_id, role="output")
        region = task.output_region or (key.region if key is not None else None)
        with orch.locks.lock(task.delivery_key):
            with open_delivery(task.output_uri, local_root=orch.cfg.local_delivery_root,
                               svc=orch.svc, key=key, region=region) as d:
                run_id = allocate_run_id(d, base)
                import json

                d.put_bytes(f"{run_id}/run.json", json.dumps(
                    {"schema": "curator.run/1", "task_id": task.id, "run_id": run_id,
                     "claimed_at": orch.clock()}, ensure_ascii=False).encode())
        return run_id

    def freeze_and_queue(self, task: P.Task, listing: dict, dataset: P.Dataset | None, *,
                         actor: str) -> P.Task:
        """created -> queued with everything the run will need (D27, P17)."""
        orch, repo = self.orch, self.orch.repo
        wd = WorkDir(orch.work_root, task.id)
        wd.ensure()
        orch.materialize_uploads(task, wd)          # module input files into the run dir (F5.5)
        try:
            run_id = self.claim_run_id(task)
        except (DeliveryError, Unavailable) as err:
            msg = getattr(err, "message_zh", str(err))
            raise ApiError("precheck_failed", f"交付目录写不进去：{msg}",
                           details={"checks": [{"id": "output", "ok": False, "code": "failed",
                                                "reason": msg, "target": task.output_uri}]}) \
                from None
        snapshot = None
        if task_needs_vlm(repo, task.id):
            try:
                snapshot = orch.svc.vlm_target_for_task(task).snapshot()
            except Unavailable as err:
                raise ApiError("validation_failed", err.message_zh) from None
        preflight = task.preflight if isinstance(task.preflight, dict) else {}
        write_json_atomic(wd.manifest, listing)
        write_json_atomic(wd.preflight, preflight)
        with repo.transaction():
            if dataset is not None and task.dataset_id != dataset.id:
                repo.update_task_fields(task.id, if_updated_at=None, owner=task.owner_id,
                                        dataset_id=dataset.id)
            repo.freeze_task_inputs(task.id, run_id=run_id, preflight=preflight,
                                    source_fingerprint=rules.fingerprint_of(listing),
                                    vlm_snapshot=snapshot)
        write_json_atomic(wd.start_mark, {"run_id": run_id, "frozen_at": orch.clock(),
                                          "by": actor})
        if not transitions.change_task_state(repo, orch.hub, task.id, {"created"}, "queued",
                                             at=orch.clock(), actor=actor, owner=task.owner_id):
            current = repo.get_task(task.id, owner=task.owner_id)
            raise ApiError("task_state_conflict", "任务的状态变了，没有开始",
                           details={"state": current.state})
        orch.scheduler.enqueue(task.id, owner=task.owner_id)
        return repo.get_task(task.id, owner=task.owner_id)
