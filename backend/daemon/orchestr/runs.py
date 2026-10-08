"""The main run and the four subtasks (design doc 00 §4 and §4.1, 17 §3).

Main run (``backend/curation/cli/README.md``, "Daemon 的调用顺序"): the plan's two blocks side by side
(:mod:`.blocks`) - the CPU block's integrity -> numeric -> frame -> dedup and the VLM block's vlm, every
stage on the episode pipeline (an episode without a task text is not judged by task_success, D72; plans
from before carry an autolabel stage, now skipped) - no stage filters another (D57). Then ``final``
(aggregate: the policy verdicts into
revision N), ``report`` (commit.json last) and ``verify``: the run
directory is synced to ``<delivery>/<run_id>/``, read back, ``_COMPLETE`` written; only then does
``result_rev`` switch (D25) and ``latest`` move for a complete batch (D29, P13). Every per-episode stage
runs with ``--resume``: after a pause or a crash nothing finished is done twice.

* ``resume`` continues the main run from its journal (stopped / failed tasks, D20): each block goes on
  from where its episodes are.
* ``retry`` re-runs the (module, episode) pairs that erred or have no record - a module that failed as a
  whole on every episode - then dedup / the skill profile when they erred or were asked for, into a new
  revision (design doc 17 §3.4; D35).
* ``apply_adjudication``: adjudicate-apply (``relabel_rerun`` v1 or full, D39) -> task_success on the
  relabelled episodes without a human verdict
  -> aggregate final -> report -> verify. dedup is not run again (its groups stand; aggregate picks
  each group's keeper after the decisions).
"""
from __future__ import annotations

import contextlib
import json
import logging

from curation.contracts import modules as registry

from . import planning, resources
from .runbase import Run, TaskFailure, input_digest
from .workdir import read_json, read_lines, write_json_atomic, write_lines

log = logging.getLogger("daemon.orchestr")

#: the stages the episode pipeline runs, dedup included since D70 (design doc 17 §3.2)
EPISODE_STAGES = ("integrity", "numeric", "frame", "vlm", "dedup")
#: the module names people read in the logs (the registry's Chinese names)
_NAME = {spec.id: spec.name_zh for spec in registry.MODULES}
_NAME["autolabel"] = "无标注补描述"


def names(modules) -> str:
    return "、".join(f"「{_NAME.get(m, m)}」" for m in modules)


