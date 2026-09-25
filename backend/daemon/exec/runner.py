"""Run one ``curation`` command as a child process (design doc 00 §2.1, 02 §4 and §5).

Why a subprocess and not an import: a native crash or an out-of-memory kill takes
the child with it, never the Daemon; stopping is a signal to a process group.

What one run looks like:

* the child gets its **own process group** (``start_new_session``), so a signal
  reaches the command and anything it started, and nothing it leaves behind
  survives: after the command exits, whatever is left of its group is killed;
* its environment comes from the caller (W8's ``cli_environment``: keys in the
  environment, never in argv) plus ``PYTHONPATH`` so ``python -m curation.cli``
  resolves the same package the Daemon runs, plus one native thread per worker
  (``THREAD_ENV``: the Daemon already starts one CPU worker per core, D54);
* on Linux its ``oom_score_adj`` is raised (+500 by default, design doc 09 §2.1) so
  the kernel kills a child before the Daemon when the container runs out of memory;
* ``--json`` is always passed: stdout carries exactly one JSON document (the
  result, or the error envelope on a non-zero exit), stderr one C3 event per line;
  every stderr line is handed to ``on_line`` as it arrives - a line that is not
  JSON is tolerated (the caller logs it), and stdout is read to the end so a
  chatty child never blocks on a full pipe;
* signals (02 §4, P9): :meth:`CliProcess.terminate` sends SIGTERM (finish the
  episodes in flight, exit 5) and SIGKILL after 90 s; :meth:`CliProcess.interrupt`
  sends SIGINT (abandon requests in flight, exit 130) and SIGKILL after 10 s.

The outcome maps the exit code onto the contract's meaning (``status``), keeps the
parsed JSON document and says which stop the Daemon had asked for, so the
orchestration decides what the task becomes - never this module.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger("daemon.exec")

#: SIGTERM -> SIGKILL, and SIGINT -> SIGKILL (design doc 02 §4).
TERM_GRACE_S = 90.0
INT_GRACE_S = 10.0
#: The child's oom_score_adj (design doc 09 §2.1); the Daemon tries -500 for itself.
CHILD_OOM_SCORE_ADJ = 500
DAEMON_OOM_SCORE_ADJ = -500

#: exit code -> status (02 §4, C2 1.1). Anything else is "unexpected"; a negative
#: return code (killed by a signal) is "crashed" unless the Daemon asked for it.
EXIT_STATUS = {0: "ok", 1: "internal", 2: "usage", 3: "unreachable", 4: "module_failed",
               5: "terminated", 6: "source_changed", 7: "rejected", 8: "wait_timeout",
               130: "interrupted"}

#: Native thread pools of a child: one thread each, since the Daemon already runs one CPU
#: worker per core (D54) and 30 workers each starting a pool per core only fight over the
#: cores. Set only where the Daemon's own environment does not (``extraEnv`` can raise
#: them); ``curation check`` applies ``OMP_NUM_THREADS`` to OpenCV's pool as well.
THREAD_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
              "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"}

_TAIL_LINES = 40
_MAX_LINE = 64 * 1024


def default_program() -> list[str]:
    """``$CURATOR_CLI`` (split like a shell would), else ``<this python> -m curation.cli``."""
    raw = os.environ.get("CURATOR_CLI", "").strip()
    if raw:
        return shlex.split(raw)
    return [sys.executable, "-m", "curation.cli"]


def package_root() -> str:
    """The directory that holds the ``curation`` package (goes on the child's PYTHONPATH)."""
    import curation

    return str(pathlib.Path(curation.__file__).resolve().parent.parent)


def with_pythonpath(env: dict[str, str]) -> dict[str, str]:
    root = package_root()
    parts = [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    if root not in parts:
        parts.insert(0, root)
    return {**env, "PYTHONPATH": os.pathsep.join(parts)}


def child_env(env: dict[str, str]) -> dict[str, str]:
    """The environment a CLI child starts with: ``PYTHONPATH`` and ``THREAD_ENV`` added."""
    return {**THREAD_ENV, **with_pythonpath(env)}


def set_oom_score_adj(pid: int | str, value: int) -> bool:
    """Best effort (Linux only): raising is always allowed, lowering needs CAP_SYS_RESOURCE."""
    path = f"/proc/{pid}/oom_score_adj"
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(int(value)))
        return True
    except OSError:
        return False


@dataclass
class CliCommand:
    """One command: ``argv`` are the arguments after the program (``--json`` is added)."""

    argv: list[str]
    env: dict[str, str] = field(repr=False)
    stage: str = ""
    cwd: str | None = None
    #: a hard limit; the process group is killed when it is over (None: no limit)
    timeout_s: float | None = None

    def describe(self) -> str:
        """The command line for logs. Keys never travel in argv (08 §3), so this is safe."""
        return "curation " + " ".join(shlex.quote(a) for a in self.argv)


@dataclass
class CliOutcome:
    returncode: int | None
    status: str
    doc: Any = None
    error_code: str | None = None
    message: str | None = None
    details: dict | None = None
    signal: int | None = None
    requested: str | None = None        # the stop the Daemon asked for: term | int | kill
    escalated: bool = False             # SIGKILL had to follow
    stdout_extra: str | None = None     # anything on stdout that was not the one document
    stderr_tail: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def reason(self) -> str:
        """One line for logs and state reasons (English CLI text is kept as it came)."""
        if self.message:
            return self.message
        if self.signal is not None:
            try:
                name = signal.Signals(self.signal).name
            except ValueError:
                name = str(self.signal)
            return f"killed by {name}"
        return f"exit code {self.returncode}"


class CliProcess:
    """One child: start, stream stderr, signal, wait."""

    def __init__(self, cmd: CliCommand, program: list[str], *,
                 on_line: Callable[[str], None] | None = None,
                 term_grace_s: float = TERM_GRACE_S, int_grace_s: float = INT_GRACE_S,
                 oom_score_adj: int | None = CHILD_OOM_SCORE_ADJ):
        self.cmd = cmd
        self.program = list(program)
        self.on_line = on_line
        self.term_grace_s = term_grace_s
        self.int_grace_s = int_grace_s
        self.oom_score_adj = oom_score_adj
        self.proc: subprocess.Popen | None = None
        self.requested: str | None = None
        self.escalated = False
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._out = bytearray()
        self._tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._readers: list[threading.Thread] = []
        self._started = 0.0
        self._timed_out = False

    # -- lifecycle ----------------------------------------------------------------
    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        argv = [*self.program, *self.cmd.argv, "--json"]
        env = child_env(dict(self.cmd.env))
        self._started = time.monotonic()
        self.proc = subprocess.Popen(argv, env=env, cwd=self.cmd.cwd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True, close_fds=True)
        if self.oom_score_adj is not None and sys.platform.startswith("linux"):
            set_oom_score_adj(self.proc.pid, self.oom_score_adj)
        self._readers = [
            threading.Thread(target=self._read_stdout, name=f"cli-out-{self.proc.pid}",
                             daemon=True),
            threading.Thread(target=self._read_stderr, name=f"cli-err-{self.proc.pid}",
                             daemon=True),
        ]
        for th in self._readers:
            th.start()
        if self.cmd.timeout_s:
            self._arm(self.cmd.timeout_s, timeout=True)

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for chunk in iter(lambda: self.proc.stdout.read(65536), b""):
            self._out += chunk

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw[:_MAX_LINE].decode("utf-8", "replace").rstrip("\r\n")
            if not line.strip():
                continue
            self._tail.append(line[:2000])
            if self.on_line is not None:
                try:
                    self.on_line(line)
                except Exception:  # noqa: BLE001 - a bad handler must not stop the reader
                    log.exception("stderr handler failed on a line of %s", self.cmd.stage)

    # -- signals ----------------------------------------------------------------------
    def _signal_group(self, sig: int) -> bool:
        if self.proc is None:
            return False
        try:
            os.killpg(self.proc.pid, sig)      # start_new_session: pgid == pid
            return True
        except ProcessLookupError:
            return False
        except PermissionError:                # the group leader is gone, pid reused
            return False

    def _arm(self, grace_s: float, *, timeout: bool = False) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(grace_s, self._escalate, kwargs={"timeout": timeout})
            self._timer.daemon = True
            self._timer.start()

    def _escalate(self, timeout: bool = False) -> None:
        if not self.running:
            return
        if timeout:
            self._timed_out = True
        self.escalated = True
        log.warning("%s (pid %s) did not exit in time; SIGKILL to its process group",
                    self.cmd.stage or self.cmd.argv[:1], self.pid)
        self._signal_group(signal.SIGKILL)

    def terminate(self, grace_s: float | None = None) -> None:
        """SIGTERM: stop starting episodes, finish the ones in flight (exit 5)."""
        if not self.running:
            return
        if self.requested in ("int", "kill"):
            return                              # a harder stop is already under way
        self.requested = "term"
        self._signal_group(signal.SIGTERM)
        self._arm(self.term_grace_s if grace_s is None else grace_s)

    def interrupt(self, grace_s: float | None = None) -> None:
        """SIGINT: abandon requests in flight, keep what is written, exit 130."""
        if not self.running:
            return
        if self.requested == "kill":
            return
        self.requested = "int"
        self._signal_group(signal.SIGINT)
        self._arm(self.int_grace_s if grace_s is None else grace_s)

    def kill(self) -> None:
        if not self.running:
            return
        self.requested = "kill"
        self.escalated = True
        self._signal_group(signal.SIGKILL)

    # -- the end --------------------------------------------------------------------
    def wait(self) -> CliOutcome:
        assert self.proc is not None, "start() first"
        rc = self.proc.wait()
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        # nothing of the command may outlive it (02 §4: stop = the whole group)
        self._signal_group(signal.SIGKILL)
        for th in self._readers:
            th.join(timeout=30)
        return self._outcome(rc)

    def _outcome(self, rc: int) -> CliOutcome:
        elapsed = time.monotonic() - self._started
        text = self._out.decode("utf-8", "replace")
        doc, extra = parse_stdout(text)
        out = CliOutcome(returncode=rc, status="unexpected", doc=doc, requested=self.requested,
                         escalated=self.escalated, stdout_extra=extra,
                         stderr_tail=list(self._tail), elapsed_s=round(elapsed, 3))
        if rc < 0:
            out.signal = -rc
            out.status = "timeout" if self._timed_out else (
                "killed" if self.requested else "crashed")
        else:
            out.status = EXIT_STATUS.get(rc, "unexpected")
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict) and rc != 0:
            err = doc["error"]
            out.error_code = err.get("code") if isinstance(err.get("code"), str) else None
            out.message = err.get("message") if isinstance(err.get("message"), str) else None
            out.details = err.get("details") if isinstance(err.get("details"), dict) else None
        elif rc == 0 and not isinstance(doc, dict):
            # a 0 without the one JSON document breaks the output discipline (02 §5)
            out.status = "unexpected"
            out.message = "the command exited 0 but printed no JSON document"
        return out


