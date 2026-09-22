"""The CLI executor: process groups, signals and their escalation, exit codes, C3 lines."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

import pytest

from daemon.exec import CliCommand, Executor, UsageAccumulator, c3
from daemon.exec.runner import parse_stdout, with_pythonpath

FAKE = os.path.join(os.path.dirname(__file__), "fakecli.py")


def _executor(**kw) -> Executor:
    return Executor([sys.executable, FAKE], **kw)


def _cmd(*argv, **kw) -> CliCommand:
    return CliCommand(list(argv), env=dict(os.environ, **kw.pop("env", {})), stage="t", **kw)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                       # a zombie is dead for our purpose
        got, _ = os.waitpid(pid, os.WNOHANG)
        return got == 0
    except ChildProcessError:
        return True
    return True


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_ok_parses_the_document_and_streams_every_stderr_line():
    lines = []
    out = _executor().run(_cmd("ok", env={"FAKECLI_ECHO": "hi"}), on_line=lines.append)
    assert out.status == "ok" and out.returncode == 0 and out.ok
    assert out.doc == {"ok": True, "env": "hi"}
    assert out.stdout_extra is None
    events = [c3.parse(line) for line in lines]
    assert [e["kind"] for e in events] == ["progress", "usage", "log", "log"]
    raw = events[2]
    assert raw["level"] == "warn" and raw["raw"] and "native library" in raw["msg"]
    assert events[3]["episode_index"] == 3 and events[1]["prompt_tokens"] == 10


@pytest.mark.parametrize("code,status,err", [
    (1, "internal", "internal"), (2, "usage", "usage"), (3, "unreachable", "input_unreachable"),
    (4, "module_failed", "module_failed"), (5, "terminated", "terminated"),
    (6, "source_changed", "source_changed"), (130, "interrupted", "interrupted"),
    (9, "unexpected", "internal")])
def test_exit_codes_map_to_the_contract(code, status, err):
    out = _executor().run(_cmd("exit", str(code)))
    assert out.returncode == code and out.status == status
    assert out.error_code == err and out.message == f"exit {code} on purpose"


def test_sigterm_finishes_and_exits_5(tmp_path):
    ex = _executor()
    started = threading.Event()
    pidfile = tmp_path / "pid"

    def on_start(proc):
        assert _wait_for(pidfile.exists)
        started.set()
        proc.terminate()

    out = ex.run(_cmd("term", str(pidfile)), on_start=on_start)
    assert started.is_set()
    assert out.status == "terminated" and out.requested == "term" and not out.escalated


def test_sigint_exits_130(tmp_path):
    pidfile = tmp_path / "pid"

    def on_start(proc):
        assert _wait_for(pidfile.exists)
        proc.interrupt()

    out = _executor().run(_cmd("term", str(pidfile)), on_start=on_start)
    assert out.status == "interrupted" and out.requested == "int"


def test_a_child_that_ignores_signals_is_killed_after_the_grace_period():
    ex = _executor(term_grace_s=0.3, int_grace_s=0.3)
    seen = []

    def on_start(proc):
        assert _wait_for(lambda: seen)            # it said "not listening": handlers are set
        t0 = time.monotonic()
        proc.terminate()
        seen.append(t0)

    out = ex.run(_cmd("stubborn"), on_line=seen.append, on_start=on_start)
    assert out.status == "killed" and out.signal == signal.SIGKILL
    assert out.requested == "term" and out.escalated


def test_nothing_of_the_group_outlives_the_command(tmp_path):
    pidfile = tmp_path / "grandchild"
    ex = _executor(term_grace_s=0.2)

    def on_start(proc):
        assert _wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
        proc.terminate()                          # the command ignores it: SIGKILL follows

    out = ex.run(_cmd("family", str(pidfile)), on_start=on_start)
    assert out.status == "killed"
    grandchild = int(pidfile.read_text())
    assert _wait_for(lambda: not _alive(grandchild), timeout=5), "orphan left behind"
    assert ex.live() == []


def test_a_native_crash_is_crashed_not_killed():
    out = _executor().run(_cmd("crash"))
    assert out.status == "crashed" and out.signal == signal.SIGSEGV and out.requested is None
    assert "killed by SIGSEGV" in out.reason()


def test_a_chatty_stdout_never_blocks_the_child():
    out = _executor().run(_cmd("chatty", "512"), )
    assert out.status == "ok" and out.doc == {"ok": True} and out.stdout_extra


def test_text_before_the_document_is_reported_but_tolerated():
    out = _executor().run(_cmd("noisy"))
    assert out.status == "ok" and out.doc == {"ok": True}
    assert out.stdout_extra == "hello from a library"


def test_a_timeout_kills_the_group():
    out = _executor().run(_cmd("stubborn", timeout_s=0.5))
    assert out.status == "timeout" and out.signal == signal.SIGKILL


def test_kill_all_leaves_no_child(tmp_path):
    ex = _executor()
    procs = [ex.spawn(_cmd("stubborn")) for _ in range(2)]
    assert _wait_for(lambda: len(ex.live()) == 2)
    assert ex.kill_all() == 2
    for p in procs:
        assert ex.finish(p).status == "killed"
    assert ex.live() == []


def test_spawn_failure_is_an_outcome():
    ex = Executor(["/nonexistent/curation-binary"])
    out = ex.run(_cmd("ok"))
    assert out.status == "spawn_failed" and "cannot start" in out.message


def test_pythonpath_puts_the_package_first():
    env = with_pythonpath({"PYTHONPATH": "/somewhere"})
    import curation

    root = os.path.dirname(os.path.dirname(os.path.abspath(curation.__file__)))
    assert env["PYTHONPATH"].split(os.pathsep)[0] == root
    assert "/somewhere" in env["PYTHONPATH"]


def test_parse_stdout_variants():
    assert parse_stdout("") == (None, None)
    assert parse_stdout('{"a": 1}\n') == ({"a": 1}, None)
    assert parse_stdout('x\n{"a": 1}\n') == ({"a": 1}, "x")
    doc, extra = parse_stdout("not json at all")
    assert doc is None and extra == "not json at all"


def test_c3_parse_normalizes_and_tolerates():
    ev = c3.parse('{"kind": "usage", "model": "m", "module": "x", "call_kind": "probe", '
                  '"requests": 2, "prompt_tokens": "bad"}')
    assert ev["prompt_tokens"] == 0 and ev["requests"] == 2 and ev["ledger"] == "actual"
    assert c3.parse('{"kind": "log", "level": "loud", "msg": 3}')["level"] == "info"
    assert c3.parse('{"kind": "weird"}')["raw"] is True
    assert c3.parse("[1, 2]")["kind"] == "log"
    assert c3.parse('{"kind": "progress", "stage": "s", "done": 3, "total": -1}')["total"] == 0


def test_usage_accumulates_per_bucket_and_flushes_in_one_call():
    calls, published = [], []

    class Repo:
        def usage_buckets(self, task_id, *, ledger):
            from daemon.repo import protocol as P

            return [P.UsageBucket(task_id=task_id, ledger="actual", module_id="a",
                                  call_kind="probe", model_name="m", prompt_tokens=100,
                                  requests=1)]

        def add_usage(self, deltas, *, at):
            calls.append((deltas, at))

    acc = UsageAccumulator(Repo(), "task_1", subtask_id="sub_1", clock=lambda: 7,
                           publish=published.append)
    ev = c3.parse('{"kind": "usage", "model": "m", "module": "task_success", '
                  '"call_kind": "probe", "requests": 1, "prompt_tokens": 10, '
                  '"completion_tokens": 2, "reasoning_tokens": 0, "cached_tokens": 5}')
    acc.add(ev)
    acc.add(ev)
    acc.add({**ev, "ledger": "attributed"})
    assert acc.totals()["prompt_tokens"] == 120            # actual only, base included
    assert published[-1]["prompt_tokens"] == 120 and len(published) == 2
    assert acc.flush() == 2
    deltas, at = calls[0]
    assert at == 7
    by = {(d.ledger, d.subtask_id): d for d in deltas}
    assert by[("actual", "sub_1")].prompt_tokens == 20 and by[("actual", "sub_1")].requests == 2
    assert by[("attributed", "sub_1")].prompt_tokens == 10
    assert acc.flush() == 0
