"""A stand-in for ``python -m curation.cli`` with just the behaviours the executor tests need.

``fakecli.py <mode> [args...] --json``:

* ``ok`` - a progress line, a usage line, a line that is not JSON, then ``{"ok": true}``;
* ``exit N`` - the error envelope of exit code N;
* ``term`` - works until SIGTERM (exit 5) or SIGINT (exit 130), writing ``pidfile`` args;
* ``stubborn`` - ignores SIGTERM and SIGINT: only SIGKILL ends it;
* ``family PIDFILE`` - starts a grandchild in its own process group-mate, writes its pid;
* ``crash`` - kills itself with SIGSEGV (a native crash);
* ``chatty N`` - N kilobytes on stdout before the document (a full pipe must not block);
* ``noisy`` - prints text on stdout before the JSON document.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def err(line: dict) -> None:
    sys.stderr.write(json.dumps(line) + "\n")
    sys.stderr.flush()


def envelope(code: int, name: str, message: str) -> None:
    print(json.dumps({"schema_version": "1.0", "exit_code": code,
                      "error": {"code": name, "message": message}}))
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--json"]
    mode = args[0] if args else "ok"
    if mode == "ok":
        err({"ts": 1, "kind": "progress", "stage": "check:x", "done": 1, "total": 2})
        err({"ts": 2, "kind": "usage", "model": "m", "module": "task_success",
             "call_kind": "probe", "requests": 1, "prompt_tokens": 10, "completion_tokens": 2,
             "reasoning_tokens": 1, "cached_tokens": 4})
        sys.stderr.write("a native library said something\n")
        sys.stderr.flush()
        err({"ts": 3, "kind": "log", "level": "info", "msg": "done", "episode_index": 3})
        print(json.dumps({"ok": True, "env": os.environ.get("FAKECLI_ECHO")}))
        return 0
    if mode == "exit":
        code = int(args[1])
        names = {1: "internal", 2: "usage", 3: "input_unreachable", 4: "module_failed",
                 5: "terminated", 6: "source_changed", 130: "interrupted"}
        envelope(code, names.get(code, "internal"), f"exit {code} on purpose")
        return code
    if mode == "term":
        pidfile = args[1] if len(args) > 1 else None
        state = {"stop": None}
        signal.signal(signal.SIGTERM, lambda *_: state.update(stop="term"))
        signal.signal(signal.SIGINT, lambda *_: state.update(stop="int"))
        if pidfile:
            with open(pidfile, "w") as fh:
                fh.write(str(os.getpid()))
        err({"ts": 1, "kind": "log", "level": "info", "msg": "working"})
        while state["stop"] is None:
            time.sleep(0.02)
        if state["stop"] == "term":
            envelope(5, "terminated", "stopped on SIGTERM")
            return 5
        envelope(130, "interrupted", "interrupted by SIGINT")
        return 130
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        err({"ts": 1, "kind": "log", "level": "info", "msg": "not listening"})
        while True:
            time.sleep(0.05)
    if mode == "family":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(args[1], "w") as fh:
            fh.write(str(child.pid))
        err({"ts": 1, "kind": "log", "level": "info", "msg": "started a grandchild"})
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(0.05)
    if mode == "crash":
        err({"ts": 1, "kind": "log", "level": "info", "msg": "about to crash"})
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(5)
        return 0
    if mode == "chatty":
        for _ in range(int(args[1])):
            sys.stdout.write("x" * 1023 + "\n")
        print(json.dumps({"ok": True}))
        return 0
    if mode == "noisy":
        print("hello from a library")
        print(json.dumps({"ok": True}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
