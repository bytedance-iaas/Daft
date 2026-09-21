"""The three checks before a task starts (D30; design doc 03, section 3 step 4; 08 §4).

Whatever the key management page says about a key or a backend ("verified", "failed"),
these run for real every time, and a task starts only when all of them pass:

* **input** - read ``meta/info.json`` under the input with the input key (the public cache
  bucket anonymously, a local path from the disk);
* **output** - write a probe object under the delivery directory with the output key and
  delete it again;
* **vlm** - only when a VLM module is selected: one minimal call to the chosen backend and
  model with the effective reasoning effort (none -> no ``reasoning_effort`` field).

Retry, continue, re-export and applying adjudications run the checks that concern them
(``checks=``). A failure becomes ``422 precheck_failed`` with one Chinese reason per check
(:meth:`PrecheckReport.raise_for_failure`); ``details.checks`` lists every check.

W5 entry points: :func:`prechecks_for_task` for a task row, :func:`run_prechecks` for a
configuration that is not stored yet.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import pathlib
import time
from dataclasses import dataclass
from typing import Iterable

from curation.contracts import modules as registry

from ..errors import ApiError
from ..repo import protocol as P
from . import tos as T
from .scrub import Scrubber
from .service import SecretsService, Unavailable

log = logging.getLogger("daemon.secrets")

CHECKS: tuple[str, ...] = ("input", "output", "vlm")
META_INFO = "meta/info.json"
#: What failed, as the head of the sentence in the error message.
_HEAD = {"input": "输入数据集读不了", "output": "交付目录写不进去", "vlm": "模型调用不通"}
#: TosFailure.kind -> the ``code`` of a check result (and of a delivery probe's error)
TOS_CODES = {"auth": "auth_failed", "forbidden": "forbidden", "no_bucket": "not_found",
             "no_object": "not_found", "not_found": "not_found", "unreachable": "unreachable",
             "server": "server_error", "other": "failed", "leftover": "leftover"}
_VLM_CODES = {"auth": "auth_failed", "not_found": "not_found", "rejected": "rejected",
              "rate_limited": "rate_limited", "server": "server_error", "timeout": "timeout",
              "unreachable": "unreachable", "bad_response": "bad_response",
              "unsupported": "failed"}


@dataclass(frozen=True)
class InputTarget:
    source: str                      # tos | public | local
    uri: str
    region: str | None = None
    credential_id: str | None = None


@dataclass(frozen=True)
class OutputTarget:
    uri: str
    region: str | None = None
    credential_id: str | None = None


@dataclass(frozen=True)
class VlmSelection:
    model_id: str | None = None      # before start: the task's model row
    reasoning_effort: str | None = None   # the task-level override (None = the model's setting)
    snapshot: dict | None = None     # after start: task.vlm_snapshot (P17), the key read live


@dataclass(frozen=True)
class CheckResult:
    id: str                          # input | output | vlm
    ok: bool
    code: str                        # ok | leftover | auth_failed | forbidden | not_found | ...
    reason: str                      # Chinese, scrubbed of every secret
    target: str                      # what was checked (address, backend/model); no secrets
    elapsed_ms: int = 0

    def to_json(self) -> dict:
        return {"id": self.id, "ok": self.ok, "code": self.code, "reason": self.reason,
                "target": self.target, "elapsed_ms": self.elapsed_ms}


@dataclass(frozen=True)
class PrecheckReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    def message(self) -> str:
        if self.ok:
            return "开始前的检查都通过了"
        parts = [f"{_HEAD.get(r.id, r.id)}，{r.reason}" for r in self.failures]
        return "开始前的检查没有通过：" + "；".join(parts)

    def details(self) -> dict:
        return {"checks": [r.to_json() for r in self.results]}

    def error(self) -> ApiError:
        return ApiError("precheck_failed", self.message(), details=self.details())

    def raise_for_failure(self) -> None:
        if not self.ok:
            raise self.error()


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

def _tos_result(check: str, failure: T.TosFailure, text: str, target: str) -> CheckResult:
    return CheckResult(check, False, TOS_CODES.get(failure.kind, "failed"), text, target)


def check_input(svc: SecretsService, target: InputTarget, *,
                owner: str = P.DEFAULT_OWNER) -> CheckResult:
    if target.source == "local":
        return _check_local_input(svc, target)
    key = None
    if target.source == "tos":
        try:
            key = svc.tos_key(target.credential_id, owner=owner, role="input")
        except Unavailable as err:
            return CheckResult("input", False, err.code, err.message_zh, target.uri)
    elif target.source != "public":
        return CheckResult("input", False, "unsupported_source",
                           f"不认识的数据来源 {target.source}", target.uri)
    scrub = key.scrubber() if key is not None else Scrubber()
    try:
        bucket, prefix = T.split_uri(target.uri)
    except ValueError as err:
        return CheckResult("input", False, "failed", f"输入地址的写法不对：{err}", target.uri)
    try:
        client, ends = svc.tos_client(key, target.region)
        T.read_object(client, bucket, T.join_key(prefix, META_INFO))
    except Exception as exc:  # noqa: BLE001 - classified below, never re-raised
        ends = svc.tos_endpoints(key, target.region)
        failure = T.classify(exc, scrub)
        if failure.kind == "no_object":
            text = f"{target.uri} 下没有 {META_INFO}：地址不对，或者不是 LeRobot 数据集"
        else:
            text = T.reason(failure, action="读取", uri=target.uri, key=key, region=ends.region,
                            endpoint=ends.server)
        return _tos_result("input", failure, text, target.uri)
    how = f"用{key.label}" if key is not None else "匿名"
    return CheckResult("input", True, "ok", f"{how}读到了 {META_INFO}", target.uri)


def _check_local_input(svc: SecretsService, target: InputTarget) -> CheckResult:
    root = svc.local_data_root
    if root is None:
        return CheckResult("input", False, "unsupported_source",
                           "「本地挂载路径」这个数据来源没有开启", target.uri)
    path = pathlib.Path(os.path.normpath(target.uri)) / META_INFO
    try:
        path.resolve().relative_to(pathlib.Path(root).resolve())
    except ValueError:
        return CheckResult("input", False, "forbidden", f"本地路径必须在 {root} 之下", target.uri)
    try:
        with open(path, "rb") as fh:
            fh.read(1 << 20)
    except FileNotFoundError:
        return CheckResult("input", False, "not_found",
                           f"{target.uri} 下没有 {META_INFO}：路径不对，或者不是 LeRobot 数据集",
                           target.uri)
    except OSError as err:
        return CheckResult("input", False, "failed",
                           f"读不了 {path}：{err.strerror or type(err).__name__}", target.uri)
    return CheckResult("input", True, "ok", f"读到了 {META_INFO}", target.uri)


def check_output(svc: SecretsService, target: OutputTarget, *,
                 owner: str = P.DEFAULT_OWNER) -> CheckResult:
    try:
        key = svc.tos_key(target.credential_id, owner=owner, role="output")
    except Unavailable as err:
        return CheckResult("output", False, err.code, err.message_zh, target.uri)
    try:
        T.split_uri(target.uri)
    except ValueError as err:
        return CheckResult("output", False, "failed", f"交付目录的写法不对：{err}", target.uri)
    try:
        client, ends = svc.tos_client(key, target.region)
        outcome = T.write_probe(client, target.uri, key=key, region=ends.region,
                                endpoint=ends.server)
    except Exception as exc:  # noqa: BLE001
        ends = svc.tos_endpoints(key, target.region)
        failure = T.classify(exc, key.scrubber())
        return _tos_result("output", failure,
                           T.reason(failure, action="写入", uri=target.uri, key=key,
                                    region=ends.region, endpoint=ends.server), target.uri)
    if not outcome.ok:
        return _tos_result("output", outcome.failure, outcome.reason, target.uri)
    if outcome.kind == "leftover":
        return CheckResult("output", True, "leftover", outcome.reason, target.uri)
    return CheckResult("output", True, "ok",
                       f"用{key.label}在交付目录写入并删除了探针对象 {T.PROBE_OBJECT}", target.uri)


def check_vlm(svc: SecretsService, selection: VlmSelection, *,
              owner: str = P.DEFAULT_OWNER) -> CheckResult:
    try:
        if isinstance(selection.snapshot, dict) and selection.snapshot.get("backend_id"):
            target = svc.vlm_target_from_snapshot(selection.snapshot, owner=owner)
        else:
            target = svc.vlm_target(selection.model_id, reasoning_effort=selection.reasoning_effort,
                                    owner=owner)
    except Unavailable as err:
        return CheckResult("vlm", False, err.code, err.message_zh, "")
    where = f"{target.backend}/{target.model}"
    result = svc.vlm.minimal_call(target.endpoint, target.api_key, target.model,
                                  target.reasoning_effort)
    if not result.ok:
        failure = result.failure
        return CheckResult("vlm", False, _VLM_CODES.get(failure.kind, "failed"),
                           f"{target.label}：{failure.reason}", where)
    effort = (f"，带 reasoning_effort={target.reasoning_effort}"
              if target.reasoning_effort is not None else "")
    return CheckResult("vlm", True, "ok", f"{target.label}完成了一次最小调用{effort}", where)


def _timed(fn, *args, **kwargs) -> CheckResult:
    started = time.monotonic()
    result = fn(*args, **kwargs)
    elapsed = int((time.monotonic() - started) * 1000)
    return CheckResult(result.id, result.ok, result.code, result.reason, result.target, elapsed)


def run_prechecks(svc: SecretsService, *, input: InputTarget | None = None,
                  output: OutputTarget | None = None, vlm: VlmSelection | None = None,
                  owner: str = P.DEFAULT_OWNER, parallel: bool = True) -> PrecheckReport:
    """Run the checks that have a target, at the same time; results in input/output/vlm order."""
    jobs = []
    if input is not None:
        jobs.append(("input", check_input, input))
    if output is not None:
        jobs.append(("output", check_output, output))
    if vlm is not None:
        jobs.append(("vlm", check_vlm, vlm))

    def run(job) -> CheckResult:
        check, fn, target = job
        try:
            return _timed(fn, svc, target, owner=owner)
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            # the type only: the message of an unexpected error is not known to be clean
            log.error("precheck %s crashed: %s", check, type(exc).__name__)
            return CheckResult(check, False, "failed",
                               f"检查时出错（{type(exc).__name__}），请稍后重试", "")

    if parallel and len(jobs) > 1:
        with concurrent.futures.ThreadPoolExecutor(len(jobs), thread_name_prefix="precheck") as ex:
            results = list(ex.map(run, jobs))
    else:
        results = [run(job) for job in jobs]
    report = PrecheckReport(tuple(results))
    if not report.ok:
        log.info("prechecks failed: %s", report.message())
    return report


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

def needs_vlm(module_ids: Iterable[str]) -> bool:
    """Whether any of these modules calls a VLM (C1 ``needs`` has ``vlm``)."""
    known = set(registry.ids())
    return any(mid in known and "vlm" in registry.get(mid).needs for mid in module_ids)


def task_needs_vlm(repo: P.Repository, task_id: str) -> bool:
    return needs_vlm(m.module_id for m in repo.get_task_modules(task_id) if m.selected)


def prechecks_for_task(svc: SecretsService, task: P.Task, *, checks: Iterable[str] = CHECKS,
                       need_vlm: bool | None = None) -> PrecheckReport:
    """The checks for a stored task. ``checks`` narrows them (a re-export needs no VLM call);
    the VLM check runs only when a selected module needs a VLM (``need_vlm`` overrides)."""
    wanted = set(checks)
    unknown = wanted - set(CHECKS)
    if unknown:
        raise ValueError(f"unknown checks: {sorted(unknown)}")
    if need_vlm is None:
        need_vlm = task_needs_vlm(svc.repo, task.id)
    inp = out = sel = None
    if "input" in wanted:
        inp = InputTarget(task.input_source, task.input_uri, task.input_region, task.input_cred_id)
    if "output" in wanted:
        out = OutputTarget(task.output_uri, task.output_region, task.output_cred_id)
    if "vlm" in wanted and need_vlm:
        sel = VlmSelection(task.vlm_model_id, task.vlm_reasoning_effort,
                           task.vlm_snapshot if isinstance(task.vlm_snapshot, dict) else None)
    return run_prechecks(svc, input=inp, output=out, vlm=sel, owner=task.owner_id)
