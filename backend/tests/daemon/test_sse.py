"""SSE (design doc 03, section 5): ids, Last-Event-ID replay, reset, throttling, heartbeats."""
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time

import pytest

from daemon.events import EventHub, parse_event_id
from daemon.transitions import change_task_state

from .conftest import T0, assert_error, assert_schema, seed_task

SSE_SCHEMAS = {"state": "SseState", "progress": "SseProgress", "log": "SseLog", "usage": "SseUsage",
               "done": "SseDone"}


class Tick:
    """A monotonic clock for throttling tests."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def parse_stream(text: str) -> list[dict]:
    """SSE text -> [{"id", "event", "data", "comment", "retry"}] per block."""
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev: dict = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                ev["comment"] = line[1:].strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                ev[key] = json.loads(value) if key == "data" else value
        out.append(ev)
    return out


def check_payloads(events):
    for ev in events:
        if ev.get("event") in SSE_SCHEMAS:
            assert_schema(SSE_SCHEMAS[ev["event"]], ev["data"])


# ---------------------------------------------------------------------------
# the hub
# ---------------------------------------------------------------------------

def test_ids_are_epoch_and_a_growing_sequence():
    hub = EventHub(7)
    a = hub.publish_state("task_a", "running", at=T0)
    b = hub.publish_state("task_b", "queued", at=T0)
    c = hub.publish_done("task_a", "succeeded", failed_modules=["dedup", "dedup"])
    assert [parse_event_id(x) for x in (a, b, c)] == [(7, 1), (7, 2), (7, 3)]
    assert [e.event for e in hub.buffered("task_a")] == ["state", "done"]
    assert hub.buffered("task_a")[1].data == {"state": "succeeded", "failed_modules": ["dedup"]}
    assert parse_event_id("1-x") is None and parse_event_id(None) is None


def _subscribe(hub, task_id, last):
    async def go():
        return hub.subscribe(task_id, last)
    return asyncio.run(go())


def test_replay_reset_and_eviction():
    hub = EventHub(3, buffer_size=5)
    ids = [hub.publish_state("t", "running", at=i) for i in range(4)]
    sub = _subscribe(hub, "t", ids[1])
    assert not sub.reset and [hub.event_id(e.seq) for e in sub.replay] == ids[2:]
    assert _subscribe(hub, "t", ids[-1]).replay == []                    # up to date
    for last in ("2-1", "3-999", "garbage", "3-"):                        # other epoch / future / junk
        assert _subscribe(hub, "t", last).reset
    assert _subscribe(hub, "t", None).fresh
    more = [hub.publish_state("t", "running", at=i) for i in range(4)]    # 8 events, 5 kept
    assert _subscribe(hub, "t", ids[1]).reset                             # evicted since
    assert [hub.event_id(e.seq) for e in _subscribe(hub, "t", ids[2]).replay] == ids[3:] + more


def test_discarded_task_buffers_force_a_reset():
    hub = EventHub(1, max_tasks=2)
    seen = hub.publish_state("t1", "running", at=0)
    newest = hub.publish_state("t1", "pausing", at=0)
    hub.publish_state("t2", "running", at=0)
    hub.publish_state("t3", "running", at=0)                              # t1's buffer goes
    assert _subscribe(hub, "t1", seen).reset                             # "pausing" was lost
    assert not _subscribe(hub, "t1", newest).reset                        # nothing was lost
    later = hub.publish_state("t1", "paused", at=0)
    assert _subscribe(hub, "t1", seen).reset                             # history still unknown
    got = _subscribe(hub, "t1", newest)
    assert not got.reset and [hub.event_id(e.seq) for e in got.replay] == [later]


def test_progress_and_usage_throttle_keep_the_newest():
    tick = Tick()
    hub = EventHub(1, clock=tick)
    stage = {"id": "vlm", "state": "running", "total": 49}
    assert hub.publish_progress("t", dict(stage, done=1))
    assert hub.publish_progress("t", dict(stage, done=2)) is None         # within 0.5 s
    assert hub.publish_progress("t", dict(stage, done=3)) is None
    assert hub.publish_usage("t", {"requests": 1})
    assert hub.publish_usage("t", {"requests": 2}) is None
    tick.t += 0.2
    hub.flush()
    assert [e.data.get("done") for e in hub.buffered("t") if e.event == "progress"] == [1]
    tick.t += 0.4
    hub.flush()
    progress = [e.data["done"] for e in hub.buffered("t") if e.event == "progress"]
    usage = [e.data["requests"] for e in hub.buffered("t") if e.event == "usage"]
    assert progress == [1, 3] and usage == [1, 2]                          # middle values dropped
    assert hub.publish_progress("t", dict(stage, done=49))                 # a stage's end: at once
    assert hub.publish_progress("t", {"id": "frame", "state": "failed", "done": 3, "total": 9})


def test_log_rate_limit_counts_what_it_dropped():
    tick = Tick()
    hub = EventHub(1, clock=tick)
    sent = [hub.publish_log("t", "vlm", "info", f"line {i}") for i in range(30)]
    assert sum(x is not None for x in sent) == 20
    tick.t += 1.0
    hub.publish_log("t", "vlm", "warn", "after the burst", episode_index=18)
    last = hub.buffered("t")[-1].data
    assert last == {"stage": "vlm", "level": "warn", "msg": "after the burst", "episode_index": 18,
                    "dropped": 10}
    with pytest.raises(ValueError):
        hub.publish_log("t", "vlm", "fatal", "x")


def test_flusher_thread_delivers_pending_progress():
    hub = EventHub(1, progress_interval_s=0.05)
    hub.start()
    try:
        stage = {"id": "vlm", "state": "running", "total": 10}
        hub.publish_progress("t", dict(stage, done=1))
        hub.publish_progress("t", dict(stage, done=2))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if [e.data["done"] for e in hub.buffered("t")] == [1, 2]:
                break
            time.sleep(0.01)
        assert [e.data["done"] for e in hub.buffered("t")] == [1, 2]
    finally:
        hub.stop()


def test_publish_from_a_worker_thread_wakes_the_stream():
    hub = EventHub(1)

    async def go():
        sub = hub.subscribe("t", None)
        threading.Timer(0.05, hub.publish_state, args=("t", "running"), kwargs={"at": 1}).start()
        assert await sub.wait(5)
        return sub.drain()

    got = asyncio.run(go())
    assert [e.data["state"] for e in got] == ["running"]


def test_a_slow_client_is_marked_overflowed():
    hub = EventHub(1, max_queue=3)

    async def go():
        sub = hub.subscribe("t", None)
        for i in range(5):
            hub.publish_state("t", "running", at=i)
        await asyncio.sleep(0)
        return sub

    sub = asyncio.run(go())
    assert sub.overflowed and len(sub.drain()) == 3


# ---------------------------------------------------------------------------
# finite streams through the app
# ---------------------------------------------------------------------------

def test_terminal_task_gets_state_and_done_then_the_stream_ends(client_for):
    c = client_for(base_path="/curation")
    rt = c.app.state.runtime
    t = seed_task(rt.repo)
    for frm, to in (("queued", "running"), ("running", "completed_with_errors")):
        change_task_state(rt.repo, rt.hub, t.id, {frm}, to, at=T0)
    rt.repo.update_module_state(t.id, "timestamp_check", {"pending"}, "failed")
    r = c.get(f"/curation/events/tasks/{t.id}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    events = parse_stream(r.text)
    assert events[0] == {"retry": "3000"}
    assert [e["event"] for e in events[1:]] == ["state", "done"]
    assert events[2]["data"] == {"state": "completed_with_errors",
                                 "failed_modules": ["timestamp_check"]}
    assert events[1]["id"] == events[2]["id"] == rt.hub.event_id(rt.hub.head)
    check_payloads(events)


def test_last_event_id_replays_what_was_missed(client_for):
    c = client_for()
    rt = c.app.state.runtime
    t = seed_task(rt.repo)
    first = rt.hub.publish_state(t.id, "running", at=T0)
    rt.hub.publish_progress(t.id, {"id": "numeric", "state": "succeeded", "done": 50, "total": 50,
                                   "elapsed_s": 1.0})
    rt.hub.publish_log(t.id, "numeric", "warn", "episode 18 too short (0.5s)")
    rt.hub.publish_usage(t.id, {"prompt_tokens": 1, "completion_tokens": 2, "reasoning_tokens": 0,
                                "cached_tokens": 0, "requests": 1, "requests_unknown_usage": 0})
    change_task_state(rt.repo, rt.hub, t.id, {"queued"}, "running", at=T0)
    change_task_state(rt.repo, rt.hub, t.id, {"running"}, "succeeded", at=T0)
    r = c.get(f"/events/tasks/{t.id}", headers={"Last-Event-ID": first})
    events = parse_stream(r.text)[1:]
    assert [e["event"] for e in events] == ["progress", "log", "usage", "state", "state", "done"]
    seqs = [parse_event_id(e["id"])[1] for e in events]
    assert seqs == sorted(seqs) and seqs[0] == parse_event_id(first)[1] + 1
    check_payloads(events)


def test_unusable_last_event_id_gets_reset_then_a_snapshot(client_for):
    c = client_for()
    rt = c.app.state.runtime
    t = seed_task(rt.repo)
    change_task_state(rt.repo, rt.hub, t.id, {"queued"}, "running", at=T0)
    change_task_state(rt.repo, rt.hub, t.id, {"running"}, "stopping", at=T0)
    change_task_state(rt.repo, rt.hub, t.id, {"stopping"}, "stopped", at=T0)
    r = c.get(f"/events/tasks/{t.id}", headers={"Last-Event-ID": f"{rt.hub.epoch - 1}-5"})
    events = parse_stream(r.text)[1:]
    assert [e["event"] for e in events] == ["reset", "state", "done"]
    assert events[1]["data"]["state"] == "stopped"


def test_unknown_task_is_a_404_before_streaming(client_for):
    c = client_for()
    assert_error(c.get("/events/tasks/task_missing"), "not_found")


def test_event_streams_are_never_gzipped(client_for):
    c = client_for()
    rt = c.app.state.runtime
    t = seed_task(rt.repo)
    change_task_state(rt.repo, rt.hub, t.id, {"queued"}, "running", at=T0)
    for i in range(40):                                   # well over the 1 KiB gzip threshold
        rt.hub.publish_state(t.id, "running", at=T0 + i)
    change_task_state(rt.repo, rt.hub, t.id, {"running"}, "failed", reason="x" * 200, at=T0)
    r = c.get(f"/events/tasks/{t.id}", headers={"Accept-Encoding": "gzip",
                                                "Last-Event-ID": f"{rt.hub.epoch}-1"})
    assert "content-encoding" not in r.headers and len(r.content) > 1024


# ---------------------------------------------------------------------------
# live streams against a real server
# ---------------------------------------------------------------------------

@pytest.fixture
def live(make_app):
    import uvicorn

    app = make_app(base_path="/curation", sse_heartbeat_s=0.2)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield app, port
    server.should_exit = True
    thread.join(10)


class Stream:
    def __init__(self, port, path, headers=None):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.conn.request("GET", path, headers=headers or {})
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200, self.resp.read()

    def next(self) -> dict | None:
        """The next event block (comments included); None when the server closed the stream."""
        lines = []
        while True:
            raw = self.resp.readline()
            if not raw:
                return None
            line = raw.decode().rstrip("\n")
            if line == "":
                if lines:
                    return parse_stream("\n".join(lines))[0]
                continue
            lines.append(line)

    def until(self, event: str) -> dict:
        while True:
            ev = self.next()
            assert ev is not None, f"stream ended before {event}"
            if ev.get("event") == event:
                return ev

    def close(self):
        self.conn.close()


def test_live_events_heartbeats_and_resume(live):
    app, port = live
    rt = app.state.runtime
    t = seed_task(rt.repo)
    change_task_state(rt.repo, rt.hub, t.id, {"queued"}, "running", at=T0)
    s = Stream(port, f"/curation/events/tasks/{t.id}")
    assert s.next() == {"retry": "3000"}
    snap = s.next()
    assert snap["event"] == "state" and snap["data"]["state"] == "running"
    assert s.next() == {"comment": "ping"}                              # heartbeat when idle
    rt.hub.publish_progress(t.id, {"id": "vlm", "state": "running", "done": 28, "total": 49,
                                   "elapsed_s": 230, "eta_s": 170})
    progress = s.until("progress")
    assert progress["data"]["done"] == 28
    s.close()

    time.sleep(0.05)
    rt.hub.publish_log(t.id, "vlm", "warn", "missed while away")         # lands in the buffer
    change_task_state(rt.repo, rt.hub, t.id, {"running"}, "succeeded", at=T0 + 1)
    s2 = Stream(port, f"/curation/events/tasks/{t.id}", {"Last-Event-ID": progress["id"]})
    assert s2.next() == {"retry": "3000"}
    got = [s2.next() for _ in range(3)]
    assert [g["event"] for g in got] == ["log", "state", "done"]
    assert got[0]["data"]["msg"] == "missed while away"
    assert s2.next() is None                                              # done + terminal = end
    s2.close()


def test_live_stream_ends_on_graceful_shutdown(live):
    app, port = live
    rt = app.state.runtime
    t = seed_task(rt.repo)
    s = Stream(port, f"/curation/events/tasks/{t.id}")
    assert s.next() == {"retry": "3000"} and s.next()["event"] == "state"
    rt.begin_shutdown()
    while True:
        ev = s.next()
        if ev is None:
            break
        assert ev == {"comment": "ping"}
    s.close()
