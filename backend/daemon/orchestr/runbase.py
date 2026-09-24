"""What every run shares: stages as CLI processes, progress, logs, results, publishing.

A *run* is the work a worker does for one queue entry: the main run of a task, or
one of its subtasks (retry, resume, apply_adjudication, reexport; design doc 00
§4.1). The main run overlaps batch commands of different funnel stages; other
steps run in plan order. It keeps its place in a journal on the work directory, and
leaves the task in the state the rules say.

Control flow is by exception:

* :class:`Interrupt` - the Daemon asked the run to stop starting work: a pause
  (user or system) or a stop. The command in flight got SIGTERM / SIGINT; what it
  finished is on disk; the journal knows where to go on.
* :class:`TaskFailure` - the task cannot go on (keys gone, source changed, the
  delivery unwritable, a command that crashes again and again, a bug): ``failed``
  with a Chinese reason, and a machine ``code`` kept in the journal.

A whole module failing (exit 4) is neither: that module is ``failed``, its gate is
open, the rest of the stages run (04 §7).
"""
from __future__ import annotations

import collections
import hashlib
import logging
import os
import pathlib
import shutil
import threading
import time
from typing import Any, Callable, Iterable

from curation.contracts import modules as registry

from .. import transitions
from ..exec import CliCommand, CliOutcome, UsageAccumulator, c3
from ..repo import protocol as P
from ..secrets import Unavailable, cli_environment
from . import rules
from .delivery import (DeliveryError, open_delivery, read_latest, sync_run_dir, write_latest)
from .workdir import Journal, WorkDir, read_json, write_json_atomic, write_lines

log = logging.getLogger("daemon.orchestr")

ALL_MODULE_STATES = frozenset({"pending", "running", "succeeded", "completed_with_errors",
                               "failed", "skipped", "stale"})
_STAGE_KEYS = ("id", "state", "done", "total", "elapsed_s", "eta_s", "note", "pipeline")
_FINAL_STAGE_STATES = ("succeeded", "completed_with_errors", "failed", "skipped")
#: the child a run has in flight (pid = its process group), for reaping after a crash
PROC_FILE = "proc.json"
#: the state reason of a system pause at shutdown; the timeline reads "被系统暂停：…，将自动恢复"
SHUTDOWN_REASON = "Daemon 停机"


class Interrupt(Exception):
    def __init__(self, intent: str):
        super().__init__(intent)
        self.intent = intent


class TaskFailure(Exception):
    def __init__(self, code: str, reason_zh: str, *, stage: str | None = None):
        super().__init__(f"{code}: {reason_zh}")
        self.code = code
        self.reason_zh = reason_zh
        self.stage = stage