def parse_stdout(text: str) -> tuple[Any, str | None]:
    """The one JSON document on stdout, and whatever else was there (None when clean)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, None
    try:
        doc = json.loads(lines[-1])
    except ValueError:
        try:
            doc = json.loads(text)
            return doc, None
        except ValueError:
            return None, text[-4000:]
    extra = "\n".join(lines[:-1])
    return doc, (extra[-4000:] if extra else None)


class Executor:
    """Starts commands and knows every live child, so nothing outlives a shutdown."""

    def __init__(self, program: Iterable[str] | None = None, *, term_grace_s: float = TERM_GRACE_S,
                 int_grace_s: float = INT_GRACE_S,
                 oom_score_adj: int | None = CHILD_OOM_SCORE_ADJ):
        self.program = list(program) if program is not None else default_program()
        self.term_grace_s = term_grace_s
        self.int_grace_s = int_grace_s
        self.oom_score_adj = oom_score_adj
        self._live: set[CliProcess] = set()
        self._lock = threading.Lock()

    def spawn(self, cmd: CliCommand, *, on_line: Callable[[str], None] | None = None
              ) -> CliProcess:
        proc = CliProcess(cmd, self.program, on_line=on_line, term_grace_s=self.term_grace_s,
                          int_grace_s=self.int_grace_s, oom_score_adj=self.oom_score_adj)
        proc.start()
        with self._lock:
            self._live.add(proc)
        return proc

    def finish(self, proc: CliProcess) -> CliOutcome:
        try:
            return proc.wait()
        finally:
            with self._lock:
                self._live.discard(proc)

    def run(self, cmd: CliCommand, *, on_line: Callable[[str], None] | None = None,
            on_start: Callable[[CliProcess], None] | None = None) -> CliOutcome:
        """Blocking: start, hand the process to ``on_start`` (for signals), wait."""
        try:
            proc = self.spawn(cmd, on_line=on_line)
        except OSError as err:
            log.error("cannot start %s: %s", cmd.describe(), err)
            return CliOutcome(returncode=None, status="spawn_failed",
                              message=f"cannot start the command: {err}")
        try:
            if on_start is not None:
                on_start(proc)
        finally:
            outcome = self.finish(proc)
        return outcome

    def live(self) -> list[CliProcess]:
        with self._lock:
            return [p for p in self._live if p.running]

    def kill_all(self) -> int:
        n = 0
        for proc in self.live():
            proc.kill()
            n += 1
        return n