class StageRun(Run):
    """The stage runners the main run and the retry share."""

    # -- a check stage --------------------------------------------------------------
    def check_stage(self, st: dict, episodes: list[int], *, fresh: bool) -> list[int]:
        """Run one plan stage of ``check`` over ``episodes``; returns the survivors.

        ``fresh``: the main run (modules without episodes become succeeded with 0);
        a retry leaves modules it has nothing for untouched.
        """
        sid, mods = st["id"], list(st["modules"])
        out_file = self.wd.episodes_file(self.run_key, f"{sid}.out")
        if self.journal.done(sid):
            return read_lines(out_file) or []
        self.progress(sid, state="running", done=0, total=len(episodes))
        if not episodes:
            if fresh:
                for m in mods:
                    self.module_result(m, "succeeded", total=0, errors=0,
                                       digest=input_digest([]))
            write_lines(out_file, [])
            self.stage_done(sid, "skipped")
            self.progress(sid, note="没有 episode 进入这一档", force=True)
            return []
        self.modules_running(mods)
        vlm = st.get("kind") == "vlm"
        argv = ["check", "--modules", ",".join(mods), *self.source_args(),
                "--run-dir", str(self.wd.root),
                "--episodes", self.episodes_arg(f"{sid}.in", episodes),
                "--plan-stage", str(self.wd.plan), "--survivors-out", str(out_file)]
        argv.append("--resume")
        if vlm:
            argv += self.vlm_args()
        argv += self.module_param_args(mods)
        if sid == "frame":
            resources.admit_memory(self, sid)
        with self.cpu_slots(st, len(episodes)) as workers:
            if workers is not None and sid in EPISODE_STAGES:
                argv += ["--concurrency", str(workers)]
            outcome = self.cli(sid, argv, need_input=True, need_vlm=vlm, crash_retry=True,
                               inflight_modules=mods, episodes=len(episodes))
        if outcome.ok:
            doc = outcome.doc.get("modules") or {}
            any_error = False
            for m in mods:
                entry = doc.get(m) or {}
                total, errors = self.counts_from_records(m)
                any_error |= errors > 0
                self.module_result(m, "completed_with_errors" if errors else "succeeded",
                                   total=total, errors=errors, digest=entry.get("input_digest"))
                if errors:
                    shown = entry.get("error_episodes") or []
                    self.log(sid, "warn", f"{names([m])}有 {errors} 条执行出错，待补跑"
                                          + (f"：{', '.join(map(str, shown[:20]))}" if shown else ""))
            survivors = read_lines(out_file) or []
            self.stage_done(sid, "completed_with_errors" if any_error else "succeeded")
            return survivors
        if outcome.status == "module_failed":
            msg = outcome.message or outcome.reason()
            for m in mods:
                total, errors = self.counts_from_records(m)
                self.module_result(m, "failed", total=total, errors=errors,
                                   digest=input_digest(episodes), error=msg[:2000])
            self.log(sid, "error", f"{names(mods)}整体失败：{msg}。别的模块照常跑；这些条目都待补跑，等「重试」")
            write_lines(out_file, episodes)                # the next stage takes them all the same
            self.stage_done(sid, "failed")
            return list(episodes)
        self.fail_on(outcome, sid)
        raise AssertionError("unreachable")

    @contextlib.contextmanager
    def cpu_slots(self, st: dict, episodes: int):
        """A whole-stage CPU command holds a block of the Daemon's CPU pool while it runs
        (:meth:`CpuPool.block`, D54); yields how many workers it may start, or None for a
        stage that does not draw from the pool (VLM stages; the data integrity stage
        without its frame-by-frame decoding, design doc 14 §2.2)."""
        pool = getattr(self.orch, "cpu_pool", None)
        sid = st["id"]
        if pool is None or st.get("kind") != "cpu" or (
                sid == "integrity" and not self.module_params("data_integrity").get("decode_test")):
            yield None
            return
        want = max(1, min(int(st.get("concurrency") or 1), episodes or 1))

        def waiting():
            self.log(sid, "info", f"等其他任务让出 CPU 名额（全局 {pool.size} 个）")
            self.progress(sid, note="等 CPU 名额", force=True)

        with pool.block(self.cpu_key, want, check=self.check_intent, on_wait=waiting) as got:
            if got < want:
                self.log(sid, "info", f"拿到 {got} 个 CPU 名额（计划上限 {want}），与其他任务共用")
            yield got

    def module_params(self, module_id: str) -> dict:
        """A selected module's parameters with the registry's defaults filled in; {} if not selected."""
        from curation.cli.modparams import with_defaults

        for m in self.repo.get_task_modules(self.task_id):
            if m.module_id == module_id and m.selected:
                return with_defaults(module_id, m.params)
        return {}

    def module_param_args(self, mods: list[str]) -> list[str]:
        """``--param`` for every module of a stage: its ``modules[].params``, upload handles replaced by
        the copies made at start (``inputs/uploads.json``). The modules v2 runs itself read all of them;
        the judgement lines of every module (registry 2.1, design doc 17 §1.3) draw its findings; v1's
        own settings of v1's modules still come from the site configuration."""
        rows = {m.module_id: m for m in self.repo.get_task_modules(self.task_id)}
        table = read_json(self.wd.root / "inputs" / "uploads.json", {}) or {}
        out: list[str] = []
        for mid in mods:
            if mid not in rows:
                continue
            kinds = registry.upload_params(mid)
            for key, value in (rows[mid].params or {}).items():
                if key in kinds:
                    if not value:
                        continue
                    entry = table.get(value)
                    if entry is None:
                        raise TaskFailure("upload_missing", f"{names([mid])}的输入文件 {value} 没有随任务拷进运行目录："
                                                            "请复制为新任务")
                    value = str(self.wd.root / entry["path"])
                out += ["--param", f"{mid}.{key}={value}"]
        return out

    # -- aggregate ------------------------------------------------------------------
    def aggregate(self, sid: str, phase: str, rev: int, modules: list[str],
                  selection: list[int]) -> dict:
        modules = registry.with_riders(modules)
        if self.journal.done(sid):
            return self.journal.stage(sid).get("counts") or {}
        if not modules:
            raise TaskFailure("no_modules", "这个任务没有能在这个数据集上运行的模块")
        self.progress(sid, state="running", done=0, total=1)
        argv = ["aggregate", "--run-dir", str(self.wd.root), "--phase", phase,
                "--revision", str(rev), "--modules", ",".join(modules),
                "--episodes", self.episodes_arg("selected", selection)]
        need_input = phase == "final"
        if need_input:
            argv += self.source_args(manifest=False, semantics=True)
        outcome = self.cli(sid, argv, need_input=need_input)
        if not outcome.ok:
            self.fail_on(outcome, sid)
        counts = outcome.doc.get("counts") or {}
        if phase == "funnel":
            self.log(sid, "info", f"漏斗判决（r{rev:04d}）：保留 {counts.get('keep', 0)}、拒绝 "
                                  f"{counts.get('drop', 0)}、待补跑 {counts.get('held', 0)}")
        else:
            self.log(sid, "info", f"终判（r{rev:04d}）：通过 {counts.get('passed', 0)}、拒绝 "
                                  f"{counts.get('reject', 0)}、待补跑 {counts.get('held', 0)}，"
                                  f"待人工复核 {counts.get('review', 0)}")
        self.stage_done(sid, "succeeded", counts=counts)
        return counts

    def report(self, rev: int, modules: list[str]) -> None:
        modules = registry.with_riders(modules)
        sid = "report"
        if self.journal.done(sid):
            return
        if not self.committed(rev):
            self.progress(sid, state="running", done=0, total=1)
            argv = ["report", "--run-dir", str(self.wd.root), "--revision", str(rev),
                    "--modules", ",".join(modules)]
            if self.sub_id:
                argv += ["--subtask-id", self.sub_id]
            outcome = self.cli(sid, argv, need_input=False)
            if not outcome.ok:
                self.fail_on(outcome, sid)
        self.stage_done(sid, "succeeded")

    # -- publishing -------------------------------------------------------------------
    def publish(self, rev: int) -> None:
        """Sync, verify, then switch the revision - one delivery directory at a time."""
        with self.orch.locks.lock(self.task.delivery_key):
            with self.delivery() as d:
                if not self.journal.done("verify"):
                    vdoc = self.sync_and_verify("verify", d)
                    self.log("verify", "info", f"交付核验通过：回读 {vdoc.get('checked', 0)} 个文件，"
                                               "写了 _COMPLETE")
                    self.stage_done("verify", "succeeded")
                self.switch_revision(rev)
                self.refresh_results(rev)
                self.maybe_latest(d, "verify")


