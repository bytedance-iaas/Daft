"""What every v2 command shares: output discipline, progress protocol, signals.

Design doc 02, sections 2, 4 and 5:

* ``--json``: stdout carries exactly one JSON document - the command's result
  on exit 0, the error envelope (``cli/error.schema.json``) otherwise - and
  stderr carries C3 events, one JSON object per line
  (``docs/contracts/progress.schema.json``). Without ``--json`` both streams
  carry text for people and logs still go to stderr.
* Library code (v1 modules print progress lines in Chinese) must not corrupt
  those channels, so while a command runs ``sys.stdout`` / ``sys.stderr`` are
  replaced by line sinks that turn stray lines into ``log`` events.
* SIGTERM asks the command to stop starting new work (``Context.check_stop``
  raises :class:`Terminated`, exit 5); SIGINT interrupts at once (exit 130).
* Every exit code is one of the contract's (``errors.py``); an unexpected
  exception becomes ``internal`` (1) with its traceback logged at ``error`` level.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import (
    CliError,
    Internal,
    Interrupted,
    Terminated,
    UsageError,
)

LEVELS = ("debug", "info", "warn", "error")
_RANK = {name: i for i, name in enumerate(LEVELS)}
#: Level used to filter the event kinds that carry none of their own.
_KIND_LEVEL = {"progress": "info", "usage": "info", "throttle": "warn"}
#: Human mode prints a progress line at most this often per stage.
_HUMAN_PROGRESS_EVERY_S = 1.0
#: How soon a sleeping command notices SIGTERM.
_SLEEP_SLICE_S = 0.1
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------- events (C3)


class Emitter:
    """Writes C3 events to the real stderr: JSON Lines with ``--json``, text otherwise.

    Writes never raise: a broken log stream must not kill a run (v1's
    ``_TolerantStream`` lesson - the data matters more than the log).
    """

    def __init__(self, stream, *, json_mode: bool, level: str = "info"):
        self._stream = stream
        self.json_mode = json_mode
        self.level = level
        self._lock = threading.RLock()
        self._last_progress: dict[str, float] = {}
        self._stage_start: dict[str, float] = {}

    def enabled(self, level: str) -> bool:
        return _RANK.get(level, 1) >= _RANK.get(self.level, 1)

    def emit(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        level = event.get("level") or _KIND_LEVEL.get(kind, "info")
        if not self.enabled(level):
            return
        event = {"ts": now_ms(), **event}
        if self.json_mode:
            text = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        else:
            text = self._render(event)
            if text is None:
                return
        self._write(text + "\n")

    def _write(self, text: str) -> None:
        with self._lock:
            try:
                self._stream.write(text)
                self._stream.flush()
            except (OSError, ValueError):
                pass

    def _render(self, event: dict[str, Any]) -> str | None:
        kind = event["kind"]
        if kind == "log":
            prefix = {"warn": "warning: ", "error": "error: ", "debug": "debug: "}.get(
                event.get("level"), "")
            return f"{prefix}{event['msg']}"
        if kind == "progress":
            # People only see progress of stages slow enough to need it, once a second.
            stage, done, total = event["stage"], event["done"], event["total"]
            t = time.monotonic()
            first = self._stage_start.setdefault(stage, t)
            last = self._last_progress.get(stage)
            if t - first < _HUMAN_PROGRESS_EVERY_S:
                return None
            if done < total and last is not None and t - last < _HUMAN_PROGRESS_EVERY_S:
                return None
            self._last_progress[stage] = t
            eta = event.get("eta_s")
            tail = f" (about {int(eta)}s left)" if eta else ""
            return f"[{stage}] {done}/{total}{tail}"
        if kind == "throttle":
            return (f"warning: {event['backend']}: concurrency limited to {event['limit']} "
                    f"({event['reason']})")
        return None                                # usage lines are for machines

    # convenience wrappers -----------------------------------------------------
    def log(self, level: str, msg: str, **fields: Any) -> None:
        self.emit({"kind": "log", "level": level, "msg": msg, **fields})

    def progress(self, stage: str, done: int, total: int, *, eta_s: float | None = None,
                 episode_index: int | None = None) -> None:
        event: dict[str, Any] = {"level": "info", "kind": "progress", "stage": stage,
                                 "done": int(done), "total": int(total)}
        if eta_s is not None:
            event["eta_s"] = max(0.0, float(eta_s))
        if episode_index is not None:
            event["episode_index"] = int(episode_index)
        self.emit(event)

    def usage(self, *, model: str, module: str, call_kind: str, requests: int,
              prompt_tokens: int, completion_tokens: int, reasoning_tokens: int = 0,
              cached_tokens: int = 0, ledger: str = "actual",
              requests_unknown_usage: int | None = None) -> None:
        event: dict[str, Any] = {"kind": "usage", "ledger": ledger, "model": model,
                                 "module": module, "call_kind": call_kind,
                                 "requests": requests, "prompt_tokens": prompt_tokens,
                                 "completion_tokens": completion_tokens,
                                 "reasoning_tokens": reasoning_tokens,
                                 "cached_tokens": cached_tokens}
        if requests_unknown_usage is not None:
            event["requests_unknown_usage"] = requests_unknown_usage
        self.emit(event)

    def throttle(self, *, backend: str, limit: int, reason: str,
                 gates: dict[str, int] | None = None) -> None:
        event: dict[str, Any] = {"kind": "throttle", "backend": backend, "limit": int(limit),
                                 "reason": reason}
        if gates:
            event["gates"] = gates
        self.emit(event)


class _LineSink(io.TextIOBase):
    """A text stream that hands every complete, non-blank line to a callback."""

    def __init__(self, on_line: Callable[[str], None]):
        super().__init__()
        self._on_line = on_line
        self._buf = ""

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:                 # print() and friends look at it
        return "utf-8"

    def write(self, s) -> int:
        s = s if isinstance(s, str) else str(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def _emit(self, line: str) -> None:
        line = line.rstrip("\r")
        if line.strip():
            self._on_line(line)

    def flush(self) -> None:
        pass

    def drain(self) -> None:
        if self._buf:
            rest, self._buf = self._buf, ""
            self._emit(rest)


# ---------------------------------------------------------------- context


class Context:
    """Per-invocation state handed to every command function."""

    def __init__(self, args: argparse.Namespace, emitter: Emitter):
        self.args = args
        self.emitter = emitter
        self.json_mode = emitter.json_mode
        self.stop_requested = False
        self._stop_logged = False
        self._config: dict | None = None

    # events
    def log(self, level: str, msg: str, **fields: Any) -> None:
        self.emitter.log(level, msg, **fields)

    def progress(self, stage: str, done: int, total: int, **kw: Any) -> None:
        self.emitter.progress(stage, done, total, **kw)

    # signals
    def request_stop(self) -> None:
        """What the SIGTERM handler does: set a flag, nothing else.

        Signal handlers run between two bytecodes of the main thread, possibly
        while it holds a lock (the emitter's, an Event's); so no locking, no I/O
        here - the flag is noticed by :meth:`check_stop`.
        """
        self.stop_requested = True

    def check_stop(self, what: str = "") -> None:
        """Call between units of work; raises :class:`Terminated` after SIGTERM."""
        if self.stop_requested:
            tail = f" ({what})" if what else ""
            if not self._stop_logged:
                self._stop_logged = True
                self.log("warn", f"SIGTERM received: stopping{tail}; nothing new is started")
            raise Terminated(f"stopped on SIGTERM{tail}")

    def sleep(self, seconds: float) -> None:
        """Sleep in short slices so SIGTERM is noticed within ``_SLEEP_SLICE_S``."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.check_stop()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(_SLEEP_SLICE_S, left))

    # global parameters (design doc 02, section 2)
    def _region(self, specific: str) -> str | None:
        value = getattr(self.args, specific, None) or getattr(self.args, "region", None)
        if not value:
            return None
        return str(value).strip() or None

    @property
    def input_region(self) -> str | None:
        return self._region("input_region")

    @property
    def output_region(self) -> str | None:
        return self._region("output_region")

    def config(self) -> dict:
        """The pipeline configuration: factory defaults + ``--config`` + ``--set``.

        ``--config`` falls back to ``$CURATION_CONFIG`` (v1's rule); nothing is
        read from the home directory.
        """
        if self._config is None:
            import yaml

            from ..pipeline.config import (
                ConfigError,
                apply_overrides,
                load_config,
                validate_config,
            )
            try:
                cfg = load_config(getattr(self.args, "config", None))
                sets = getattr(self.args, "set", None) or []
                if sets:
                    cfg = apply_overrides(cfg, sets)
                    validate_config(cfg, "--set")
            except (ConfigError, OSError, yaml.YAMLError) as e:
                raise UsageError(f"configuration: {e}") from None
            self._config = cfg
        return self._config


