"""The CLI skeleton: --json discipline, error envelope, exit codes, signals, credentials."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from curation.cli import app, creds, framework
from curation.cli.errors import (
    InputUnreachable,
    ModuleFailed,
    SourceChanged,
    Terminated,
    UsageError,
)
from curation.contracts import schemas

from .fakes import StubDaemon, make_task

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(capsys, func, *, json_mode=True, log_level="info"):
    args = argparse.Namespace(json=json_mode, log_level=log_level)
    capsys.readouterr()
    rc = framework.run_command(func, args)
    out, err = capsys.readouterr()
    return rc, out, err


def _events(err: str) -> list[dict]:
    events = [json.loads(line) for line in err.splitlines()]
    for ev in events:
        assert schemas.errors("progress.schema.json", ev) == [], ev
    return events


# ---------------------------------------------------------------- output discipline


def test_stray_prints_never_reach_stdout_in_json_mode(capsys):
    def noisy(ctx, args):
        print("[curation] 列出对象清单:27 个文件")          # v1 library style
        print("warning from a library", file=sys.stderr)
        sys.stdout.write("partial line without newline")
        ctx.progress("demo", 1, 2)
        return framework.Result({"ok": True})

    rc, out, err = _run(capsys, noisy)
    assert rc == 0 and out == '{"ok": true}\n'
    events = _events(err)
    msgs = [(e["kind"], e.get("level"), e.get("msg")) for e in events]
    assert ("log", "info", "[curation] 列出对象清单:27 个文件") in msgs
    assert ("log", "warn", "warning from a library") in msgs
    assert ("log", "info", "partial line without newline") in msgs
    assert any(e["kind"] == "progress" and e["stage"] == "demo" for e in events)


def test_human_mode_keeps_library_prints_off_stdout(capsys):
    def noisy(ctx, args):
        print("chatter")
        ctx.log("warn", "careful")
        return framework.Result({"ok": True}, human="done")

    rc, out, err = _run(capsys, noisy, json_mode=False)
    assert rc == 0 and out == "done\n"
    assert "chatter" in err and "warning: careful" in err


@pytest.mark.parametrize("exc,rc,code", [
    (UsageError("bad"), 2, "usage"),
    (InputUnreachable("gone"), 3, "input_unreachable"),
    (SourceChanged("moved", {"key": "meta/info.json"}), 6, "source_changed"),
    (Terminated("stop"), 5, "terminated"),
    (KeyboardInterrupt(), 130, "interrupted"),
    (ModuleFailed("endpoint down"), 4, "module_failed"),
    (RuntimeError("boom"), 1, "internal"),
])
def test_every_failure_ends_in_the_envelope(capsys, exc, rc, code):
    def fails(ctx, args):
        raise exc

    got, out, err = _run(capsys, fails)
    assert got == rc
    doc = json.loads(out)
    assert schemas.errors("cli/error.schema.json", doc) == []
    assert doc["exit_code"] == rc and doc["error"]["code"] == code
    events = _events(err)
    if code == "internal":                           # a bug: the traceback is logged
        assert any(e["level"] == "error" and "RuntimeError: boom" in e["msg"] for e in events)


def test_human_errors_go_to_stderr(capsys):
    def fails(ctx, args):
        raise InputUnreachable("cannot list tos://b/x")

    rc, out, err = _run(capsys, fails, json_mode=False)
    assert rc == 3 and out == "" and err.strip() == "error: cannot list tos://b/x"


def test_log_level_filters_events(capsys):
    def chatty(ctx, args):
        ctx.log("debug", "d")
        ctx.log("info", "i")
        ctx.progress("s", 1, 1)
        ctx.log("warn", "w")
        return framework.Result({})

    _, _, err = _run(capsys, chatty, log_level="warn")
    assert [e.get("msg") for e in _events(err)] == ["w"]
    _, _, err = _run(capsys, chatty, log_level="debug")
    assert [e.get("msg") for e in _events(err) if e["kind"] == "log"] == ["d", "i", "w"]


def test_sigterm_request_stops_at_the_next_check(capsys):
    def worker(ctx, args):
        for i in range(10):
            if i == 3:
                ctx.request_stop()                   # what the SIGTERM handler does
            ctx.check_stop("between episodes")
        return framework.Result({})

    rc, out, _ = _run(capsys, worker)
    assert rc == 5 and json.loads(out)["error"]["code"] == "terminated"


# ---------------------------------------------------------------- argument errors


def test_argument_errors_are_usage_envelopes(cli):
    res = cli("bogus")
    assert res.rc == 2 and "invalid choice" in res.doc["error"]["message"]
    res = cli()
    assert res.rc == 2
    res = cli("preflight")
    assert res.rc == 2 and "--input" in res.doc["error"]["message"]
    res = cli("task", "get")
    assert res.rc == 2


def test_secrets_in_unknown_arguments_are_masked(cli):
    res = cli("preflight", "--input", "x", "--secret-key", "hunter2", "--api-token=abc")
    assert res.rc == 2
    msg = res.doc["error"]["message"]
    assert "hunter2" not in msg and "abc" not in msg
    assert "--secret-key ***" in msg and "--api-token=***" in msg


def _all_options(parser: argparse.ArgumentParser, seen=None):
    seen = seen if seen is not None else set()
    for action in parser._actions:
        yield from action.option_strings
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                if id(sub) not in seen:
                    seen.add(id(sub))
                    yield from _all_options(sub, seen)


def test_no_option_takes_a_credential():
    """Credentials only come from the environment (design doc 02 §2, doc 08 §3)."""
    opts = set(_all_options(app.build_parser()))
    assert "--json" in opts and "--input" in opts                  # the walk works
    secretish = {o for o in opts
                 if any(w in o for w in ("key", "secret", "password", "token", "credential"))}
    # a request id, and the NAME of the variable that holds the model's API key
    assert secretish == {"--idempotency-key", "--vlm-api-key-env"}


# ---------------------------------------------------------------- credentials


def test_credential_roles_and_fallback():
    env = {"CURATION_INPUT_TOS_ACCESS_KEY": "in-ak", "CURATION_INPUT_TOS_SECRET_KEY": "in-sk",
           "TOS_ACCESS_KEY": "ak", "TOS_SECRET_KEY": "sk", "TOS_SESSION_TOKEN": "tok"}
    ci = creds.tos_credentials("input", env)
    co = creds.tos_credentials("output", env)
    assert (ci.access_key, ci.secret_key, ci.session_token) == ("in-ak", "in-sk", None)
    assert (co.access_key, co.secret_key, co.session_token) == ("ak", "sk", "tok")
    assert "sk" not in repr(ci) and "in-sk" not in repr(ci)
    assert creds.tos_credentials("output", {}) is None
    with pytest.raises(UsageError, match="CURATION_OUTPUT_TOS_ACCESS_KEY is not set"):
        creds.tos_credentials("output", {"CURATION_OUTPUT_TOS_SECRET_KEY": "x"})
    with pytest.raises(UsageError, match="CURATION_INPUT_TOS_ACCESS_KEY and"):
        creds.require_tos_credentials("input", {})


def test_region_and_endpoint_rules(monkeypatch):
    monkeypatch.setenv("TOS_ENDPOINT", "https://tos-s3-cn-beijing.ivolces.com")
    assert creds.resolve_region(None) == ("cn-beijing", "https://tos-cn-beijing.ivolces.com")
    assert creds.resolve_region("cn-shanghai") == ("cn-shanghai",
                                                   "https://tos-cn-shanghai.volces.com")


# ---------------------------------------------------------------- entry points


def _python(*args, env=None, **kw):
    return subprocess.run([sys.executable, "-m", "curation.cli", *args], cwd=BACKEND,
                          capture_output=True, text=True, env=env, timeout=120, **kw)


def test_python_m_entry_help_and_version():
    r = _python("--help")
    assert r.returncode == 0
    assert "preflight" in r.stdout and "run, rejudge" in r.stdout      # v1 commands listed
    r = _python("--version")
    assert r.returncode == 0 and r.stdout.startswith("curation ")


def test_console_script_target_is_still_curation_cli_main():
    with open(os.path.join(BACKEND, "pyproject.toml"), encoding="utf-8") as fh:
        assert 'curation = "curation.cli:main"' in fh.read()
    from curation.cli import main

    assert main(["--version"]) == 0


def test_v1_commands_are_handed_to_the_legacy_cli(tmp_path, capsys):
    from curation.cli import main

    (tmp_path / "a.txt").write_text("hello")
    capsys.readouterr()
    assert main(["ls", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "a.txt" in out and "共 0 个目录、1 个文件" in out      # v1's own wording
    with pytest.raises(SystemExit) as info:                        # hidden v1 command
        main(["reprofile", "--help"])
    assert info.value.code == 0


# ---------------------------------------------------------------- signals (subprocess)


@pytest.mark.parametrize("signum,rc,code", [(signal.SIGTERM, 5, "terminated"),
                                            (signal.SIGINT, 130, "interrupted")])
def test_signals_end_with_their_exit_codes(signum, rc, code):
    with StubDaemon() as stub:
        stub.tasks["task_01"] = make_task("task_01", "running")
        env = dict(os.environ, CURATOR_URL=stub.url, CURATOR_USER=stub.user,
                   CURATOR_PASSWORD=stub.password)
        proc = subprocess.Popen([sys.executable, "-m", "curation.cli", "task", "wait", "task_01",
                                 "--poll-interval", "0.1", "--json"], cwd=BACKEND, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 60
            while not any(r["path"].endswith("/tasks/task_01") for r in stub.requests):
                assert proc.poll() is None, proc.communicate()
                assert time.time() < deadline, "the client never polled"
                time.sleep(0.05)
            proc.send_signal(signum)
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == rc, err
        doc = json.loads(out)
        assert schemas.errors("cli/error.schema.json", doc) == []
        assert doc["error"]["code"] == code
        for line in err.splitlines():
            assert schemas.errors("progress.schema.json", json.loads(line)) == [], line
