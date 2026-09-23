"""The main run and the four subtasks (design doc 00 §4 and §4.1).

Main run (``backend/curation/cli/README.md``, "Daemon 的调用顺序"): the plan's
stages in order - autolabel, then the funnel stages ``numeric`` -> ``frame`` ->
``vlm`` on concurrent bounded batch workers (each reads the survivors of the
one before; a module that fails as a whole leaves its gate open), ``verdict``
(aggregate funnel into revision N), ``dedup``
on ``keep.txt``, ``profile`` on the kept set minus duplicates, ``final``
(aggregate final) - then ``report`` (commit.json last), ``export`` when the task
exports, and ``verify``: the run directory is synced to ``<delivery>/<run_id>/``,
read back, ``_COMPLETE`` written; only then does ``result_rev`` switch (D25) and
``latest`` move for a complete batch (D29, P13). Every check stage runs with
``--resume``: after a pause or a crash nothing finished is done twice.

* ``resume`` continues the main run from its journal (stopped / failed tasks, D20).
* ``retry`` re-runs the held episodes from the stage they erred in (whole failed
  modules in full), then syncs dedup / profile when their input changed, into a
  new revision (D25, D35).
* ``apply_adjudication``: adjudicate-apply (``relabel_rerun`` v1 or full, D39) ->
  task_success on the relabelled episodes without a human verdict -> aggregate
  funnel (new revision; its ``keep.txt`` follows the decisions) -> skill_profile
  ``--incremental`` on that ``keep.txt`` -> aggregate final -> report -> verify.
  dedup is not run again (its first result stands, as in v1's rejudge). It never
  exports (D9); the delivery becomes stale.
* ``reexport``: ``export --incremental`` of the current revision, then verify.
"""
from __future__ import annotations

import json
import logging

from curation.contracts import modules as registry

from . import planning, resources
from .runbase import Run, TaskFailure, input_digest
from .workdir import read_json, read_lines, write_json_atomic, write_lines

log = logging.getLogger("daemon.orchestr")

FUNNEL = ("numeric", "frame", "vlm")
#: plan stages of the advisory modules (``advisory_<stage>``, registry 1.4): all selected episodes
ADVISORY = "advisory_"
#: the module names people read in the logs (the registry's Chinese names)
_NAME = {spec.id: spec.name_zh for spec in registry.MODULES}
_NAME["autolabel"] = "无标注补描述"


def names(modules) -> str:
    return "、".join(f"「{_NAME.get(m, m)}」" for m in modules)


