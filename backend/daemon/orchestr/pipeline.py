"""Funnel entry point and batch compatibility for site-supplied CLI wrappers."""
from __future__ import annotations

import concurrent.futures as futures
from contextlib import nullcontext
import os
import queue
import threading

from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.records import compact
from daemon.exec import CliCommand

from . import resources
from .runbase import Interrupt, TaskFailure, input_digest
from .stage_worker import StageWorker
from .workdir import read_lines, write_lines


def _batches(episodes: list[int], size: int):
    for start in range(0, len(episodes), size):
        yield episodes[start:start + size]


def batch_size_for(stages: list[dict]) -> int:
    parallel = max(int(s.get("concurrency") or (s.get("gates") or {}).get("episode") or 1)
                   for s in stages)
    return max(8, min(64, parallel * 2))


def effective_batch_size(stages: list[dict], episodes: int,
                         configured: int | None = None) -> int:
    """The API override is exact; the default also fills small pipelines."""
    if configured is not None:
        return configured
    return min(batch_size_for(stages), max(1, (episodes + len(stages) - 1) // len(stages)))


def cpu_shares_for(stages: list[dict]) -> dict[str, int]:
    """Divide the plan's CPU worker limit between overlapping numeric/frame calls."""
    cpu = [s for s in stages if s["id"] in ("numeric", "frame")]
    if not cpu:
        return {}
    budget = min(max(1, int(s.get("concurrency") or 1)) for s in cpu)
    if len(cpu) == 1:
        return {cpu[0]["id"]: budget}
    numeric = max(1, budget // 4)
    return {"numeric": numeric, "frame": max(1, budget - numeric)}


def run_funnel(run, stages: list[dict], selection: list[int]) -> None:
    if not os.environ.get("CURATOR_CLI"):
        from .episode_pipeline import run_episodes

        return run_episodes(run, stages, selection)
    return run_batches(run, stages, selection)


def run_batches(run, stages: list[dict], selection: list[int]) -> None:
    """Compatibility path for external CLI wrappers and batch comparison tests.

    Every worker commits each episode into SQLite before publishing its batch's
    survivor list. The queues hold at most two batches, so a fast upstream layer
    cannot fill memory while a slow VLM layer is working.
    """
    if not stages:
        return
    cpu_shares = cpu_shares_for(stages)
    stages = [{**s, "_pipeline_concurrency": cpu_shares.get(s["id"])} for s in stages]
    ids = [s["id"] for s in stages]
    modules = [m for stage in stages for m in stage["modules"]]
    path = state_path(run.wd.root)
    store = EpisodeState(path)
    try:
        store.bootstrap(str(run.wd.root), modules)
        store.seed_progress(selection, [(s["id"], s["modules"]) for s in stages])
        saved = store.progress_for(selection)
    finally:
        store.close()

    routes: dict[str, list[int]] = {sid: [] for sid in ids}
    for ep in selection:
        dest = saved.get(ep, ids[0])
        if dest in routes:
            routes[dest].append(ep)
    configured = (run.task.params or {}).get("batch_size")
    batch_size = effective_batch_size(stages, len(selection), configured)
    run.log("system", "info", f"流水线：每批最多 {batch_size} 条，{len(stages)} 层同时运行；"
                              f"已有 {len(selection) - sum(map(len, routes.values()))} 条完成")
    for st in stages:
        if not run.journal.done(st["id"]):
            run.progress(st["id"], state="pending", done=0, total=len(selection),
                         note=f"按 episode 流水执行，每批最多 {batch_size} 条")

    queues = [queue.Queue(maxsize=2) for _ in stages]
    abort = threading.Event()
    run._pipeline_abort = abort
    first_error: list[BaseException] = []
    error_lock = threading.Lock()
    sync_lock = threading.Lock()
    cpu_budget = min((int(s.get("concurrency") or 1) for s in stages
                      if s["id"] in cpu_shares), default=1)
    cpu_lock = threading.Lock() if len(cpu_shares) > 1 and cpu_budget == 1 else None
    sentinel = object()
    seen: dict[str, set[int]] = {sid: set() for sid in ids}
    failed: set[str] = set()
    failure_messages: dict[str, str] = {}

    def finish_stage(st: dict) -> None:
        sid = st["id"]
        if run.journal.done(sid):
            return
        mods = st["modules"]
        output = run.wd.episodes_file(run.run_key, f"{sid}.out")
        index = EpisodeState(path)
        try:
            stage_inputs = sorted(set(index.stage_inputs(mods)) | seen[sid])
            if sid in failed:
                write_lines(output, stage_inputs)
                for module in mods:
                    total, errors = index.counts(module)
                    run.module_result(module, "failed", total=total, errors=errors,
                                      digest=input_digest(stage_inputs),
                                      error=failure_messages.get(sid, "模块整体失败；本档的门已放行"))
                state = "failed"
            else:
                write_lines(output, index.survivors(mods))
                errors_found = False
                for module in mods:
                    total, errors = index.counts(module)
                    errors_found |= errors > 0
                    run.module_result(module, "completed_with_errors" if errors else "succeeded",
                                      total=total, errors=errors,
                                      digest=input_digest(stage_inputs))
                state = ("skipped" if not stage_inputs else
                         "completed_with_errors" if errors_found else "succeeded")
        finally:
            index.close()
        for module in mods:
            compact(str(run.wd.root), module)
        run.progress(sid, done=len(stage_inputs), total=len(stage_inputs), force=True)
        run.stage_done(sid, state)
        with sync_lock:
            run.sync_quietly(sid)

    def put(q: queue.Queue, item) -> None:
        while not abort.is_set():
            try:
                q.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def worker(index: int) -> None:
        st = stages[index]
        sid = st["id"]
        next_stage = ids[index + 1] if index + 1 < len(ids) else "done"
        downstream = queues[index + 1] if index + 1 < len(ids) else None
        seen_batches = 0
        count = 0
        dropped: list[int] = []
        stage_failed = False
        child: list[StageWorker] = []
        try:
            while not abort.is_set():
                try:
                    item = queues[index].get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is sentinel:
                    break
                batch = item
                if seen_batches == 0 and not run.journal.done(sid):
                    run.modules_running(st["modules"])
                    run.progress(sid, state="running")
                seen_batches += 1
                seen[sid].update(batch)
                run.progress(sid, note=(f"按 episode 流水执行，每批最多 {batch_size} 条；"
                                       f"当前批次 {len(batch)} 条处理中"), force=True)
                if stage_failed:
                    survivors = batch
                    correction = EpisodeState(path)
                    try:
                        correction.forward(sid, batch, next_stage)
                    finally:
                        correction.close()
                else:
                    guard = cpu_lock if cpu_lock is not None and sid in cpu_shares else nullcontext()
                    with guard:
                        survivors, module_failed, failure_message = _check_batch(
                            run, st, batch, seen_batches, count, len(selection),
                            str(path), next_stage, child)
                    if module_failed:
                        stage_failed = True
                        failed.add(sid)
                        failure_messages[sid] = (failure_message or "模块整体失败")[:2000]
                        # A failed module's gate is open for the whole input, including
                        # episodes rejected by earlier batches of that same layer.
                        if dropped:
                            correction = EpisodeState(path)
                            try:
                                correction.forward(sid, dropped, next_stage)
                            finally:
                                correction.close()
                            if downstream is not None:
                                for chunk in _batches(dropped, batch_size):
                                    put(downstream, chunk)
                            dropped.clear()
                    else:
                        passed = set(survivors)
                        dropped.extend(ep for ep in batch if ep not in passed)
                count += len(batch)
                run.progress(sid, done=count, total=len(selection),
                             note=(f"按 episode 流水执行，每批最多 {batch_size} 条；"
                                   f"已完成 {count} 条"), force=True)
                if downstream is not None and survivors:
                    put(downstream, survivors)
            if not abort.is_set():
                finish_stage(st)
                if downstream is not None:
                    put(downstream, sentinel)
        except BaseException as exc:
            with error_lock:
                if not first_error:
                    first_error.append(exc)
            abort.set()
            run.terminate_children()
        finally:
            if child:
                run._detach(child[0])
                child[0].close()

    try:
        with futures.ThreadPoolExecutor(max_workers=len(stages),
                                        thread_name_prefix="funnel") as pool:
            jobs = [pool.submit(worker, i) for i in range(len(stages))]
            for sid, q in zip(ids, queues):
                for batch in _batches(routes[sid], batch_size):
                    put(q, batch)
            put(queues[0], sentinel)
            for job in jobs:
                job.result()
    finally:
        run._pipeline_abort = None
    if first_error:
        raise first_error[0]
    run.check_intent()


def _check_batch(run, st: dict, episodes: list[int], number: int, offset: int,
                 total: int, db_path: str, next_stage: str,
                 child: list[StageWorker]) -> tuple[list[int], bool, str | None]:
    sid, mods = st["id"], list(st["modules"])
    tag = f"{sid}.batch-{number:06d}"
    out_file = run.wd.episodes_file(run.run_key, f"{tag}.out")
    episodes_arg = run.episodes_arg(f"{tag}.in", episodes)
    argv = ["check", "--modules", ",".join(mods), *run.source_args(),
            "--run-dir", str(run.wd.root), "--episodes", "0",
            "--plan-stage", str(run.wd.plan),
            "--resume", "--pipeline-state", db_path, "--pipeline-next", next_stage]
    if st.get("_pipeline_concurrency") is not None:
        argv += ["--concurrency", str(st["_pipeline_concurrency"])]
    vlm = st.get("kind") == "vlm"
    if vlm:
        argv += run.vlm_args()
    if sid == "frame":
        resources.admit_memory(run, sid)
    if os.environ.get("CURATOR_CLI"):
        # A site supplied CLI may add instrumentation or wrap native libraries.
        # Its entry point cannot be imported into a multiprocessing worker.
        external = [*argv]
        external[external.index("--episodes") + 1] = episodes_arg
        external += ["--survivors-out", str(out_file)]
        outcome = run.cli(sid, external, need_input=True, need_vlm=vlm,
                          crash_retry=True, inflight_modules=mods, episodes=len(episodes),
                          progress_offset=offset, progress_total=total)
    else:
        outcome = _exchange_batch(run, sid, argv, episodes_arg, out_file, child,
                                  offset, total, vlm, number, mods)
    if outcome.status in ("terminated", "interrupted", "killed") and run.intent:
        raise Interrupt(run.intent)
    if outcome.ok:
        survivors = read_lines(out_file)
        if survivors is None:
            raise TaskFailure("pipeline_missing_output", f"{sid} 第 {number} 批没有幸存条目文件",
                              stage=sid)
        return survivors, False, None
    if outcome.status == "module_failed":
        message = outcome.message or outcome.reason()
        run.log(sid, "error", f"本档整体失败，放行第 {number} 批的 {len(episodes)} 条 episode："
                              f"{message}")
        correction = EpisodeState(db_path)
        try:
            correction.forward(sid, episodes, next_stage)
        finally:
            correction.close()
        return episodes, True, message
    run.fail_on(outcome, sid)
    raise AssertionError("unreachable")


def _exchange_batch(run, sid, argv, episodes_arg, out_file, child, offset, total,
                    vlm, number, mods):
    for _attempt in range(3):
        run.check_intent()
        if not child:
            cli = run._cli_environment(need_input=True, need_output=False, need_vlm=vlm)
            full = list(argv)
            if cli.input_region:
                full += ["--input-region", cli.input_region]
            if vlm and cli.vlm_api_key_env:
                full += ["--vlm-api-key-env", cli.vlm_api_key_env]
            cmd = CliCommand(full, env=dict(cli.env), stage=sid, cwd=str(run.wd.root))
            proc = StageWorker(cmd, run._line_handler(sid, progress_offset=offset,
                                                      progress_total=total),
                               term_grace_s=run.orch.executor.term_grace_s,
                               int_grace_s=run.orch.executor.int_grace_s)
            child.append(proc)
            run._attach(proc)
            run.log(sid, "debug", f"persistent worker pid={proc.pid}")
        child[0].on_line = run._line_handler(sid, progress_offset=offset,
                                             progress_total=total)
        try:
            outcome = child[0].exchange(episodes_arg, str(out_file))
            break
        except (EOFError, BrokenPipeError, RuntimeError, OSError) as exc:
            inflight = run.inflight(mods)
            run._detach(child[0])
            child[0].close()
            child.clear()
            run.check_intent()
            detail = f"当时在处理 episode {', '.join(map(str, inflight))}" if inflight \
                else "当时没有在途 episode"
            run.log(sid, "error", f"worker 异常退出（{exc}），{detail}；"
                                  f"第 {number} 批从 episode 状态重试")
    else:
        raise TaskFailure("crashed", f"{sid} worker 反复异常退出", stage=sid)
    run.usage.flush()
    return outcome
