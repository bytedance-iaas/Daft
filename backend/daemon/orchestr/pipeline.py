"""The entry point of a chain's per-episode stages, and batch compatibility for site-supplied CLI wrappers.

A chain is one block's streaming stages (:mod:`.blocks`)."""
from __future__ import annotations

import concurrent.futures as futures
from contextlib import nullcontext
import os
import queue
import threading

from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.records import compact

from . import resources
from .runbase import Interrupt, TaskFailure, input_digest
from .workdir import read_lines


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


def run_chain(run, stages: list[dict], selection: list[int], *,
               abort: threading.Event | None = None, pool_key=None, chain: str | None = None,
               wait_for: tuple[str, str] | None = None) -> None:
    """A chain of per-episode stages over ``selection`` (``abort``, ``pool_key``, ``chain``, ``wait_for``: see
    :func:`.episode_pipeline.run_episodes`; the batch path for external CLI wrappers waits for the whole selection's
    integrity verdicts before it starts)."""
    if not os.environ.get("CURATOR_CLI"):
        from .episode_pipeline import run_episodes

        return run_episodes(run, stages, selection, abort=abort, pool_key=pool_key, chain=chain,
                            wait_for=wait_for)
    return run_batches(run, stages, selection, abort=abort, chain=chain, wait_for=wait_for)


def _wait_for_gate(run, selection: list[int], wait_for: tuple[str, str], abort: threading.Event | None) -> None:
    """Until every episode of ``selection`` is past the other chain's stage (D92: data integrity's verdict is in),
    or that stage failed as a whole."""
    import time

    gate_chain, gate_stage = wait_for
    path = state_path(run.wd.root)
    while abort is None or not abort.is_set():
        run.check_intent()
        store = EpisodeState(path)
        try:
            past = store.positions(selection, gate_chain)
            failed = gate_stage in store.failed_stages()
        finally:
            store.close()
        if failed or all(past.get(e) not in (None, gate_stage) for e in selection):
            return
        time.sleep(0.25)


def run_batches(run, stages: list[dict], selection: list[int], *,
                abort: threading.Event | None = None, pool_key=None, chain: str | None = None,
                wait_for: tuple[str, str] | None = None) -> None:
    """Compatibility path for external CLI wrappers and batch comparison tests.

    Every worker commits each episode into SQLite before handing its batch's
    finished episodes on. The queues hold at most two batches, so a fast upstream layer
    cannot fill memory while a slow VLM layer is working. ``wait_for``: the chain
    starts once the whole selection has data integrity's verdict (D92).
    """
    if not stages:
        return
    if wait_for is not None:
        _wait_for_gate(run, selection, wait_for, abort)
    cpu_shares = cpu_shares_for(stages)
    stages = [{**s, "_pipeline_concurrency": cpu_shares.get(s["id"])} for s in stages]
    ids = [s["id"] for s in stages]
    modules = [m for stage in stages for m in stage["modules"]]
    path = state_path(run.wd.root)
    store = EpisodeState(path)
    try:
        store.bootstrap(str(run.wd.root), modules)
        saved = store.positions(selection, chain or stages[0].get("block"))
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
    own_abort = abort is None
    if own_abort:
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
        last = getattr(run, "last_stage_of", None)     # a CPU half's module is judged later (registry 5.3)
        own = [m for m in mods if last is None or last(m) in (None, sid)]
        index = EpisodeState(path)
        try:
            stage_inputs = sorted(set(index.stage_inputs(mods)) | seen[sid])
            if sid in failed:
                for module in own:
                    total, errors = index.counts(module)
                    run.module_result(module, "failed", total=total, errors=errors,
                                      digest=input_digest(stage_inputs),
                                      error=failure_messages.get(sid, "模块整体失败；本档的门已放行"))
                state = "failed"
            else:
                errors_found = False
                for module in own:
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
        stage_failed = False
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
                    done = batch
                    correction = EpisodeState(path)
                    try:
                        correction.forward(sid, batch, next_stage)
                    finally:
                        correction.close()
                else:
                    guard = cpu_lock if cpu_lock is not None and sid in cpu_shares else nullcontext()
                    with guard:
                        done, module_failed, failure_message = _check_batch(
                            run, st, batch, seen_batches, count, len(selection),
                            str(path), next_stage)
                    if module_failed:
                        stage_failed = True
                        failed.add(sid)
                        failure_messages[sid] = (failure_message or "模块整体失败")[:2000]
                count += len(batch)
                run.progress(sid, done=count, total=len(selection),
                             note=(f"按 episode 流水执行，每批最多 {batch_size} 条；"
                                   f"已完成 {count} 条"), force=True)
                if downstream is not None and done:
                    put(downstream, done)
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

    try:
        with futures.ThreadPoolExecutor(max_workers=len(stages),
                                        thread_name_prefix="batches") as pool:
            jobs = [pool.submit(worker, i) for i in range(len(stages))]
            for sid, q in zip(ids, queues):
                for batch in _batches(routes[sid], batch_size):
                    put(q, batch)
            put(queues[0], sentinel)
            for job in jobs:
                job.result()
    finally:
        if own_abort:
            run._pipeline_abort = None
    if first_error:
        raise first_error[0]
    run.check_intent()


def _check_batch(run, st: dict, episodes: list[int], number: int, offset: int,
                 total: int, db_path: str, next_stage: str) -> tuple[list[int], bool, str | None]:
    """One batch of one stage as one ``check`` call (``CURATOR_CLI``, or the default command line)."""
    sid, mods = st["id"], list(st["modules"])
    tag = f"{sid}.batch-{number:06d}"
    out_file = run.wd.episodes_file(run.run_key, f"{tag}.out")
    episodes_arg = run.episodes_arg(f"{tag}.in", episodes)
    argv = ["check", "--modules", ",".join(mods), *run.source_args(),
            "--run-dir", str(run.wd.root), "--episodes", episodes_arg,
            "--plan-stage", str(run.wd.plan),
            "--resume", "--pipeline-state", db_path, "--pipeline-next", next_stage,
            "--done-out", str(out_file)]
    if st.get("_pipeline_concurrency") is not None:
        argv += ["--concurrency", str(st["_pipeline_concurrency"])]
    vlm = st.get("kind") == "vlm" and run.stage_asks_model(mods)   # an EEF stage without a model: no model
    argv += run.half_args(st, mods)            # a module of two halves: which one, and the model's settings
    argv += run.module_param_args(mods)                        # every module's parameters (registry 2.1)
    if sid == "frame":
        resources.admit_memory(run, sid)
    # a site supplied CLI may add instrumentation or wrap native libraries: one process per batch
    outcome = run.cli(sid, argv, need_input=True, need_vlm=vlm,
                      crash_retry=True, inflight_modules=mods, episodes=len(episodes),
                      progress_offset=offset, progress_total=total)
    if outcome.status in ("terminated", "interrupted", "killed") and run.intent:
        raise Interrupt(run.intent)
    if outcome.ok:
        done = read_lines(out_file)
        if done is None:
            raise TaskFailure("pipeline_missing_output", f"{sid} 第 {number} 批没有写出处理完的条目",
                              stage=sid)
        return done, False, None
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