class MainRun(StageRun):
    """The main run of a task; ``resume`` subtasks continue it with the same journal."""

    kind = "main"

    def journal_key(self) -> str:
        return "main"

    def write_progress(self, doc: dict) -> None:
        self.repo.set_task_progress(self.task_id, doc)
        if self.sub_id:                                   # a resume: the subtask's too
            self.repo.set_subtask_progress(self.sub_id, doc)

    def stage_ids(self, plan: dict) -> list[str]:
        return [s["id"] for s in plan["stages"]] + ["report", "verify"]

    def execute(self) -> str:
        from .blocks import run_blocks

        self.reload()
        if self.sub_id is not None:                      # a resume, maybe days later
            self.ensure_local()
            self.forget_listing()
        self.require_current_format()
        plan = planning.ensure_plan(self)
        self.plan_progress(self.stage_ids(plan), plan)
        modules = self.plan_modules(plan)
        planning.mark_skipped_modules(self, plan)
        selection = self.selection()
        rev = self.allocate_revision()
        run_blocks(self, plan, selection)
        self.check_intent()
        self.aggregate("final", "final", rev, modules, selection)
        self.report(rev, modules)
        self.check_intent()
        self.publish(rev)
        return self.recomputed_state(rev)


class ResumeRun(MainRun):
    kind = "resume"


