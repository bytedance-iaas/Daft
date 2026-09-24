"""Deterministic scheduling tests with a slow episode and bounded credits."""
from collections import Counter
from types import SimpleNamespace

import pytest

from curation.pipeline.episode_state import EpisodeState, state_path
from daemon.exec import CliOutcome
from daemon.orchestr import episode_pipeline as ep
from daemon.orchestr.workdir import WorkDir


class Run:
    def __init__(self, root, batch):
        self.wd = WorkDir(root, "task")
        self.run_key = "main"
        self.task = SimpleNamespace(params={"batch_size": batch})
        self.journal = SimpleNamespace(done=lambda _: False)
        self.usage = SimpleNamespace(flush=lambda: None)
        self.cfg = SimpleNamespace(memory_admission=0)
        self.stages = {}
        self._pipeline_abort = None
        self.intent = None
        self.activity = []

    def log(self, *args, **kwargs):
        pass

    def progress(self, *args, **kwargs):
        if "pipeline" in kwargs:
            self.activity.append((args[0], kwargs["pipeline"]))

    def modules_running(self, *args):
        pass

    def module_result(self, *args, **kwargs):
        pass

    def stage_done(self, sid, state):
        self.stages[sid] = state

    def check_intent(self):
        pass

    def sync_quietly(self, *args, **kwargs):
        pass

    def terminate_children(self):
        pass

    def _detach(self, proc):
        pass

    def inflight(self, mods):
        return []

    def fail_on(self, outcome, sid):
        raise AssertionError(outcome)


STAGES = [
    {"id": "numeric", "kind": "cpu", "concurrency": 4,
     "modules": ["timestamp_check"]},
    {"id": "frame", "kind": "cpu", "concurrency": 4,
     "modules": ["visual_quality"]},
    {"id": "vlm", "kind": "vlm", "gates": {"episode": 2},
     "modules": ["task_success"]},
]


def install_workers(monkeypatch, run, *, lost_completion=False, fail_frame=False,
                    broken_send=False):
    events, workers, attempts = [], {}, Counter()
    finished = set()
    lost = False
    disconnected = False

    class Worker:
        requested = "term"
        term_grace_s = int_grace_s = 0

        def __init__(self, layer, path, next_stage):
            self.layer, self.path, self.next = layer, path, next_stage
            self.pending = []
            self.ended = False
            self.on_line = lambda _: None
            self.ticks = 0

        def submit(self, episodes):
            nonlocal disconnected
            if broken_send and self.layer.sid == "numeric" and not disconnected:
                disconnected = True
                raise BrokenPipeError("worker exited before dispatch")
            for e in episodes:
                events.append(("start", self.layer.sid, e))
                attempts[(self.layer.sid, e)] += 1
            self.pending.extend(episodes)

        def poll(self):
            nonlocal lost
            self.ticks += 1
            assert self.ticks < 2000, "dispatcher did not make progress"
            sid = self.layer.sid
            if fail_frame and sid == "frame" and ("frame", 0) in finished:
                return "result", {"returncode": 4}
            for e in self.pending:
                # Episode 0 waits for episode 2, which requires cross-batch refill.
                if sid == "vlm" and e == 0 and ("vlm", 2) not in finished:
                    continue
                if sid == "frame" and e == 1 and ("vlm", 0) not in {
                        (stage, ep) for kind, stage, ep in events if kind == "start"}:
                    continue
                self.pending.remove(e)
                verdict = "fail" if fail_frame and sid == "frame" and e == 0 else "pass"
                records = {m: {"module": m, "episode_index": e, "verdict": verdict}
                           for m in self.layer.stage["modules"]}
                store = EpisodeState(self.path)
                store.finish(sid, e, records, self.next)
                store.close()
                finished.add((sid, e))
                events.append(("done", sid, e))
                if lost_completion and sid == "numeric" and not lost:
                    lost = True
                    raise EOFError("commit succeeded, completion notification lost")
                return "episode", {"episode": e, "survivor": verdict == "pass"}
            if self.ended and not self.pending:
                return "result", {"returncode": 0}
            return None

        def finish(self):
            self.ended = True

        def close(self):
            pass

        def outcome(self, payload):
            rc = payload["returncode"]
            return CliOutcome(returncode=rc, status="module_failed" if rc else "ok",
                              doc={}, message="injected module failure" if rc else None)

    def start(run, layer, selection, path, next_stage):
        child = Worker(layer, path, next_stage)
        workers.setdefault(layer.sid, []).append(child)
        layer.child = child

    monkeypatch.setattr(ep, "_start_worker", start)
    monkeypatch.setattr(ep.resources, "admit_memory", lambda *a: None)
    monkeypatch.setattr(ep, "compact", lambda *a: None)
    return events, attempts


@pytest.mark.parametrize("batch", [1, 2, 8])
def test_handoff_and_refill_do_not_wait_for_slow_episode(tmp_path, monkeypatch, batch):
    run = Run(tmp_path, batch)
    events, _ = install_workers(monkeypatch, run)
    ep.run_episodes(run, STAGES, [0, 1, 2, 3])
    assert events.index(("start", "vlm", 0)) < events.index(("done", "frame", 1))
    assert events.index(("start", "vlm", 2)) < events.index(("done", "vlm", 0))
    active = Counter()
    maximum = Counter()
    for kind, stage, _ in events:
        active[stage] += 1 if kind == "start" else -1
        maximum[stage] = max(maximum[stage], active[stage])
        assert active["numeric"] + active["frame"] <= 4
        assert active["vlm"] <= 2
    assert maximum["vlm"] == 2
    vlm = [activity for sid, activity in run.activity if sid == "vlm"]
    assert max(row["inflight"] for row in vlm) == 2
    assert vlm[-1]["inflight"] == 0
    assert vlm[-1]["dispatches"] >= 2
    assert vlm[-1]["recent"][-1]["episodes"]
    assert run.stages == {s["id"]: "succeeded" for s in STAGES}


def test_crash_after_commit_routes_without_repeating_work(tmp_path, monkeypatch):
    run = Run(tmp_path, 2)
    events, attempts = install_workers(monkeypatch, run, lost_completion=True)
    ep.run_episodes(run, STAGES, [0, 1, 2, 3])
    assert attempts[("numeric", 0)] == 1
    assert attempts[("vlm", 0)] == 1
    assert len([e for e in events if e[:2] == ("done", "vlm")]) == 4


def test_worker_exit_during_send_retries_the_unsent_credit(tmp_path, monkeypatch):
    run = Run(tmp_path, 2)
    events, attempts = install_workers(monkeypatch, run, broken_send=True)
    ep.run_episodes(run, STAGES, [0, 1, 2, 3])
    assert attempts[("numeric", 0)] == 1
    assert len([e for e in events if e[:2] == ("done", "vlm")]) == 4


def test_module_failure_releases_previously_rejected_episodes(tmp_path, monkeypatch):
    run = Run(tmp_path, 1)
    # A single CPU slot also proves the dispatcher releases it between episodes.
    stages = [{**s, "concurrency": 1} if s["kind"] == "cpu" else s for s in STAGES]
    events, attempts = install_workers(monkeypatch, run, fail_frame=True)
    ep.run_episodes(run, stages, [0, 1, 2, 3])
    assert run.stages["frame"] == "failed"
    assert {e for kind, sid, e in events if kind == "done" and sid == "vlm"} == {0, 1, 2, 3}
    assert all(attempts[("vlm", e)] == 1 for e in range(4))
    store = EpisodeState(state_path(run.wd.root))
    try:
        assert store.failed_stages() == {"frame": "injected module failure"}
    finally:
        store.close()