@dataclass
class Result:
    """What a command returns: the ``--json`` document and, optionally, text for people."""

    payload: Any
    human: str | None = None


# ---------------------------------------------------------------- execution


def _install_signals(ctx: Context) -> Callable[[], None]:
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {}

    def on_term(signum, frame):                  # noqa: ARG001 - signal API
        ctx.request_stop()

    previous[signal.SIGTERM] = signal.signal(signal.SIGTERM, on_term)
    # Explicitly: a parent that ignored SIGINT would otherwise pass SIG_IGN down.
    previous[signal.SIGINT] = signal.signal(signal.SIGINT, signal.default_int_handler)

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


def _write(stream, text: str) -> None:
    try:
        stream.write(text)
        stream.flush()
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii"))
        stream.flush()
    except (OSError, ValueError):
        pass


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def run_command(func: Callable[[Context, argparse.Namespace], Result],
                args: argparse.Namespace) -> int:
    """Run one command function under the output and signal discipline."""
    json_mode = bool(getattr(args, "json", False))
    real_out, real_err = sys.stdout, sys.stderr
    emitter = Emitter(real_err, json_mode=json_mode,
                      level=getattr(args, "log_level", None) or "info")
    ctx = Context(args, emitter)
    out_sink = _LineSink(lambda line: emitter.log("info", line) if json_mode
                         else _write(real_err, line + "\n"))
    err_sink = _LineSink(lambda line: emitter.log("warn", line))
    result: Result | None = None
    error: CliError | None = None
    restore_signals = _install_signals(ctx)
    try:
        sys.stdout = out_sink
        if json_mode:
            sys.stderr = err_sink
        try:
            _validate_regions(args)
            if getattr(args, "config", None) or getattr(args, "set", None):
                ctx.config()                 # an explicit --config / --set must be valid
            result = func(ctx, args)
        except CliError as e:
            error = e
        except KeyboardInterrupt:
            error = Interrupted("interrupted by SIGINT; results already written are kept")
        except SystemExit as e:
            error = Internal(f"the command exited early (status {e.code})")
        except Exception as e:  # noqa: BLE001 - every failure must end in an envelope
            emitter.log("error", "".join(traceback.format_exception(e)).rstrip())
            error = Internal(f"{type(e).__name__}: {e}", {"exception": type(e).__name__})
        finally:
            out_sink.drain()
            err_sink.drain()
            sys.stdout, sys.stderr = real_out, real_err
    finally:
        restore_signals()
    return _finish(ctx, result, error, real_out, real_err)


def _finish(ctx: Context, result: Result | None, error: CliError | None,
            out, err) -> int:
    if error is not None:
        if ctx.json_mode:
            _write(out, dump_json(error.envelope()) + "\n")
        else:
            _write(err, f"error: {error.message}\n")
        return error.exit_code
    assert result is not None
    if ctx.json_mode:
        _write(out, dump_json(result.payload) + "\n")
    else:
        text = result.human if result.human is not None else json.dumps(
            result.payload, ensure_ascii=False, indent=1)
        _write(out, text.rstrip("\n") + "\n")
    return 0


def _validate_regions(args: argparse.Namespace) -> None:
    for name in ("region", "input_region", "output_region"):
        value = getattr(args, name, None)
        if value and not _REGION_RE.match(str(value)):
            flag = "--" + name.replace("_", "-")
            raise UsageError(f"{flag} {value!r} is not a TOS region such as cn-beijing")


def emit_usage_error(message: str, *, json_mode: bool) -> int:
    """Report an argument error that happened before a command could run."""
    error = UsageError(message)
    if json_mode:
        _write(sys.stdout, dump_json(error.envelope()) + "\n")
    else:
        _write(sys.stderr, f"error: {message}\n")
    return error.exit_code