class RetryRun(StageRun):
    """The (module, episode) pairs that erred or have no record, again; a module that failed as a whole on
    every episode (design doc 17 §3.4). Nothing downstream depends on them: no stage is redone after them."""

    kind = "retry"

    def execute(self) -> str:
        from curation.pipeline.records import is_error, latest_results
        from curation.pipeline.skipped import all_skipped

        self.reload()
        self.ensure_local()
        self.forget_listing()
        self.require_current_format()
        plan = self.plan_doc()
        modules = self.plan_modules(plan)
        scope = set((self.subtask.scope or {}).get("modules") or [])
        rows = self.module_rows()
        failed = {m for m in modules if rows.get(m) and rows[m].state == "failed"
                  and (not scope or m in scope)}
        selection = self.selection()
        left_out = set(all_skipped(str(self.wd.root)))        # missing source files (D40)
        judged = [e for e in selection if e not in left_out]
        stream = [s for s in plan["stages"] if s.get("command") == "check"]
        todo: dict[str, tuple[list[str], list[int]]] = {}
        for st in stream:
            mods, eps = [], set()
            for m in st["modules"]:
                host = registry.get(m).rides_on or m        # a rider is answered in its host's requests
                if scope and m not in scope and host not in scope:
                    continue
                if m in failed:
                    need = set(judged)
                else:
                    recs = latest_results(str(self.wd.root), m)
                    need = {e for e in judged if e not in recs or is_error(recs[e])}
                if need:
                    eps |= need
                    mods += [x for x in (host, m) if x not in mods]
            todo[st["id"]] = ([m for m in st["modules"] if m in mods], sorted(eps))
        ids = [s["id"] for s in stream] + ["final", "report", "verify"]
        self.plan_progress(ids, plan)
        pairs = sum(len(m) * len(e) for m, e in todo.values())
        self.journal.set(retry={sid: {"modules": m, "episodes": e} for sid, (m, e) in todo.items()},
                         failed_modules=sorted(failed))
        self.log("system", "info", f"重试：补跑 {len(set().union(*[set(e) for _, e in todo.values()]))} 条 episode 上"
                                   f"出错或没有结果的模块（共 {pairs} 对）"
                 + (f"，整体失败的模块全量重跑：{names(sorted(failed))}" if failed else ""))
        rev = self.allocate_revision()
        for st in stream:
            self.check_intent()
            mods, eps = todo[st["id"]]
            if mods and eps:
                self.check_stage({**st, "modules": mods}, eps, fresh=False)
            elif not self.journal.done(st["id"]):
                self.stage_done(st["id"], "skipped")
                self.progress(st["id"], note="没有需要补跑的条目", force=True)
        self.check_intent()
        self.aggregate("final", "final", rev, modules, selection)
        self.report(rev, modules)
        self.check_intent()
        self.publish(rev)
        return self.recomputed_state(rev)