def input_digest(episodes: Iterable[int]) -> str:
    """The digest ``curation check`` reports for an episode set (``input_digest``)."""
    text = "".join(f"{int(e)}\n" for e in sorted({int(x) for x in episodes}))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class Run:
    """Base of the four kinds of work. Subclasses implement :meth:`execute`."""

    kind = "main"

    def __init__(self, orch, task: P.Task, subtask: P.Subtask | None = None):
        self.orch = orch
        self.repo: P.Repository = orch.repo
        self.hub = orch.hub
        self.clock: Callable[[], int] = orch.clock
        self.cfg = orch.cfg
        self.task = task
        self.task_id = task.id
        self.owner = task.owner_id
        self.subtask = subtask
        self.sub_id = subtask.id if subtask is not None else None
        self.wd = WorkDir(orch.work_root, task.id)
        self.wd.ensure()
        self.run_key = self.journal_key()
        self.journal = Journal(self.wd.journal(self.run_key))
        self.usage = UsageAccumulator(self.repo, task.id, subtask_id=self.sub_id or "",
                                      clock=self.clock, every_s=self.cfg.usage_flush_s,
                                      publish=lambda totals: self.hub.publish_usage(task.id, totals))
        self.stages: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._stage_started: dict[str, float] = {}
        self._last_progress_write = 0.0
        self._lock = threading.Lock()
        self._progress_lock = threading.RLock()
        self._intent: str | None = None
        self._procs: dict[int, Any] = {}
        self._pipeline_abort: threading.Event | None = None

    # ------------------------------------------------------------------ identity
    def journal_key(self) -> str:
        return self.sub_id or "main"

    @property
    def log_subtask(self) -> str | None:
        return self.sub_id

    def reload(self) -> P.Task:
        self.task = self.repo.get_task(self.task_id, owner=self.owner, include_deleted=True)
        return self.task

    # ------------------------------------------------------------------ intents
    def request(self, intent: str) -> None:
        """``pause`` / ``shutdown`` (SIGTERM) or ``stop`` (SIGINT) - from any thread."""
        with self._lock:
            if intent == "stop" or self._intent is None:
                self._intent = intent
            procs = list(self._procs.values())
            current = self._intent
        for proc in procs:
            if current == "stop":
                proc.interrupt()
            else:
                proc.terminate()

    @property
    def intent(self) -> str | None:
        with self._lock:
            return self._intent

    def check_intent(self) -> None:
        intent = self.intent
        if intent is not None:
            raise Interrupt(intent)
        if self._pipeline_abort is not None and self._pipeline_abort.is_set():
            raise TaskFailure("pipeline_cancelled", "流水线已停止，等待上游错误处理")

    def terminate_children(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            proc.terminate()

    def _attach(self, proc) -> None:
        with self._lock:
            self._procs[proc.pid] = proc
            intent = self._intent
            self._write_procs()
        if intent == "stop":
            proc.interrupt()
        elif intent is not None:
            proc.terminate()

    def _write_procs(self) -> None:
        """Call with ``_lock`` held."""
        path = self.wd.private / PROC_FILE
        if not self._procs:
            try:
                path.unlink()
            except OSError:
                pass
            return
        rows = [{"pid": p.pid, "stage": p.cmd.stage, "command": p.cmd.argv[:1],
                 "started_at": self.clock()} for p in self._procs.values()]
        try:
            write_json_atomic(path, {**rows[0], "processes": rows})
        except OSError:
            pass

    def _detach(self, proc) -> None:
        with self._lock:
            self._procs.pop(proc.pid, None)
            self._write_procs()

    # ------------------------------------------------------------------ logs
    def log(self, stage: str, level: str, msg: str, **extra: Any) -> None:
        line = c3.log_line(level, msg, **extra)
        try:
            self.orch.logs.append(self.task_id, stage, line, subtask_id=self.log_subtask)
        except (OSError, ValueError) as err:
            log.warning("could not write a log line of %s: %s", self.task_id, err)
        self.hub.publish_log(self.task_id, stage, line["level"], line["msg"],
                             subtask_id=self.sub_id, episode_index=extra.get("episode_index"))

    def _line_handler(self, stage: str, *, progress_offset: int = 0,
                      progress_total: int | None = None) -> Callable[[str], None]:
        def handle(raw: str) -> None:
            ev = c3.parse(raw)
            try:
                self.orch.logs.append(self.task_id, stage, ev, subtask_id=self.log_subtask)
            except (OSError, ValueError):
                pass
            kind = ev["kind"]
            if kind == "log":
                self.hub.publish_log(self.task_id, stage, ev["level"], ev["msg"],
                                     subtask_id=self.sub_id,
                                     episode_index=ev.get("episode_index"))
            elif kind == "progress":
                self.progress(stage, done=progress_offset + ev["done"],
                              total=progress_total if progress_total is not None else ev["total"],
                              eta_s=ev.get("eta_s"))
            elif kind == "usage":
                self.usage.add(ev)
            elif kind == "throttle":                 # a log line too: the logs page keeps it
                msg = (f"模型服务 {ev['backend']} 限流：并发降到 {ev['limit']}（{ev['reason']}）")
                self.log(stage, "warn", msg)
                log.warning("task %s: %s", self.task_id, msg)
        return handle

    # ------------------------------------------------------------------ progress
    def plan_progress(self, stage_ids: Iterable[str]) -> None:
        """The stage list of this run; done stages keep what the journal says about them."""
        self.stages.clear()
        for sid in stage_ids:
            kept = self.journal.stage(sid).get("progress")
            if isinstance(kept, dict) and self.journal.done(sid):
                self.stages[sid] = {k: kept[k] for k in _STAGE_KEYS if k in kept}
            else:
                self.stages[sid] = {"id": sid, "state": "pending", "done": 0, "total": 0,
                                    "elapsed_s": None, "eta_s": None}
        self.persist_progress(force=True)

    def progress(self, stage: str, *, state: str | None = None, done: int | None = None,
                 total: int | None = None, eta_s: float | None = None, note: str | None = None,
                 force: bool = False, pipeline: dict | None = None) -> None:
        with self._progress_lock:
            self._progress_unlocked(stage, state=state, done=done, total=total,
                                    eta_s=eta_s, note=note, force=force, pipeline=pipeline)

    def _progress_unlocked(self, stage: str, *, state: str | None = None,
                           done: int | None = None, total: int | None = None,
                           eta_s: float | None = None, note: str | None = None,
                           force: bool = False, pipeline: dict | None = None) -> None:
        entry = self.stages.get(stage)
        if entry is None:
            entry = self.stages[stage] = {"id": stage, "state": "pending", "done": 0, "total": 0,
                                          "elapsed_s": None, "eta_s": None}
        changed = False
        if state is not None and state != entry.get("state"):
            entry["state"] = state
            changed = True
            if state == "running":
                self._stage_started[stage] = time.monotonic()
                entry["eta_s"] = None
        if done is not None:
            entry["done"] = int(done)
        if total is not None:
            entry["total"] = int(total)
        if eta_s is not None:
            entry["eta_s"] = round(float(eta_s), 1)
        if note is not None:
            entry["note"] = note
        if pipeline is not None:
            entry["pipeline"] = pipeline
        if stage in self._stage_started:
            entry["elapsed_s"] = round(time.monotonic() - self._stage_started[stage], 1)
        if entry.get("state") in _FINAL_STAGE_STATES:
            entry["eta_s"] = None
        self.hub.publish_progress(self.task_id, dict(entry))
        self.persist_progress(force=force or changed)

    def persist_progress(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress_write < self.cfg.progress_write_s:
            return
        self._last_progress_write = now
        doc = {"stages": [{k: v for k, v in e.items() if k in _STAGE_KEYS}
                          for e in self.stages.values()]}
        try:
            self.write_progress(doc)
        except Exception:  # noqa: BLE001 - progress is advisory; never fail a run on it
            log.warning("could not store the progress of %s", self.task_id, exc_info=True)

    def write_progress(self, doc: dict) -> None:
        if self.sub_id:
            self.repo.set_subtask_progress(self.sub_id, doc)
        else:
            self.repo.set_task_progress(self.task_id, doc)

    def stage_done(self, stage: str, state: str, **journal_fields: Any) -> None:
        entry = self.stages.get(stage) or {}
        if state in ("succeeded", "completed_with_errors") and entry.get("total") \
                and entry.get("done", 0) < entry["total"]:
            entry["done"] = entry["total"]
        self.progress(stage, state=state, force=True)
        self.usage.flush()
        self.journal.mark(stage, done=True, state=state,
                          progress={k: v for k, v in self.stages[stage].items()
                                    if k in _STAGE_KEYS}, **journal_fields)

    # ------------------------------------------------------------------ the CLI
    def _cli_environment(self, *, need_input: bool, need_output: bool, need_vlm: bool):
        try:
            return cli_environment(self.orch.svc, self.task, need_input=need_input,
                                   need_output=need_output, need_vlm=need_vlm)
        except Unavailable as err:
            raise TaskFailure(err.code, err.message_zh) from None

    def env(self, *, need_input: bool, need_output: bool, need_vlm: bool) -> dict:
        return dict(self._cli_environment(need_input=need_input, need_output=need_output,
                                          need_vlm=need_vlm).env)

    def cli(self, stage: str, argv: list[str], *, need_input: bool = True,
            need_output: bool = False, need_vlm: bool = False, crash_retry: bool = False,
            inflight_modules: Iterable[str] = (), extra_env: dict | None = None,
            timeout_s: float | None = None, episodes: int = 0,
            progress_offset: int = 0, progress_total: int | None = None) -> CliOutcome:
        """Run one command of ``stage``; relaunch it with ``--resume`` after a crash.

        A command killed by a native crash (or the OOM killer) is started again with
        ``--resume``: the CLI counts the episodes that were in flight, and one found
        twice in a dead process's hands is recorded as an error and skipped (P14), so
        the loop always ends. The Daemon names those episodes in the task log.
        """
        blank = crashes = 0
        argv = list(argv)
        while True:
            self.check_intent()
            cli = self._cli_environment(need_input=need_input, need_output=need_output,
                                        need_vlm=need_vlm)
            env = dict(cli.env)
            if need_input:
                env.update(self.source_env())
            if extra_env:
                env.update(extra_env)
            full = list(argv)
            if need_input and cli.input_region:
                full += ["--input-region", cli.input_region]
            if need_output and cli.output_region:
                full += ["--output-region", cli.output_region]
            if need_vlm and cli.vlm_api_key_env:
                full += ["--vlm-api-key-env", cli.vlm_api_key_env]
            cmd = CliCommand(full, env=env, stage=stage, cwd=str(self.wd.root),
                             timeout_s=timeout_s)
            self.log(stage, "debug", f"run: {cmd.describe()}")
            attached = []
            def on_start(proc):
                self._attach(proc)
                attached.append(proc)
            try:
                outcome = self.orch.executor.run(cmd, on_line=self._line_handler(
                    stage, progress_offset=progress_offset, progress_total=progress_total),
                                                 on_start=on_start)
            finally:
                for proc in attached:
                    self._detach(proc)
            self.usage.flush()
            asked = outcome.requested is not None and self.intent is not None
            if asked and outcome.status in ("terminated", "interrupted", "killed", "crashed"):
                raise Interrupt(self.intent or "pause")
            crashed = outcome.status == "crashed" or (
                outcome.status in ("terminated", "interrupted", "killed") and not asked)
            if not crashed:
                if outcome.status == "timeout":
                    raise TaskFailure("timeout", f"命令超时没有结束（{cmd.describe()}）", stage=stage)
                return outcome
            crashes += 1
            inflight = self.inflight(inflight_modules)
            if not crash_retry:
                raise TaskFailure("crashed", f"命令异常退出（{outcome.reason()}）：{cmd.describe()}",
                                  stage=stage)
            if inflight:
                blank = 0
                self.log(stage, "error",
                         f"进程异常退出（{outcome.reason()}），当时在处理 episode "
                         f"{', '.join(map(str, inflight))}；带 --resume 重新拉起。"
                         "同一条连续两次出现在崩溃现场会被记为出错并跳过")
            else:
                blank += 1
                self.log(stage, "error", f"进程异常退出（{outcome.reason()}），当时手上没有 episode；"
                                         f"带 --resume 重新拉起（第 {blank} 次）")
            if blank > self.cfg.blank_crash_limit or crashes > max(10, 2 * episodes + 5):
                raise TaskFailure("crashed", f"进程反复崩溃（{outcome.reason()}），任务没法继续",
                                  stage=stage)
            if "--resume" not in argv:
                argv.append("--resume")

    def inflight(self, modules: Iterable[str]) -> list[int]:
        found: set[int] = set()
        for m in modules:
            doc = read_json(self.wd.root / "checks" / m / "inflight.json", None)
            if isinstance(doc, dict):
                found |= {int(e) for e in doc.get("episodes") or [] if isinstance(e, int)}
        return sorted(found)

    def fail_on(self, outcome: CliOutcome, stage: str) -> None:
        """Turn an outcome that is not ok into :class:`TaskFailure` (02 §4)."""
        msg = outcome.message or outcome.reason()
        if outcome.status == "unreachable":
            if outcome.error_code == "output_unreachable":
                raise TaskFailure("output_unreachable", f"读写不了交付目录：{msg}", stage=stage)
            raise TaskFailure("input_unreachable", f"读不到输入数据集：{msg}", stage=stage)
        if outcome.status == "source_changed":
            raise TaskFailure("source_changed",
                              f"源数据在任务开始之后变了（{msg}）：一个任务不混用两个版本的数据，"
                              "请重新预检后另建任务", stage=stage)
        if outcome.status == "module_failed":
            raise TaskFailure("module_failed", f"这一步没法完成：{msg}", stage=stage)
        raise TaskFailure("internal", f"命令出错（{outcome.reason()}）：{msg}；这是平台的问题，"
                                      "请联系管理员", stage=stage)

    # ------------------------------------------------------------------ arguments
    @property
    def preflight(self) -> dict:
        return self.task.preflight if isinstance(self.task.preflight, dict) else {}

    def episode_count(self) -> int:
        ds = self.preflight.get("dataset") or {}
        return int(ds.get("episode_count") or 0)

    def selection(self) -> list[int]:
        """The task's episodes, without the ones whose source files are missing (D40)."""
        chosen = rules.selected_episodes(self.task.episode_selector, self.episode_count())
        skipped = self.skipped_episodes()
        return [e for e in chosen if e not in skipped] if skipped else chosen

    def skipped_episodes(self) -> set[int]:
        """``skipped_episodes`` of the source manifest: missing parquet or video (C2 1.4)."""
        doc = read_json(self.wd.manifest, {}) or {}
        return {int(e["episode_index"]) for e in doc.get("skipped_episodes") or []
                if isinstance(e, dict) and isinstance(e.get("episode_index"), int)}

    def source_args(self, *, manifest: bool = True, semantics: bool = True) -> list[str]:
        t = self.task
        out = ["--input", t.input_uri, "--source", t.input_source]
        if manifest:
            out += ["--source-manifest", str(self.wd.manifest)]
        if semantics:
            if t.embodiment_id:
                out += ["--embodiment-id", t.embodiment_id]
            n = rules.max_episodes(t.episode_selector)
            if n:
                out += ["--max-episodes", str(n)]
            if self.container_kind():
                # mcap / lance (D44): v1 resolves their semantics on the first 100 episodes of
                # the whole selection, whichever stage's survivors a command reads
                out += ["--selection", self.episodes_arg("selection", self.selection())]
        return out

    # -- mcap / lance (D44) ----------------------------------------------------------
    def container_kind(self) -> str | None:
        from .datasets import container_kind

        return container_kind(self.preflight)

    def source_cache_dir(self) -> pathlib.Path:
        """The task's local copy of a remote mcap / lance dataset, on the data volume."""
        return pathlib.Path(self.orch.settings.source_cache_dir) / self.task_id

    def source_env(self) -> dict:
        """What every CLI command of a run on an mcap / lance dataset gets: the task's
        source cache (a remote dataset's local copy, kept across the run's commands,
        removed when the run ends) and a temporary directory next to it for the readers'
        muxed / extracted videos (the data volume, not the container's /tmp). Empty for
        LeRobot, whose commands run exactly as before."""
        if not self.container_kind():
            return {}
        root = self.source_cache_dir()
        (root / "tmp").mkdir(parents=True, exist_ok=True)
        return {"CURATION_SOURCE_CACHE": str(root), "TMPDIR": str(root / "tmp")}

    def drop_source_cache(self) -> None:
        root = self.source_cache_dir()
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
            log.info("task %s: source cache %s removed", self.task_id, root)

    def vlm_args(self) -> list[str]:
        snap = self.task.vlm_snapshot if isinstance(self.task.vlm_snapshot, dict) else None
        if not snap or not snap.get("endpoint") or not snap.get("model"):
            raise TaskFailure("vlm_missing", "这个任务要调用模型，但启动时没有固化模型配置：请复制为新任务")
        params = self.task.params or {}
        out = ["--vlm-endpoint", str(snap["endpoint"]), "--vlm-model", str(snap["model"]),
               "--retry", str(int(params.get("vlm_retry", 3)))]
        if params.get("vlm_hedge", True):
            out.append("--hedge")
        if snap.get("reasoning_effort"):
            # only when there is one: v1 never sends the field, parity depends on it (C2 1.4)
            out += ["--vlm-reasoning-effort", str(snap["reasoning_effort"])]
        for kind, seconds in sorted((params.get("vlm_timeouts_s") or {}).items()):
            out += ["--set", f"checks.task_success.vlm.timeouts_s.{kind}={float(seconds):g}"]
        return out

    def plan_doc(self) -> dict:
        doc = read_json(self.wd.plan, None)
        if not isinstance(doc, dict) or not doc.get("stages"):
            raise TaskFailure("plan_missing", "执行计划丢了（工作目录不完整）：请复制为新任务")
        return doc

    def plan_modules(self, plan: dict) -> list[str]:
        mods: list[str] = []
        for st in plan.get("stages") or []:
            mods += [m for m in st.get("modules") or [] if m not in mods]
        return [m for m in registry.ids() if m in mods]

    def episodes_arg(self, name: str, episodes: Iterable[int]) -> str:
        path = write_lines(self.wd.episodes_file(self.run_key, name), episodes)
        return "@" + str(path)

    # ------------------------------------------------------------------ modules
    def module_rows(self) -> dict[str, P.TaskModule]:
        return {m.module_id: m for m in self.repo.get_task_modules(self.task_id)}

    def modules_running(self, modules: Iterable[str]) -> None:
        now = self.clock()
        for m in modules:
            self.repo.update_module_state(self.task_id, m, ALL_MODULE_STATES, "running",
                                          started_at=now, finished_at=None, error=None)

    def module_result(self, module: str, state: str, *, total: int, errors: int,
                      digest: str | None = None, error: str | None = None) -> None:
        fields: dict[str, Any] = {"episodes_total": int(total), "episodes_error": int(errors),
                                  "finished_at": self.clock(), "error": error}
        if digest is not None:
            fields["input_digest"] = digest
        self.repo.update_module_state(self.task_id, module, ALL_MODULE_STATES, state, **fields)

    def counts_from_records(self, module: str) -> tuple[int, int]:
        """(episodes with a result, of which errors) of a funnel module, over the task's episodes."""
        from curation.pipeline.records import latest_results

        selected = set(self.selection())
        recs = {e: r for e, r in latest_results(str(self.wd.root), module).items()
                if e in selected}
        return len(recs), sum(1 for r in recs.values() if r.get("verdict") == "error")

    # ------------------------------------------------------------------ revisions
    def allocate_revision(self) -> int:
        """The revision this run writes: kept in the journal, so a resumed run keeps it."""
        from curation.pipeline.records import committed_revisions

        rev = self.journal.get("revision")
        if isinstance(rev, int) and rev >= 1:
            return rev
        rev = max([int(self.task.result_rev or 0), *committed_revisions(str(self.wd.root))]) + 1
        leftover = self.wd.revision_dir(rev)
        if leftover.is_dir() and not (leftover / "commit.json").is_file():
            shutil.rmtree(leftover, ignore_errors=True)       # an earlier run gave up on it
        self.journal.set(revision=rev)
        return rev

    def committed(self, rev: int) -> bool:
        return (self.wd.revision_dir(rev) / "commit.json").is_file()

    # ------------------------------------------------------------------ publishing
    def export_format(self) -> str:
        kind = self.container_kind()
        if kind:
            return kind                                  # mcap / lance (D44)
        version = (self.preflight.get("format") or {}).get("version")
        return "lerobot_v3" if version == "v3" else "lerobot_v2"

    def source_digest(self) -> str:
        doc = read_json(self.wd.manifest, {}) or {}
        return str((doc.get("summary") or {}).get("digest") or "")

    def current_fingerprint(self, rev: int) -> str | None:
        from curation.export.manifest import export_fingerprint

        passed = rules.read_list(self.wd.revision_dir(rev), "passed")
        if passed is None:
            return None
        return export_fingerprint(passed.get("episodes") or [], source_format=self.export_format(),
                                  source_digest=self.source_digest())

    def delivery(self):
        """``with self.delivery() as d:`` - the task's delivery directory."""
        key = None
        if self.cfg.local_delivery_root is None:
            try:
                key = self.orch.svc.tos_key(self.task.output_cred_id, owner=self.owner,
                                            role="output")
            except Unavailable as err:
                raise TaskFailure(err.code, err.message_zh) from None
        region = self.task.output_region or (key.region if key is not None else None)
        return open_delivery(self.task.output_uri, local_root=self.cfg.local_delivery_root,
                             svc=self.orch.svc, key=key, region=region)

    def ensure_local(self) -> None:
        """A subtask on a task whose work directory the janitor cleaned (7 days after the
        end) first brings back from the delivery what it builds on (00 §4.2)."""
        restorer = self.orch.restorer
        if not restorer.needed(self.task):
            return
        where = f"{self.task.output_uri.rstrip('/')}/{self.task.run_id}"
        self.log("system", "info", f"本地工作目录已清理，从交付目录 {where}/ 取回（大文件不取回）")
        try:
            n = restorer.restore(self.task, check_stop=self.check_intent)
        except DeliveryError as err:
            raise TaskFailure("output_unreachable",
                              f"本地工作目录已清理，从交付目录取回时出错：{err.message_zh}：{err.cause}"
                              ) from None
        self.log("system", "info", f"从交付目录取回 {n} 个文件")
        if self.task.result_rev and not (self.wd.revision_dir(int(self.task.result_rev))
                                         / "commit.json").is_file():
            raise TaskFailure("no_result", f"交付目录 {where}/ 里没有结果版本 "
                                           f"r{int(self.task.result_rev):04d}（可能已被清理），没法接着做")
        if not self.wd.manifest.is_file():
            raise TaskFailure("workdir_incomplete", f"交付目录 {where}/ 里也没有源文件清单，"
                                                    "没法接着做：请复制为新任务")

    def export(self, stage: str, delivery, rev: int, *, incremental: bool) -> dict:
        self.progress(stage, state="running", done=0, total=0)
        run_uri = delivery.cli_uri(self.task.run_id)
        scratch = pathlib.Path(self.orch.settings.scratch_dir) / self.task_id
        shutil.rmtree(scratch, ignore_errors=True)         # what a killed export left behind
        (scratch / "tmp").mkdir(parents=True, exist_ok=True)
        argv = ["export", "--run-dir", str(self.wd.root),
                *self.source_args(manifest=True, semantics=False), "--revision", str(rev),
                "--output", run_uri, "--scratch", str(scratch)]
        if incremental:
            argv.append("--incremental")
        try:
            outcome = self.cli(stage, argv, need_input=True, need_output=True,
                               extra_env={"CURATION_EXPORT_SCRATCH": str(scratch),
                                          "TMPDIR": str(scratch / "tmp")})
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not outcome.ok:
            self.fail_on(outcome, stage)
        doc = outcome.doc
        d = doc.get("diff") or {}
        self.log(stage, "info",
                 f"导出 {doc.get('episodes')} 条（{'增量' if doc.get('incremental') else '全量'}）："
                 f"保留 {d.get('keep', 0)}、改标 {d.get('relabel', 0)}、改号 {d.get('renumber', 0)}、"
                 f"新增 {d.get('add', 0)}、剔除 {d.get('drop', 0)}"
                 + (f"；交付数据集在 {doc['dataset_dir']}/" if doc.get("dataset_dir") else "")
                 + (f"；退回全量的原因：{doc['full_reason']}" if doc.get("full_reason") else ""))
        if doc.get("note"):                                # lance (D44): no native delivery yet
            self.log(stage, "info", doc["note"])
        return doc

    def check_stop(self) -> None:
        """Only a stop cuts short work in hand; a pause lets it finish (it starts nothing new)."""
        if self.intent == "stop":
            raise Interrupt("stop")

    def sync_quietly(self, stage: str, *, through_pause: bool = False) -> None:
        """What a stage produced goes to the delivery at once (00 §4.2, "随产随传").

        Only the main run does this: before its first publish the batch has no
        ``_COMPLETE`` that a half-synced change could contradict. A failure here costs
        nothing but time - the publish step syncs and verifies everything anyway.
        ``through_pause``: a finished stage's upload running next to later stages (the
        pipelined funnel) completes when a pause comes in - a paused task has its finished
        stages delivered; only a stop cuts it short.
        """
        if not self.task.run_id:
            return
        try:
            with self.delivery() as d:
                sync_run_dir(d, self.task.run_id, self.wd.root, self.wd.sync_state,
                             check_stop=self.check_stop if through_pause else self.check_intent)
        except Interrupt:
            raise
        except (DeliveryError, TaskFailure, OSError) as err:
            reason = err.reason_zh if isinstance(err, TaskFailure) else str(err)
            self.log(stage, "warn", f"这一档的产物暂时没传到交付目录（{reason}），发布时再传")

    def sync_and_verify(self, stage: str, delivery) -> dict:
        """Upload the run directory, read it back (``curation verify``); twice at most."""
        run_id = self.task.run_id
        for attempt in (1, 2):
            self.progress(stage, state="running", done=0, total=0,
                          note="上传运行目录" if attempt == 1 else "核验没通过，重新上传后再核验")
            try:
                sync_run_dir(delivery, run_id, self.wd.root, self.wd.sync_state,
                             check_stop=self.check_intent,
                             progress=lambda n, t: self.progress(stage, done=n, total=t))
            except DeliveryError as err:
                raise TaskFailure("output_unreachable", f"{err.message_zh}：{err.cause}",
                                  stage=stage) from None
            argv = ["verify", "--run-dir", str(self.wd.root), "--output",
                    delivery.cli_uri(run_id),
                    "--visibility-timeout", f"{self.cfg.verify_visibility_s:g}"]
            outcome = self.cli(stage, argv, need_input=False, need_output=True)
            if not outcome.ok:
                self.fail_on(outcome, stage)
            doc = outcome.doc
            if not doc.get("failed") and doc.get("complete_marker"):
                self.stages.get(stage, {}).pop("note", None)
                self.wd.purged_mark.unlink(missing_ok=True)   # the batch is there again
                return doc
            bad = doc.get("failed") or []
            shown = "、".join(f"{f.get('path')}（{f.get('reason')}）" for f in bad[:5])
            self.log(stage, "warn", f"交付核验有 {len(bad)} 个文件没通过：{shown}")
            from .delivery import forget_sync

            forget_sync(self.wd.sync_state)                   # upload everything again
        raise TaskFailure("verify_failed", f"交付核验没有通过：{shown}", stage=stage)

    def switch_revision(self, rev: int) -> None:
        """CAS ``result_rev`` to ``rev``, audit it; readers see the old or the new one (D25)."""
        self.reload()
        if int(self.task.result_rev or 0) >= rev:
            return                                              # a resumed run did it already
        if not self.repo.switch_result_rev(self.task_id, int(self.task.result_rev or 0), rev):
            raise TaskFailure("internal", "结果版本切换冲突：另一个进程改了这个任务的结果版本")
        transitions.record_revision(self.repo, self.task_id, rev, at=self.clock(),
                                    subtask_id=self.sub_id, owner=self.owner)
        if self.sub_id:
            self.repo.set_subtask_result_rev(self.sub_id, rev)
        self.reload()

    def refresh_results(self, rev: int) -> None:
        """Summary and ``delivery_stale`` after a revision switched (01 §2.3).

        The summary is the result readers' (W5b ``refresh_summary``): its
        ``pending_adjudication`` counts the adjudication queue the way the queue does.
        """
        from ..results import refresh_summary, store_of

        try:
            refresh_summary(store_of(self.orch.rt), self.repo, self.task_id, owner=self.owner)
        except Exception:  # noqa: BLE001 - the counts must follow the revision regardless
            log.warning("summary of task %s not refreshed by the result readers; counting "
                        "the lists of r%04d", self.task_id, rev, exc_info=True)
            self.repo.set_task_summary(self.task_id, rules.summary(self.wd.revision_dir(rev)))
        current = self.current_fingerprint(rev)
        self.reload()
        stale = current is None or current != self.task.export_fingerprint
        self.repo.set_export_fingerprint(self.task_id, self.task.export_fingerprint, stale)
        self.reload()

    def held_count(self, rev: int) -> int:
        doc = rules.read_list(self.wd.revision_dir(rev), "held")
        return int((doc or {}).get("count") or 0)

    def recomputed_state(self, rev: int | None = None) -> str:
        rev = rev or int(self.reload().result_rev or 0)
        held = self.held_count(rev) if rev else 0
        return rules.terminal_state(self.repo.get_task_modules(self.task_id), held)

    def maybe_latest(self, delivery, stage: str) -> None:
        """Move ``latest`` to this batch when it is complete (06 §1, P13)."""
        self.reload()
        if self.task.delivery_stale or not self.task.export_fingerprint:
            return
        if self.recomputed_state() != "succeeded":
            return
        try:
            if read_latest(delivery) != self.task.run_id:
                write_latest(delivery, self.task.run_id)
                self.log(stage, "info", f"latest 指向 {self.task.run_id}")
        except DeliveryError as err:
            raise TaskFailure("output_unreachable", f"{err.message_zh}：{err.cause}",
                              stage=stage) from None

    # ------------------------------------------------------------------ run it
    def execute(self) -> str:
        """Do the work; return the task's (recomputed) terminal state for the end transition."""
        raise NotImplementedError

    def close(self) -> None:
        try:
            self.usage.flush()
        except Exception:  # noqa: BLE001
            log.warning("could not store the token usage of %s", self.task_id, exc_info=True)
        self.persist_progress(force=True)
        try:
            self.drop_source_cache()          # D44: the next run fetches what it needs again
        except Exception:  # noqa: BLE001 - the janitor sweeps what is left
            log.warning("could not remove the source cache of %s", self.task_id, exc_info=True)