class StageRun(Run):
    """The stage runners the main run and the retry share."""

    # -- a check stage --------------------------------------------------------------
    def check_stage(self, st: dict, episodes: list[int], *, fresh: bool,
                    incremental: bool = False) -> list[int]:
        """Run one plan stage of ``check`` over ``episodes``; returns the survivors.

        ``fresh``: the main run (modules without episodes become succeeded with 0);
        a retry leaves modules it has nothing for untouched.
        """
        sid, mods = st["id"], list(st["modules"])
        out_file = self.wd.episodes_file(self.run_key, f"{sid}.out")
        if self.journal.done(sid):
            return read_lines(out_file) or []
        post = sid in ("dedup", "profile")
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
        if not post:
            argv.append("--resume")
        if incremental:
            argv.append("--incremental")
        if vlm:
            argv += self.vlm_args()
        argv += self.module_param_args(mods)
        if sid == "frame":
            resources.admit_memory(self, sid)
        outcome = self.cli(sid, argv, need_input=True, need_vlm=vlm, crash_retry=not post,
                           inflight_modules=mods, episodes=len(episodes))
        if outcome.ok:
            doc = outcome.doc.get("modules") or {}
            any_error = False
            for m in mods:
                entry = doc.get(m) or {}
                if post:
                    counts = entry.get("episodes") or {}
                    total, errors = int(counts.get("total") or 0), int(counts.get("error") or 0)
                else:
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
                total, errors = (0, 0) if post else self.counts_from_records(m)
                self.module_result(m, "failed", total=total, errors=errors,
                                   digest=input_digest(episodes), error=msg[:2000])
            self.log(sid, "error", f"{names(mods)}整体失败：{msg}。它的门视为尚未生效，后面的模块照常跑；"
                                   "这些条目都待补跑，等「重试」")
            write_lines(out_file, episodes)                # the gate stays open (04 §7)
            self.stage_done(sid, "failed")
            return list(episodes)
        self.fail_on(outcome, sid)
        raise AssertionError("unreachable")

    def module_param_args(self, mods: list[str]) -> list[str]:
        """``--param`` for the advisory modules of a stage (registry 1.4) and the modules they
        re-examine: their ``modules[].params``, upload handles replaced by the copies made at start
        (``inputs/uploads.json``). v1's modules take their settings from the site configuration as before."""
        rows = {m.module_id: m for m in self.repo.get_task_modules(self.task_id)}
        table = read_json(self.wd.root / "inputs" / "uploads.json", {}) or {}
        out: list[str] = []
        # a module that re-examines another one (depends_on a module id: the EEF review) reads its file
        wanted = list(dict.fromkeys([*mods, *(d for m in mods for d in registry.get(m).depends_on
                                              if d in registry.ids())]))
        for mid in wanted:
            if registry.get(mid).affects_dataset_verdict or mid not in rows:
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

    # -- autolabel ------------------------------------------------------------------
    def autolabel(self, episodes: list[int]) -> None:
        sid = "autolabel"
        if self.journal.done(sid) or not episodes:
            if not self.journal.done(sid):
                self.stage_done(sid, "skipped")
            return
        self.progress(sid, state="running", done=0, total=0)
        argv = ["autolabel", *self.source_args(), "--run-dir", str(self.wd.root),
                "--episodes", self.episodes_arg("autolabel.in", episodes), "--resume",
                "--plan-stage", str(self.wd.plan), *self.vlm_args()]
        outcome = self.cli(sid, argv, need_input=True, need_vlm=True, crash_retry=True,
                           episodes=len(episodes))
        if outcome.ok:
            c = outcome.doc.get("counts") or {}
            self.log(sid, "info", f"给 {c.get('total', 0)} 条没有任务标注的 episode 补描述："
                                  f"{c.get('ok', 0)} 条补上、{c.get('unclear', 0)} 条看不清、"
                                  f"{c.get('error', 0)} 条出错")
            self.stage_done(sid, "completed_with_errors" if c.get("error") else "succeeded")
            return
        if outcome.status == "module_failed":
            self.log(sid, "error", f"补描述整体失败：{outcome.message or outcome.reason()}。"
                                   "没有标注的条目，任务成败判定会待补跑")
            self.stage_done(sid, "failed")
            return
        self.fail_on(outcome, sid)

    # -- aggregate ------------------------------------------------------------------
    def aggregate(self, sid: str, phase: str, rev: int, modules: list[str],
                  selection: list[int]) -> dict:
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

    def keep_of(self, rev: int) -> list[int]:
        return read_lines(self.wd.revision_dir(rev) / "keep.txt") or []

    def minus_duplicates(self, episodes: list[int]) -> list[int]:
        from curation.pipeline.records import latest_results

        dup = {e for e, r in latest_results(str(self.wd.root), "dedup").items()
               if r.get("verdict") in ("fail", "error")}
        return [e for e in episodes if e not in dup]

    # -- publishing -------------------------------------------------------------------
    def publish(self, rev: int, *, export: bool) -> None:
        """Export (optional), sync, verify, then switch the revision - one delivery at a time."""
        with self.orch.locks.lock(self.task.delivery_key):
            with self.delivery() as d:
                if export and not self.journal.done("export"):
                    incremental = (self.wd.root / "export" / "manifest.json").is_file()
                    doc = self.export("export", d, rev, incremental=incremental)
                    self.stage_done("export", "succeeded", fingerprint=doc.get("fingerprint"))
                if not self.journal.done("verify"):
                    vdoc = self.sync_and_verify("verify", d)
                    self.log("verify", "info", f"交付核验通过：回读 {vdoc.get('checked', 0)} 个文件，"
                                               "写了 _COMPLETE")
                    self.stage_done("verify", "succeeded")
                self.switch_revision(rev)
                fingerprint = self.journal.stage("export").get("fingerprint")
                if export and fingerprint:
                    self.repo.set_export_fingerprint(self.task_id, fingerprint, False)
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
        ids = [s["id"] for s in plan["stages"]] + ["report"]
        if (self.task.params or {}).get("export", True):
            ids.append("export")
        return ids + ["verify"]

    def execute(self) -> str:
        self.reload()
        if self.sub_id is not None:                      # a resume, maybe days later
            self.ensure_local()
        plan = planning.ensure_plan(self)
        self.plan_progress(self.stage_ids(plan))
        modules = self.plan_modules(plan)
        planning.mark_skipped_modules(self, plan)
        selection = self.selection()
        rev = self.allocate_revision()
        stages = {s["id"]: s for s in plan["stages"]}
        survivors: dict[str, list[int]] = {}
        funnel_stages = [s for s in plan["stages"] if s["id"] in FUNNEL]
        from curation.pipeline.episode_state import state_path

        # A pre-pipeline task may already have journaled whole stages. Finish it
        # with its original stage boundaries; new tasks and pipeline resumes use
        # the per-episode SQLite handoff instead.
        pipeline = bool(funnel_stages) and (state_path(self.wd.root).is_file() or not any(
            self.journal.done(s["id"]) for s in funnel_stages))
        funnel_started = False
        for st in plan["stages"]:
            self.check_intent()
            sid = st["id"]
            if st.get("command") == "autolabel":
                self.autolabel(selection)
            elif sid in FUNNEL:
                if pipeline:
                    if not funnel_started:
                        if not all(self.journal.done(s["id"]) for s in funnel_stages):
                            from .pipeline import run_funnel

                            run_funnel(self, funnel_stages, selection)
                        funnel_started = True
                else:
                    ref = st.get("episodes", "selected")
                    source = selection if ref == "selected" else survivors.get(
                        ref.split(":", 1)[1], [])
                    survivors[sid] = self.check_stage(st, source, fresh=True)
            elif sid.startswith(ADVISORY):
                # every selected episode, never a gate (registry 1.4): nothing reads its survivors
                self.check_stage(st, selection, fresh=True)
            elif st.get("phase") == "funnel":
                self.aggregate(sid, "funnel", rev, modules, selection)
            elif sid == "dedup":
                survivors["dedup"] = self.check_stage(st, self.keep_of(rev), fresh=True)
            elif sid == "profile":
                source = survivors["dedup"] if "dedup" in stages else self.keep_of(rev)
                survivors["profile"] = self.check_stage(st, source, fresh=True)
            elif st.get("phase") == "final":
                self.aggregate(sid, "final", rev, modules, selection)
            if (st.get("command") == "check" and not (pipeline and sid in FUNNEL)) \
                    or st.get("command") == "autolabel":
                self.sync_quietly(sid)                 # checks/<module>/ goes up as it is done
        self.check_intent()
        self.report(rev, modules)
        self.check_intent()
        self.publish(rev, export=bool((self.task.params or {}).get("export", True)))
        return self.recomputed_state(rev)


