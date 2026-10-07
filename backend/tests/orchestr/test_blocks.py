"""The two blocks side by side, with stand-in workers (design doc 17 §3; F12.4 acceptance 1 and 2).

Deterministic: the workers commit into the real episode store and answer on the dispatcher's polls, the
frame stage only every few looks, so the VLM block is done long before the CPU block.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from curation.pipeline.episode_state import EpisodeState, state_path
from daemon.exec import CliOutcome
from daemon.orchestr import blocks
from daemon.orchestr import episode_pipeline as ep
from daemon.orchestr.workdir import WorkDir

PLAN = {"schema_version": "2.0", "stages": [
    {"id": "numeric", "kind": "cpu", "command": "check", "block": "cpu", "concurrency": 4,
     "modules": ["timestamp_check"], "episodes": "selected"},
    {"id": "frame", "kind": "cpu", "command": "check", "block": "cpu", "after": "numeric", "concurrency": 4,
     "modules": ["visual_quality"], "episodes": "selected"},
    {"id": "dedup", "kind": "cpu", "command": "check", "block": "cpu", "after": "frame", "full_set": True,
     "concurrency": 1, "modules": ["dedup"], "episodes": "selected"},
    {"id": "autolabel", "kind": "vlm", "command": "autolabel", "block": "vlm", "episodes": "unlabeled",
     "gates": {"caption": 2}},
    {"id": "vlm", "kind": "vlm", "command": "check", "block": "vlm", "after": "autolabel",
     "modules": ["task_success"], "episodes": "selected", "gates": {"episode": 2}},
    {"id": "final", "kind": "aggregate", "command": "aggregate", "phase": "final"},
]}


class Journal:
    def __init__(self):
        self.marks = {}

    def done(self, sid):
        return bool(self.marks.get(sid))

    def mark(self, sid, **fields):
        self.marks[sid] = fields.get("done", True)


class Run:
    cpu_key = ("task", None)

    def __init__(self, root, events):
        self.wd = WorkDir(root, "task")
        self.run_key = "main"
        self.task = SimpleNamespace(params={"batch_size": 2})
        self.journal = Journal()
        self.usage = SimpleNamespace(flush=lambda: None)
        self.cfg = SimpleNamespace(memory_admission=0)
        self.stages = {}
        self._pipeline_abort = None
        self.intent = None
        self.events = events
        self.lock = threading.Lock()

    def note(self, *event):
        with self.lock:
            self.events.append(event)

    def log(self, *args, **kwargs):
        pass

    def progress(self, *args, **kwargs):
        pass

    def modules_running(self, *args):
        pass

    def module_result(self, *args, **kwargs):
        pass

    def stage_done(self, sid, state, **fields):
        self.stages[sid] = state
        self.journal.mark(sid, done=True)

    def check_intent(self):
        if self._pipeline_abort is not None and self._pipeline_abort.is_set():
            raise RuntimeError("the other block gave up")

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

    def module_params(self, module_id):
        return {}

    # the whole-stage steps (StageRun's): what they did, in order
    def autolabel(self, selection):
        if self.journal.done("autolabel"):
            return
        self.note("autolabel", "start", None)
        self.note("autolabel", "done", None)
        self.stage_done("autolabel", "succeeded")

    def check_stage(self, st, episodes, *, fresh, incremental=False):
        if self.journal.done(st["id"]):
            return []
        self.note(st["id"], "start", tuple(episodes))
        self.note(st["id"], "done", tuple(episodes))
        self.stage_done(st["id"], "succeeded")
        return list(episodes)


def install_workers(monkeypatch, run, *, frame_every=6, numeric_error=frozenset()):
    class Worker:
        requested = "term"
        term_grace_s = int_grace_s = 0

        def __init__(self, layer, path, next_stage):
            self.layer, self.path, self.next = layer, path, next_stage
            self.pending, self.ended, self.ticks = [], False, 0
            self.on_line = lambda _: None

        def submit(self, episodes):
            for e in episodes:
                run.note(self.layer.sid, "start", e)
            self.pending.extend(episodes)

        def poll(self):
            self.ticks += 1
            assert self.ticks < 5000, "dispatcher did not make progress"
            sid = self.layer.sid
            if self.pending and not (sid == "frame" and self.ticks % frame_every):
                e = self.pending.pop(0)
                failed = sid == "numeric" and e in numeric_error
                records = {m: {"module": m, "episode_index": e, "status": "error" if failed else "ok",
                               "findings": [], "error": {"kind": "execution", "incidents": []} if failed else None}
                           for m in self.layer.stage["modules"]}
                store = EpisodeState(self.path)
                store.finish(sid, e, records, self.next)
                store.close()
                run.note(sid, "done", e)
                return "episode", {"episode": e, "survivor": True}
            if self.ended and not self.pending:
                return "result", {"returncode": 0}
            return None

        def finish(self):
            self.ended = True

        def close(self):
            pass

        def outcome(self, payload):
            return CliOutcome(returncode=0, status="ok", doc={}, message=None)

    def start(run_, layer, selection, path, next_stage):
        layer.child = Worker(layer, path, next_stage)

    monkeypatch.setattr(ep, "_start_worker", start)
    monkeypatch.setattr(ep.resources, "admit_memory", lambda *a: None)
    monkeypatch.setattr(ep, "compact", lambda *a: None)


def at(events, *event):
    return events.index(event)


def test_the_blocks_run_side_by_side_and_never_filter_each_other(tmp_path, monkeypatch):
    events: list = []
    run = Run(tmp_path, events)
    install_workers(monkeypatch, run, numeric_error={1})
    selection = list(range(6))
    blocks.run_blocks(run, PLAN, selection)
    done = {(sid, e) for sid, kind, e in events if kind == "done"}
    # every per-episode stage judged every episode: an error in numeric stops nothing (D57)
    for sid in ("numeric", "frame", "vlm"):
        assert {e for s, e in done if s == sid} == set(selection), sid
    assert at(events, "numeric", "done", 1) < at(events, "frame", "start", 1)
    # the VLM block does not wait for the CPU block: its last episode is done before frame's last
    last_vlm = max(at(events, "vlm", "done", e) for e in selection)
    last_frame = max(at(events, "frame", "done", e) for e in selection)
    assert last_vlm < last_frame
    # autolabel before the block's checks, the full-set steps after them, on the whole selection
    assert at(events, "autolabel", "done", None) < min(at(events, "vlm", "start", e) for e in selection)
    assert at(events, "dedup", "start", tuple(selection)) > last_frame
    assert run.stages == {"numeric": "completed_with_errors", "frame": "succeeded", "dedup": "succeeded",
                          "autolabel": "succeeded", "vlm": "succeeded"}
    # the store knows where every episode is in each block
    store = EpisodeState(state_path(run.wd.root))
    try:
        assert store.positions(selection, "cpu") == {e: "done" for e in selection}
        assert store.positions(selection, "vlm") == {e: "done" for e in selection}
        assert store.stage_states(1) == {"numeric": "error", "frame": "done", "vlm": "done"}
        assert store.totals() == {"started": 6, "finished": 6}
    finally:
        store.close()


def test_a_resumed_run_goes_on_where_each_block_was(tmp_path, monkeypatch):
    """design doc 17 §3.4: continuing reuses every finished (module, episode); each block from its own place."""
    events: list = []
    run = Run(tmp_path, events)
    install_workers(monkeypatch, run)
    selection = list(range(4))
    blocks.prepare_store(run, PLAN, selection)
    store = EpisodeState(state_path(run.wd.root))
    try:
        ok = {"status": "ok", "findings": [], "error": None}
        for e in selection:                               # the CPU block was through numeric for all
            store.finish("numeric", e, {"timestamp_check": {"module": "timestamp_check", "episode_index": e, **ok}},
                         "frame")
        store.finish("vlm", 0, {"task_success": {"module": "task_success", "episode_index": 0, **ok}}, "done")
    finally:
        store.close()
    run.journal.mark("autolabel", done=True)
    blocks.run_blocks(run, PLAN, selection)
    started = [(sid, e) for sid, kind, e in events if kind == "start"]
    assert not any(sid == "numeric" for sid, _ in started)
    assert sorted(e for sid, e in started if sid == "frame") == selection
    assert sorted(e for sid, e in started if sid == "vlm") == [1, 2, 3]
    assert ("autolabel", "start", None) not in events


def test_a_failing_block_stops_the_other(tmp_path, monkeypatch):
    events: list = []
    run = Run(tmp_path, events)
    install_workers(monkeypatch, run, frame_every=50)

    def broken(st, episodes, *, fresh, incremental=False):
        raise RuntimeError("the full-set stage went wrong")

    run.check_stage = broken
    try:
        blocks.run_blocks(run, PLAN, list(range(4)))
    except RuntimeError as exc:
        assert "the full-set stage went wrong" in str(exc)
    else:
        raise AssertionError("the failure was not raised")
    assert run._pipeline_abort is None
    assert ("dedup", "start", (0, 1, 2, 3)) not in events
