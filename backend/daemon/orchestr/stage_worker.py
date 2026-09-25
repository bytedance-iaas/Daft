"""A persistent multiprocessing child for one funnel layer."""
from __future__ import annotations

import io
import json
import multiprocessing as mp
import os
import signal
import sys
import threading
from dataclasses import dataclass

from daemon.exec import CliCommand, CliOutcome
from daemon.exec.runner import EXIT_STATUS


class _EventStream:
    def __init__(self, conn):
        self.conn = conn
        self.pending = ""
        self.lock = threading.RLock()

    def write(self, data):
        with self.lock:
            self.pending += str(data)
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line:
                    self.conn.send(("event", line))
        return len(data)

    def flush(self):
        pass


class _Connection:
    """Serialize C3 and completion messages from the child's request threads."""

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.RLock()

    def send(self, message):
        with self.lock:
            self.conn.send(message)


class EpisodeStream:
    def __init__(self, conn, output):
        self.conn, self.output = conn, output

    def receive(self, timeout=0):
        if not self.conn.poll(timeout):
            return []
        message = self.conn.recv()
        return None if message is None else message["episodes"]

    def completed(self, episode, survivor):
        self.output.send(("episode", {"episode": episode, "survivor": survivor}))


def _serve(conn, argv: list[str], env: dict[str, str], cwd: str | None) -> None:
    """Spawn entry: CLI semantics with a connection in place of stdio."""
    os.setsid()
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    from daemon.exec.runner import THREAD_ENV

    os.environ.clear()
    # before curation (numpy, OpenCV) is imported: their thread pools read it at load time
    os.environ.update({**THREAD_ENV, **env})
    if cwd:
        os.chdir(cwd)
    from daemon.exec.runner import CHILD_OOM_SCORE_ADJ, set_oom_score_adj

    set_oom_score_adj(os.getpid(), CHILD_OOM_SCORE_ADJ)
    from curation.cli.app import build_parser
    from curation.cli.framework import run_command

    args = build_parser().parse_args(argv)
    output = _Connection(conn)
    events = _EventStream(output)
    cache: dict = {}
    conn.send(("ready", None))
    try:
        while True:
            try:
                request = conn.recv()
            except EOFError:
                break
            if request is None:
                break
            batch = type(args)(**vars(args))
            batch.episodes = request["episodes"]
            batch.survivors_out = request["survivors_out"]
            batch._worker_cache = cache
            streaming = request.get("stream", False)
            if streaming:
                batch._episode_stream = EpisodeStream(conn, output)
            capture = io.StringIO()
            original_out, original_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = capture, events
            try:
                rc = run_command(batch.func, batch)
            finally:
                sys.stdout, sys.stderr = original_out, original_err
            try:
                doc = json.loads(capture.getvalue())
            except ValueError:
                rc, doc = 1, {"error": {"code": "worker_protocol",
                                        "message": "worker returned invalid JSON"}}
            if streaming:
                # Flush latency and uninstall transport before acknowledging EOF.
                prepared = cache.pop("vlm", None)
                if prepared is not None:
                    prepared["session"].__exit__(None, None, None)
            output.send(("result", {"returncode": rc, "doc": doc}))
            if streaming:
                break
    finally:
        prepared = cache.get("vlm")
        if prepared is not None:
            prepared["session"].__exit__(None, None, None)
        conn.close()


@dataclass
class StageWorker:
    cmd: CliCommand
    on_line: object
    term_grace_s: float = 90.0
    int_grace_s: float = 10.0

    def __post_init__(self):
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.process = ctx.Process(target=_serve,
                                   args=(child, [*self.cmd.argv, "--json"], self.cmd.env,
                                         self.cmd.cwd),
                                   name=f"curation-{self.cmd.stage}")
        self.process.start()
        child.close()
        try:
            if not self.conn.poll(30) or self.conn.recv()[0] != "ready":
                raise RuntimeError(f"{self.cmd.stage} worker failed to start")
        except (EOFError, RuntimeError):
            if self.process.is_alive():
                self.process.kill()
            self.process.join(timeout=5)
            self.conn.close()
            raise RuntimeError(f"{self.cmd.stage} worker failed to start") from None
        self.requested = None
        self._timer = None

    @property
    def pid(self):
        return self.process.pid

    @property
    def running(self):
        return self.process.is_alive()

    def exchange(self, episodes: str, survivors_out: str) -> CliOutcome:
        self.conn.send({"episodes": episodes, "survivors_out": survivors_out})
        while True:
            try:
                kind, payload = self.conn.recv()
            except EOFError as exc:
                raise RuntimeError(f"{self.cmd.stage} worker exited ({self.process.exitcode})") from exc
            if kind == "event":
                self.on_line(payload)
                continue
            return self.outcome(payload)

    def start_stream(self, episodes: str) -> None:
        self.conn.send({"stream": True, "episodes": episodes, "survivors_out": None})

    def submit(self, episodes: list[int]) -> None:
        self.conn.send({"episodes": episodes})

    def finish(self) -> None:
        self.conn.send(None)

    def poll(self):
        if not self.conn.poll():
            if not self.running:
                raise EOFError(f"{self.cmd.stage} worker exited ({self.process.exitcode})")
            return None
        return self.conn.recv()

    def outcome(self, payload) -> CliOutcome:
        rc, doc = payload["returncode"], payload["doc"]
        outcome = CliOutcome(returncode=rc, status=EXIT_STATUS.get(rc, "unexpected"),
                             doc=doc, requested=self.requested)
        if rc and isinstance(doc, dict):
            error = doc.get("error") or {}
            outcome.error_code = error.get("code")
            outcome.message = error.get("message")
            outcome.details = error.get("details")
        return outcome

    def _signal(self, sig: int, grace: float):
        if not self.running:
            return
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            return
        self._timer = threading.Timer(grace, self.kill)
        self._timer.daemon = True
        self._timer.start()

    def terminate(self):
        self.requested = "term"
        self._signal(signal.SIGTERM, self.term_grace_s)

    def interrupt(self):
        self.requested = "int"
        self._signal(signal.SIGINT, self.int_grace_s)

    def kill(self):
        if self.running:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self.running and self.requested is None:
            try:
                self.conn.send(None)
            except (BrokenPipeError, EOFError, OSError):
                pass
        self.process.join(timeout=5)
        if self.running:
            self.kill()
            self.process.join(timeout=5)
        if self._timer is not None:
            self._timer.cancel()
        self.conn.close()