class ResumeRun(MainRun):
    kind = "resume"


class RetryRun(StageRun):
    """Held episodes again, from the stage they erred in; whole failed modules in full (D25)."""

    kind = "retry"

    def execute(self) -> str:
        self.reload()
        self.ensure_local()
        plan = self.plan_doc()
        modules = self.plan_modules(plan)
        cur = int(self.task.result_rev or 0)
        scope = set((self.subtask.scope or {}).get("modules") or [])
        held_doc = read_json(self.wd.revision_dir(cur) / "held.json", {}) or {}
        widened = scope | ({"autolabel"} if "task_success" in scope else set())
        held = sorted({int(e["episode_index"]) for e in held_doc.get("episodes") or []
                       if not scope or any((r.get("module") in widened)
                                           for r in e.get("reasons") or [])})
        rows = self.module_rows()
        failed = {m for m in modules if rows.get(m) and rows[m].state == "failed"
                  and (not scope or m in scope)}
        funnel = [s for s in plan["stages"] if s["id"] in FUNNEL]
        advisory = [s for s in plan["stages"] if s["id"].startswith(ADVISORY)
                    and any(rows.get(m) and (rows[m].state in ("failed", "stale") or rows[m].episodes_error > 0
                                             or m in scope) for m in s["modules"])]
        ids = (["autolabel"] if any(s.get("command") == "autolabel" for s in plan["stages"])
               else []) + [s["id"] for s in funnel] + [s["id"] for s in advisory] + ["verdict"]
        post = {s["id"]: s for s in plan["stages"] if s["id"] in ("dedup", "profile")}
        ids += list(post) + ["final", "report", "verify"]
        self.plan_progress(ids)
        self.journal.set(episodes=held, failed_modules=sorted(failed))
        self.log("verdict", "info", f"重试：补跑 {len(held)} 条待补跑的 episode"
                 + (f"，整体失败的模块全量重跑：{names(sorted(failed))}" if failed else ""))
        rev = self.allocate_revision()
        selection = self.selection()
        todo = list(held)
        if "autolabel" in ids:
            self.autolabel(todo)
        for st in funnel:
            self.check_intent()
            todo = self.check_stage(st, todo, fresh=False)
        for st in advisory:                 # --resume redoes only their error lines
            self.check_intent()
            self.check_stage(st, selection, fresh=False)
        self.check_intent()
        self.aggregate("verdict", "funnel", rev, modules, selection)
        keep = self.keep_of(rev)
        if "dedup" in post:
            self.sync_post("dedup", post["dedup"], keep, scope, rows)
        if "profile" in post:
            base = self.minus_duplicates(keep) if "dedup" in post else keep
            self.sync_post("profile", post["profile"], base, scope, rows, incremental=True)
        self.check_intent()
        self.aggregate("final", "final", rev, modules, selection)
        self.report(rev, modules)
        self.check_intent()
        self.publish(rev, export=False)
        return self.recomputed_state(rev)

    def sync_post(self, sid: str, st: dict, episodes: list[int], scope: set, rows: dict, *,
                  incremental: bool = False) -> list[int]:
        """dedup / profile follow the kept set: re-run when it changed, or when they erred."""
        module = st["modules"][0]
        row = rows.get(module)
        changed = row is None or row.input_digest != input_digest(episodes)
        erred = row is not None and (row.state in ("failed", "stale") or row.episodes_error > 0)
        if self.journal.done(sid):
            return read_lines(self.wd.episodes_file(self.run_key, f"{sid}.out")) or []
        if not (changed or erred or module in scope):
            self.stage_done(sid, "skipped")
            self.progress(sid, note="输入没有变化，沿用上一版结果", force=True)
            return episodes
        if changed and row is not None and row.state in ("succeeded", "completed_with_errors"):
            self.repo.mark_modules_stale(self.task_id, [module])
        # --incremental builds on an existing profile; without one (never ran, failed, empty)
        # the module runs in full
        full = row is None or row.state == "failed" or row.episodes_total == 0 or not incremental
        return self.check_stage(st, episodes, fresh=True, incremental=incremental and not full)