class AdjudicationRun(StageRun):
    """Human decisions onto the verdicts; model calls only for relabelled episodes (D10)."""

    kind = "apply_adjudication"

    def execute(self) -> str:
        self.reload()
        self.ensure_local()
        self.require_current_format()
        plan = self.plan_doc()
        modules = self.plan_modules(plan)
        stages = {s["id"]: s for s in plan["stages"]}
        ids = ["adjudicate"] + (["vlm"] if "vlm" in stages else []) \
            + ["final", "report", "verify"]
        self.plan_progress(ids, plan)
        rev = self.allocate_revision()
        selection = self.selection()
        applied = self.apply_decisions()
        rerun = applied.get("rerun", [])
        if "vlm" in stages and "task_success" in stages["vlm"].get("modules", []):
            self.rejudge(stages["vlm"], rerun)
        self.check_intent()
        self.aggregate("final", "final", rev, modules, selection)
        self.report(rev, modules)
        self.check_intent()
        self.publish(rev)
        ids_applied = applied.get("ids") or []
        if ids_applied:
            self.repo.mark_adjudications_applied(ids_applied, self.sub_id)
            self.refresh_results(rev)
        return self.recomputed_state(rev)

    def apply_decisions(self) -> dict:
        sid = "adjudicate"
        entry = self.journal.stage(sid)
        if entry.get("done"):
            return entry.get("applied") or {}
        self.progress(sid, state="running", done=0, total=1)
        rows = self.orch.decisions_to_apply(self.reload())
        rerun_how = (self.subtask.scope or {}).get("relabel_rerun") or "v1"
        doc = {"schema_version": "1.0", "relabel_rerun": rerun_how, "decisions": [
            {"id": a.id, "episode_index": a.episode_index, "line": a.line, "decision": a.decision,
             "new_label": a.new_label, "note": a.note, "decided_by": a.decided_by,
             "decided_at": a.decided_at} for a in sorted(rows, key=lambda r: r.id)]}
        path = self.wd.run_dir(self.run_key) / "decisions.json"
        write_json_atomic(path, doc)
        outcome = self.cli(sid, ["adjudicate-apply", "--run-dir", str(self.wd.root),
                                 "--decisions", str(path)], need_input=False)
        if not outcome.ok:
            self.fail_on(outcome, sid)
        self.copy_decisions()
        res = outcome.doc
        applied = {"ids": [a.id for a in rows], "rerun": list(res.get("rerun_task_success") or [])}
        self.log(sid, "info", f"执行裁决：应用 {res.get('applied', 0)} 条（{res.get('skipped_already_applied', 0)} "
                              f"条之前已应用）；按新标注重跑任务成败判定 {len(applied['rerun'])} 条")
        self.stage_done(sid, "succeeded", applied=applied)
        return applied

    def copy_decisions(self) -> None:
        """``adjudicate-apply`` rewrote ``human-decisions/*.csv`` with the applied decisions
        only; every recorded one goes back (W5b's writer, the database is the authority).
        The publish that follows delivers them."""
        from ..results import store_of, write_copies

        try:
            write_copies(store_of(self.orch.rt), self.repo, self.reload())
        except Exception:  # noqa: BLE001 - a copy; the database has the decisions
            log.warning("task %s: human-decisions/ not rewritten after adjudicate-apply",
                        self.task_id, exc_info=True)

    def rejudge(self, st: dict, episodes: list[int]) -> None:
        """task_success again for relabelled episodes (not ``--resume``: they have results)."""
        from curation.pipeline.records import load_parts, next_part

        sid = "vlm"
        if self.journal.done(sid):
            return
        if not episodes:
            self.stage_done(sid, "skipped")
            self.progress(sid, note="没有需要按新标注重跑的条目", force=True)
            return
        first = self.journal.stage(sid).get("first_part")
        if not first:
            first = next_part(str(self.wd.root), ["task_success"])
            self.journal.mark(sid, first_part=first)
        self.modules_running(["task_success"])
        self.progress(sid, state="running", done=0, total=len(episodes))
        for attempt in range(3):
            done = {e for e, (part, _) in load_parts(str(self.wd.root), "task_success").items()
                    if part >= first}
            todo = [e for e in episodes if e not in done]
            if not todo:
                break
            argv = ["check", "--modules", "task_success", *self.source_args(),
                    "--run-dir", str(self.wd.root),
                    "--episodes", self.episodes_arg("rejudge.in", todo),
                    "--plan-stage", str(self.wd.plan), *self.vlm_args()]
            try:
                outcome = self.cli(sid, argv, need_input=True, need_vlm=True, crash_retry=False)
            except TaskFailure as err:
                if err.code == "crashed" and attempt < 2:
                    self.log(sid, "error", f"{err.reason_zh}；重新拉起没做完的 {len(todo)} 条")
                    continue
                raise
            if outcome.ok:
                break
            if outcome.status == "module_failed":
                total, errors = self.counts_from_records("task_success")
                self.module_result("task_success", "failed", total=total, errors=errors,
                                   error=(outcome.message or outcome.reason())[:2000])
                self.stage_done(sid, "failed")
                return
            self.fail_on(outcome, sid)
        total, errors = self.counts_from_records("task_success")
        self.module_result("task_success", "completed_with_errors" if errors else "succeeded",
                           total=total, errors=errors)
        self.stage_done(sid, "completed_with_errors" if errors else "succeeded")


RUNS = {"main": MainRun, "resume": ResumeRun, "retry": RetryRun,
        "apply_adjudication": AdjudicationRun}


def run_for(orch, task, subtask=None) -> Run:
    kind = subtask.kind if subtask is not None else "main"
    return RUNS[kind](orch, task, subtask)


def describe(doc) -> str:
    return json.dumps(doc, ensure_ascii=False)[:200]
