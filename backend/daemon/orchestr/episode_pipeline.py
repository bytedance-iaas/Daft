"""Credit-based episode dispatch to one persistent process per funnel layer."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
import time

from curation.pipeline.episode_state import EpisodeState, state_path
from curation.pipeline.records import compact
from curation.pipeline.timing import processing_times
from daemon.exec import CliCommand

from . import resources
from .runbase import TaskFailure, input_digest
from .stage_worker import StageWorker
from .workdir import write_lines


@dataclass
class Layer:
    stage: dict
    width: int
    ready: deque = field(default_factory=deque)
    active: set[int] = field(default_factory=set)
    seen: set[int] = field(default_factory=set)
    child: StageWorker | None = None
    closing: bool = False
    done: bool = False
    started: bool = False
    failed: str | None = None
    crashes: int = 0
    completed: int = 0
    send_error: OSError | None = None
    dispatches: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=5))
    started_at: int | None = None
    finished_at: int | None = None
    last_snapshot: tuple | None = None
    timed: set[int] = field(default_factory=set)
    processing_total_s: float = 0.0
    processing_min_s: float | None = None
    processing_max_s: float | None = None

    @property
    def sid(self):
        return self.stage["id"]

    def record_time(self, episode, seconds):
        if seconds is None or episode in self.timed:
            return
        self.timed.add(episode)
        self.processing_total_s += seconds
        self.processing_min_s = min(self.processing_min_s, seconds) if self.processing_min_s is not None else seconds
        self.processing_max_s = max(self.processing_max_s, seconds) if self.processing_max_s is not None else seconds


def _publish_activity(run, layer):
    snapshot = (len(layer.active), len(layer.ready), layer.dispatches,
                layer.started_at, layer.finished_at, len(layer.timed))
    if snapshot == layer.last_snapshot:
        return
    layer.last_snapshot = snapshot
    run.progress(layer.sid, pipeline={
        "inflight": len(layer.active), "queued": len(layer.ready),
        "capacity": layer.width, "dispatches": layer.dispatches,
        "recent": list(layer.recent), "started_at": layer.started_at,
        "finished_at": layer.finished_at, "updated_at": int(time.time() * 1000),
        "processing": {"count": len(layer.timed),
                       "total_s": round(layer.processing_total_s, 3),
                       "mean_s": round(layer.processing_total_s / len(layer.timed), 3) if layer.timed else None,
                       "min_s": layer.processing_min_s, "max_s": layer.processing_max_s},
    })


def _start_worker(run, layer, selection, path, next_stage):
    st, sid = layer.stage, layer.sid
    vlm = st.get("kind") == "vlm"
    argv = ["check", "--modules", ",".join(st["modules"]), *run.source_args(),
            "--run-dir", str(run.wd.root), "--episodes", "0",
            "--plan-stage", str(run.wd.plan), "--resume",
            "--pipeline-state", str(path), "--pipeline-next", next_stage]
    if not vlm:
        argv += ["--concurrency", str(layer.width)]
    else:
        argv += run.vlm_args()
    cli = run._cli_environment(need_input=True, need_output=False, need_vlm=vlm)
    if cli.input_region:
        argv += ["--input-region", cli.input_region]
    if vlm and cli.vlm_api_key_env:
        argv += ["--vlm-api-key-env", cli.vlm_api_key_env]
    proc = StageWorker(CliCommand(argv, env=dict(cli.env), stage=sid, cwd=str(run.wd.root)),
                       run._line_handler(sid, progress_offset=layer.completed,
                                         progress_total=len(selection)),
                       term_grace_s=run.orch.executor.term_grace_s,
                       int_grace_s=run.orch.executor.int_grace_s)
    layer.child = proc
    run._attach(proc)
    run.log(sid, "debug", f"persistent worker pid={proc.pid}; episode concurrency={layer.width}")
    proc.start_stream(run.episodes_arg(f"{sid}.stream", selection))


def _close_worker(run, layer):
    if layer.child is not None:
        # Drain/kill before removing the process from crash recovery bookkeeping.
        layer.child.close()
        run._detach(layer.child)
        layer.child = None


def _drain_stopping_worker(run, layer):
    proc = layer.child
    if proc is None:
        return
    grace = proc.int_grace_s if proc.requested == "int" else proc.term_grace_s
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            event = proc.poll()
        except (EOFError, OSError):
            break
        if event is None:
            time.sleep(0.01)
        elif event[0] == "event":
            proc.on_line(event[1])
        elif event[0] == "result":
            break


def _finish_stage(run, layer, store):
    sid, mods = layer.sid, layer.stage["modules"]
    if run.journal.done(sid):
        return
    inputs = sorted(set(store.stage_inputs(mods)) | layer.seen)
    survivors = inputs if layer.failed else store.survivors(mods)
    write_lines(run.wd.episodes_file(run.run_key, f"{sid}.out"), survivors)
    errors_found = False
    for module in mods:
        total, errors = store.counts(module)
        errors_found |= errors > 0
        state = "failed" if layer.failed else "completed_with_errors" if errors else "succeeded"
        run.module_result(module, state, total=total, errors=errors,
                          digest=input_digest(inputs), error=layer.failed)
        compact(str(run.wd.root), module)
    state = ("failed" if layer.failed else "skipped" if not inputs else
             "completed_with_errors" if errors_found else "succeeded")
    run.progress(sid, done=len(inputs), total=len(inputs), force=True)
    run.stage_done(sid, state)


def run_episodes(run, stages: list[dict], selection: list[int]) -> None:
    from .pipeline import cpu_shares_for, effective_batch_size

    if not stages:
        return
    shares = cpu_shares_for(stages)
    cpu_budget = min((int(s.get("concurrency") or 1) for s in stages
                      if s["id"] in shares), default=1)
    layers = [Layer(s, shares.get(s["id"], int((s.get("gates") or {}).get("episode") or 1)))
              for s in stages]
    batch_size = effective_batch_size(stages, len(selection),
                                     (run.task.params or {}).get("batch_size"))
    store = EpisodeState(state_path(run.wd.root))
    abort = threading.Event()
    run._pipeline_abort = abort
    sync_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-sync")
    sync_jobs = []
    try:
        store.bootstrap(str(run.wd.root), [m for s in stages for m in s["modules"]])
        store.seed_progress(selection, [(s["id"], s["modules"]) for s in stages])
        failures = store.failed_stages()
        for i, layer in enumerate(layers):
            for episode in store.stage_inputs(layer.stage["modules"]):
                layer.record_time(episode, processing_times(store.episode_records(episode)).get(layer.sid))
            layer.failed = failures.get(layer.sid)
            if layer.failed:
                store.reopen_gate(layer.sid, layers[i + 1].sid if i + 1 < len(layers) else "done")
        saved = store.progress_for(selection)
        by_id = {layer.sid: layer for layer in layers}
        for ep in selection:
            dest = saved.get(ep, layers[0].sid)
            if dest in by_id:
                by_id[dest].ready.append(ep)
        run.log("system", "info", f"流水线：逐条交接并持续补位，每次最多派发 {batch_size} 条；"
                                  + "，".join(f"{l.sid} 并发 {l.width}" for l in layers))
        for layer in layers:
            if not run.journal.done(layer.sid):
                run.progress(layer.sid, state="pending", done=0, total=len(selection),
                             note=f"逐条交接，最多 {layer.width} 条在途")

        while not all(layer.done for layer in layers):
            run.check_intent()
            changed = False
            # Drain completion messages before allocating any new credits.
            for i, layer in enumerate(layers):
                proc = layer.child
                if proc is None or layer.done:
                    continue
                downstream = layers[i + 1] if i + 1 < len(layers) else None
                next_stage = downstream.sid if downstream else "done"
                try:
                    if layer.send_error is not None:
                        error, layer.send_error = layer.send_error, None
                        raise error
                    for _ in range(256):
                        event = proc.poll()
                        if event is None:
                            break
                        changed = True
                        kind, payload = event
                        if kind == "event":
                            proc.on_line(payload)
                        elif kind == "episode":
                            ep = payload["episode"]
                            layer.active.remove(ep)
                            layer.record_time(ep, processing_times(store.episode_records(ep)).get(layer.sid))
                            layer.completed += 1
                            layer.crashes = 0
                            if payload["survivor"]:
                                if downstream is not None:
                                    downstream.ready.append(ep)
                        elif kind == "result":
                            outcome = proc.outcome(payload)
                            run.check_intent()
                            if outcome.status == "module_failed":
                                layer.failed = (outcome.message or outcome.reason())[:2000]
                                store.fail_stage(layer.sid, layer.failed)
                                reopened = store.reopen_gate(layer.sid, next_stage)
                                layer.ready.extend(sorted(set(reopened) | layer.active))
                                layer.active.clear()
                                run.log(layer.sid, "error", f"模块整体失败，放行本层输入：{layer.failed}")
                            elif not outcome.ok:
                                run.fail_on(outcome, layer.sid)
                            elif layer.active or not layer.closing:
                                raise TaskFailure("worker_protocol", "worker 提前结束", stage=layer.sid)
                            _close_worker(run, layer)
                            break
                        else:
                            raise TaskFailure("worker_protocol", f"未知 worker 事件 {kind}", stage=layer.sid)
                except (EOFError, BrokenPipeError, OSError) as exc:
                    inflight = run.inflight(layer.stage["modules"])
                    _close_worker(run, layer)
                    run.check_intent()
                    layer.crashes += 1
                    if layer.crashes >= 3:
                        raise TaskFailure("crashed", f"{layer.sid} worker 反复异常退出", stage=layer.sid) from exc
                    # The child may have committed a result before its notification
                    # reached us. Route those records instead of executing them twice.
                    positions = store.progress_for(sorted(layer.active))
                    for ep in sorted(layer.active):
                        dest = positions.get(ep, layer.sid)
                        if dest == layer.sid:
                            layer.ready.appendleft(ep)
                        else:
                            layer.completed += 1
                            layer.record_time(ep, processing_times(store.episode_records(ep)).get(layer.sid))
                            if dest == next_stage and downstream is not None:
                                downstream.ready.append(ep)
                    layer.active.clear()
                    layer.closing = False
                    detail = f"当时在处理 episode {', '.join(map(str, inflight))}" if inflight else "当时没有在途 episode"
                    run.log(layer.sid, "error", f"worker 异常退出（{exc}），{detail}；从 episode 状态重试")

            # Downstream gets first use of released CPU credits. Its queue drains
            # before upstream produces more, including when there is just one CPU.
            cpu_active = sum(len(l.active) for l in layers if l.sid in shares)
            for i in range(len(layers) - 1, -1, -1):
                layer = layers[i]
                if layer.done:
                    continue
                downstream = layers[i + 1] if i + 1 < len(layers) else None
                next_stage = downstream.sid if downstream else "done"
                capacity = layer.width - len(layer.active)
                if layer.sid in shares:
                    capacity = min(capacity, cpu_budget - cpu_active)
                if downstream is not None:
                    capacity = min(capacity, max(0, max(2 * batch_size, downstream.width)
                                                 - len(downstream.ready)))
                if layer.sid == "frame" and not layer.failed and capacity > 0 and layer.ready:
                    # Admission must not block the event loop: completed VLM/CPU
                    # results still need draining while frame memory is scarce.
                    limit = float(run.cfg.memory_admission or 0)
                    use = resources.memory_use() if limit > 0 else None
                    if use is not None and use > limit:
                        capacity = 0
                n = min(batch_size, capacity, len(layer.ready))
                if n > 0:
                    if not layer.started:
                        layer.started = True
                        layer.started_at = int(time.time() * 1000)
                        run.modules_running(layer.stage["modules"])
                        run.progress(layer.sid, state="running")
                    episodes = [layer.ready.popleft() for _ in range(n)]
                    layer.seen.update(episodes)
                    if layer.failed:
                        store.forward(layer.sid, episodes, next_stage)
                        if downstream is not None:
                            downstream.ready.extend(episodes)
                        layer.completed += len(episodes)
                    else:
                        if layer.child is None:
                            _start_worker(run, layer, selection, store.path, next_stage)
                        layer.active.update(episodes)
                        try:
                            layer.child.submit(episodes)
                        except OSError as exc:
                            # Reconcile both sent and unsent credits through the
                            # same durable recovery path on the next iteration.
                            layer.send_error = exc
                        else:
                            layer.dispatches += 1
                            layer.recent.append({"number": layer.dispatches,
                                                 "count": len(episodes),
                                                 "episodes": episodes[:16],
                                                 "at": int(time.time() * 1000)})
                        if layer.sid in shares:
                            cpu_active += len(episodes)
                    changed = True
                upstream_done = i == 0 or layers[i - 1].done
                if upstream_done and not layer.ready and not layer.active:
                    if layer.child is not None and not layer.closing:
                        try:
                            layer.child.finish()
                        except OSError as exc:
                            layer.send_error = exc
                        layer.closing = True
                        changed = True
                    elif layer.child is None:
                        layer.done = True
                        layer.finished_at = int(time.time() * 1000)
                        _publish_activity(run, layer)
                        _finish_stage(run, layer, store)
                        sync_jobs.append(sync_pool.submit(run.sync_quietly, layer.sid))
                        changed = True
            for layer in layers:
                _publish_activity(run, layer)
            if not changed:
                abort.wait(0.01)
        run.usage.flush()
        for job in sync_jobs:
            job.result()
    finally:
        if any(layer.child is not None for layer in layers):
            run.terminate_children()
        for layer in layers:
            _drain_stopping_worker(run, layer)
            _close_worker(run, layer)
            layer.active.clear()
            _publish_activity(run, layer)
        sync_pool.shutdown(wait=True)
        run.usage.flush()
        store.close()
        run._pipeline_abort = None