class AdjudicationRun(StageRun):
    """Human decisions onto the verdicts; model calls only for relabelled episodes (D10)."""

    kind = "apply_adjudication"

    def execute(self) -> str:
        self.reload()
        self.ensure_local()
        plan = self.plan_doc()
        modules = self.plan_modules(plan)
        stages = {s["id"]: s for s in plan["stages"]}
        ids = ["adjudicate"] + (["vlm"] if "vlm" in stages else []) + ["verdict"]
        ids += (["profile"] if "profile" in stages else []) + ["final", "report", "verify"]
        self.plan_progress(ids)
        rev = self.allocate_revision()
        selection = self.selection()
        applied = self.apply_decisions()
        rerun = applied.get("rerun", [])
        if "vlm" in stages and "task_success" in stages["vlm"].get("modules", []):
            self.rejudge(stages["vlm"], rerun)
        self.check_intent()
        self.aggregate("verdict", "funnel", rev, modules, selection)
        keep = self.keep_of(rev)                  # already follows the applied decisions
        rows = self.module_rows()
        if "profile" in stages and not self.journal.done("profile"):
            row = rows.get("skill_profile")
            if applied.get("resync") or row is None or row.input_digest != input_digest(keep) \
                    or row.state in ("failed", "stale"):
                self.repo.mark_modules_stale(self.task_id, ["skill_profile"])
                full = row is None or row.state == "failed"
                self.check_stage(stages["profile"], keep, fresh=True, incremental=not full)
            else:
                self.stage_done("profile", "skipped")
                self.progress("profile", note="画像的输入没有变化", force=True)
        self.check_intent()
        self.aggregate("final", "final", rev, modules, selection)
        self.report(rev, modules)
        self.check_intent()
        self.publish(rev, export=False)
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
        rows, lapsed = self.orch.decisions_to_apply(self.reload())
        if lapsed:
            self.log(sid, "info", f"{len(lapsed)} 条追问的回答已作废（打开它的标注判断后来改了），不执行："
                     + "、".join(f"ep{a.episode_index:06d}" for a in lapsed[:20]))
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
        applied = {"ids": [a.id for a in rows], "rerun": list(res.get("rerun_task_success") or []),
                   "resync": list(res.get("profile_resync") or [])}
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


class ReexportRun(StageRun):
    """``export --incremental`` of the current revision, then verify (06 §4; D9)."""

    kind = "reexport"

    def execute(self) -> str:
        self.reload()
        rev = int(self.task.result_rev or 0)
        if rev < 1:
            raise TaskFailure("no_result", "这个任务还没有结果，没什么可导出的")
        self.ensure_local()
        self.plan_progress(["export", "verify"])
        with self.orch.locks.lock(self.task.delivery_key):
            with self.delivery() as d:
                if not self.journal.done("export"):
                    doc = self.export("export", d, rev, incremental=True)
                    self.stage_done("export", "succeeded", fingerprint=doc.get("fingerprint"))
                if not self.journal.done("verify"):
                    self.sync_and_verify("verify", d)
                    self.stage_done("verify", "succeeded")
                fingerprint = self.journal.stage("export").get("fingerprint")
                current = self.current_fingerprint(rev)
                self.repo.set_export_fingerprint(self.task_id, fingerprint,
                                                 current is None or current != fingerprint)
                self.maybe_latest(d, "verify")
        self.reload()
        return self.recomputed_state(rev)


RUNS = {"main": MainRun, "resume": ResumeRun, "retry": RetryRun,
        "apply_adjudication": AdjudicationRun, "reexport": ReexportRun}


def run_for(orch, task, subtask=None) -> Run:
    kind = subtask.kind if subtask is not None else "main"
    return RUNS[kind](orch, task, subtask)


def describe(doc) -> str:
    return json.dumps(doc, ensure_ascii=False)[:200]
