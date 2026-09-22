"""The queue and the worker pool (design doc 04 §2.3, 01 §3).

* ``maxRunningTasks`` slots (default 1, P1); main runs and subtasks share them: a
  retry of task A and the main run of task B never run at the same time.
* First in, first out, by the time an entry became ``queued``; after a restart the
  queue is rebuilt from the database (``queued`` tasks and subtasks), so the
  startup reconciliation's "system paused -> queued" entries run again on their own.
* A slot runs one :class:`Job`: ``queued -> running`` by CAS (a stop that got there
  first wins), the run, then the end state - also by CAS, so a pause or stop that
  arrives while the run finishes is honoured instead of overwritten.
* A task is only run when the start procedure prepared it (``.orchestr/start.json``
  in its work directory: frozen inputs, source manifest). Anything else is left
  alone with a warning - never failed, never guessed.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Iterable

from .. import transitions
from ..repo import protocol as P
from .runbase import ALL_MODULE_STATES, Interrupt, TaskFailure
from .runs import run_for
from .workdir import WorkDir

log = logging.getLogger("daemon.orchestr")

_Key = tuple[str, str | None]            # (task id, subtask id)


class Job:
    """One queue entry being worked on: the run and its end transition."""

    def __init__(self, orch, key: _Key, owner: str):
        self.orch = orch
        self.key = key
        self.task_id, self.sub_id = key
        self.owner = owner
        self.run = None
        self.thread: threading.Thread | None = None
        self._pending: list[str] = []
        self._lock = threading.Lock()

    # -- signals from the API or a shutdown -----------------------------------------
    def request(self, intent: str) -> None:
        with self._lock:
            run = self.run
            if run is None:
                self._pending.append(intent)
                return
        run.request(intent)

    # -- claim --------------------------------------------------------------------------
    def claim(self) -> bool:
        repo, now = self.orch.repo, self.orch.clock()
        if self.sub_id is None:
            if not WorkDir(self.orch.work_root, self.task_id).started():
                log.warning("task %s is queued but was not prepared by the start procedure "
                            "(no .orchestr/start.json); left alone", self.task_id)
                return False
            return transitions.change_task_state(repo, self.orch.hub, self.task_id, {"queued"},
                                                 "running", at=now, owner=self.owner)
        return transitions.change_subtask_state(repo, self.orch.hub, self.sub_id, {"queued"},
                                                "running", at=now, owner=self.owner)

    # -- the work -------------------------------------------------------------------------
    def execute(self) -> None:
        repo = self.orch.repo
        try:
            task = repo.get_task(self.task_id, owner=self.owner, include_deleted=True)
            sub = repo.get_subtask(self.sub_id) if self.sub_id else None
            run = run_for(self.orch, task, sub)
        except Exception as err:  # noqa: BLE001 - never leave the entry running
            log.exception("could not set up the run of %s", self.key)
            self._end("failed", failure=TaskFailure("internal", f"编排出错（{type(err).__name__}）"))
            return
        with self._lock:
            self.run = run
            pending, self._pending = self._pending, []
        for intent in pending:
            run.request(intent)
        state_now = (sub.state if sub is not None else task.state)
        if state_now == "pausing":
            run.request("pause")
        elif state_now == "stopping":
            run.request("stop")
        modules_before = {m.module_id: m for m in repo.get_task_modules(self.task_id)}
        outcome: tuple = ("done", None)
        try:
            outcome = ("done", run.execute())
        except Interrupt as it:
            outcome = ("interrupt", it.intent)
        except TaskFailure as failure:
            outcome = ("failed", failure)
        except Exception as err:  # noqa: BLE001 - a bug must not leave the task running
            log.exception("run %s of task %s crashed", run.kind, self.task_id)
            outcome = ("failed", TaskFailure("internal",
                                             f"编排出错（{type(err).__name__}: {err}）"[:500]))
        finally:
            run.close()
        if outcome[0] != "done":
            self._restore_modules(modules_before)
        if outcome[0] == "failed":
            failure = outcome[1]
            run.journal.set(failure={"code": failure.code, "reason": failure.reason_zh,
                                     "stage": failure.stage})
            run.log(failure.stage or "system", "error", failure.reason_zh)
            if failure.stage and failure.stage in run.stages:
                run.progress(failure.stage, state="failed", force=True)
        elif outcome[0] == "done":
            run.journal.set(failure=None)
        self._end(outcome[0], state=outcome[1] if outcome[0] == "done" else None,
                  intent=outcome[1] if outcome[0] == "interrupt" else None,
                  failure=outcome[1] if outcome[0] == "failed" else None, run=run)

    def _restore_modules(self, before: dict[str, P.TaskModule]) -> None:
        """A run that did not finish leaves no module ``running``: back to what it was."""
        repo = self.orch.repo
        for m in repo.get_task_modules(self.task_id):
            if m.state != "running":
                continue
            old = before.get(m.module_id)
            state = old.state if old is not None and old.state != "running" else "pending"
            fields = {"started_at": old.started_at, "finished_at": old.finished_at,
                      "error": old.error} if old is not None else {}
            repo.update_module_state(self.task_id, m.module_id, {"running"}, state, **fields)

    # -- the end transition -----------------------------------------------------------------
    def _end(self, kind: str, *, state: str | None = None, intent: str | None = None,
             failure: TaskFailure | None = None, run=None) -> None:
        if self.sub_id is None:
            self._end_task(kind, state, intent, failure)
        else:
            self._end_subtask(kind, state, intent, failure)

    def _step(self, frm: Iterable[str], to: str, **kw) -> bool:
        return transitions.change_task_state(self.orch.repo, self.orch.hub, self.task_id,
                                             set(frm), to, at=self.orch.clock(),
                                             owner=self.owner, **kw)

    def _sub_step(self, frm: Iterable[str], to: str, **kw) -> bool:
        return transitions.change_subtask_state(self.orch.repo, self.orch.hub, self.sub_id,
                                                set(frm), to, at=self.orch.clock(),
                                                owner=self.owner, **kw)

    def _settle_task(self) -> None:
        """Wherever a pause or stop left the task, bring it to rest."""
        if self._step({"pausing"}, "paused"):
            return
        if self._step({"stopping"}, "stopped", reason="用户停止"):
            return

    def _end_task(self, kind, state, intent, failure) -> None:
        if kind == "done":
            if self._step({"running"}, state):
                return
            self._settle_task()
            return
        if kind == "interrupt":
            if intent == "stop":
                self._step({"running", "pausing", "paused"}, "stopping", reason="用户停止")
                self._step({"stopping"}, "stopped", reason="用户停止")
                return
            pause_reason = "system" if intent == "shutdown" else "user"
            reason = "Daemon 停机，任务被系统暂停" if pause_reason == "system" else None
            self._step({"running"}, "pausing", pause_reason=pause_reason, reason=reason)
            self._settle_task()
            return
        if self._step({"running"}, "failed", reason=failure.reason_zh[:500]):
            return
        self._settle_task()

    def _end_subtask(self, kind, state, intent, failure) -> None:
        repo = self.orch.repo
        if kind == "done":
            task = repo.get_task(self.task_id, owner=self.owner, include_deleted=True)
            if task.state != state and P.can_transition(task.state, state):
                # the parent first, without "done": the subtask's end is the one end signal
                transitions.change_task_state(repo, self.orch.hub, self.task_id, {task.state},
                                              state, at=self.orch.clock(), owner=self.owner,
                                              publish_done=False)
            if self._sub_step({"running"}, "succeeded"):
                return
            self._settle_subtask()
            return
        if kind == "interrupt":
            if intent == "stop":
                self._sub_step({"running", "pausing", "paused"}, "stopping", reason="用户停止")
                self._sub_step({"stopping"}, "stopped", reason="用户停止")
                return
            pause_reason = "system" if intent == "shutdown" else "user"
            reason = "Daemon 停机，子任务被系统暂停" if pause_reason == "system" else None
            self._sub_step({"running"}, "pausing", pause_reason=pause_reason, reason=reason)
            self._settle_subtask()
            return
        if self._sub_step({"running"}, "failed", reason=failure.reason_zh[:500]):
            return
        self._settle_subtask()

    def _settle_subtask(self) -> None:
        if self._sub_step({"pausing"}, "paused"):
            return
        self._sub_step({"stopping"}, "stopped", reason="用户停止")


class Scheduler:
    def __init__(self, orch):
        self.orch = orch
        self._queue: collections.OrderedDict[_Key, str] = collections.OrderedDict()
        self._running: dict[_Key, Job] = {}
        self._cond = threading.Condition()
        self._accepting = False
        self._stopped = False
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        """Rebuild the queue from the database and start dispatching."""
        repo = self.orch.repo
        entries: list[tuple[int, _Key, str]] = []
        for t in repo.tasks_in_states(["queued"]):
            if t.deleted_at is None:
                entries.append((t.updated_at, (t.id, None), t.owner_id))
        owners: dict[str, str] = {}
        for s in repo.subtasks_in_states(["queued"]):
            owner = owners.get(s.task_id)
            if owner is None:
                try:
                    owner = repo.get_task(s.task_id, include_deleted=True).owner_id
                except P.NotFound:
                    continue
                owners[s.task_id] = owner
            entries.append((s.created_at, (s.task_id, s.id), owner))
        with self._cond:
            for _, key, owner in sorted(entries, key=lambda e: e[0]):
                self._queue.setdefault(key, owner)
            self._accepting = True
            self._stopped = False
            self._cond.notify_all()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="orchestr-dispatch",
                                            daemon=True)
            self._thread.start()
        log.info("worker pool started: %d slot(s), %d queued", self.orch.cfg.max_running,
                 len(entries))

    def stop_accepting(self) -> None:
        with self._cond:
            self._accepting = False
            self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._accepting = False
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- the queue ------------------------------------------------------------------------
    def enqueue(self, task_id: str, subtask_id: str | None = None, *,
                owner: str = P.DEFAULT_OWNER) -> None:
        with self._cond:
            self._queue[(task_id, subtask_id)] = owner
            self._cond.notify_all()

    def discard(self, task_id: str, subtask_id: str | None = None) -> None:
        with self._cond:
            self._queue.pop((task_id, subtask_id), None)

    def queued(self) -> list[_Key]:
        with self._cond:
            return list(self._queue)

    def job_for(self, task_id: str, subtask_id: str | None = None) -> Job | None:
        with self._cond:
            return self._running.get((task_id, subtask_id))

    def jobs(self) -> list[Job]:
        with self._cond:
            return list(self._running.values())

    def running_count(self) -> int:
        with self._cond:
            return len(self._running)

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(timeout=min(left, 0.5))
        return True

    # -- dispatching ----------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._stopped and (not self._accepting or not self._queue
                                             or len(self._running) >= self.orch.cfg.max_running):
                    self._cond.wait(timeout=1.0)
                if self._stopped:
                    return
                key, owner = self._queue.popitem(last=False)
                job = Job(self.orch, key, owner)
                self._running[key] = job
            try:
                claimed = job.claim()
            except Exception:  # noqa: BLE001
                log.exception("could not claim %s", key)
                claimed = False
            if not claimed:
                with self._cond:
                    self._running.pop(key, None)
                    self._cond.notify_all()
                continue
            th = threading.Thread(target=self._work, args=(job,), daemon=True,
                                  name=f"orchestr-{key[1] or key[0]}")
            job.thread = th
            th.start()

    def _work(self, job: Job) -> None:
        try:
            job.execute()
        except Exception:  # noqa: BLE001
            log.exception("job %s ended with an error", job.key)
        finally:
            with self._cond:
                self._running.pop(job.key, None)
                self._cond.notify_all()


__all__ = ["ALL_MODULE_STATES", "Job", "Scheduler"]
