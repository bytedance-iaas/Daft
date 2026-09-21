"""The ``curation`` entry point: v2 commands, and v1 commands handed to the legacy CLI.

v2 commands (design doc 02): ``preflight``, ``snapshot``, ``verify`` and the
REST client ``task``. The v1 subcommands (``run``, ``rejudge``, ``ls``, ...)
keep working unchanged through :mod:`curation.cli.legacy` until their
functions move into atomic commands (doc 10, section 2.3).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from . import (adjudicate, aggregate, autolabel, check, plan, preflight, snapshot,
               task_client, verify)
from .errors import EXIT_INTERRUPTED, UsageError
from .framework import LEVELS, emit_usage_error, run_command

#: Handed to the v1 command line as they are (``reprofile`` is v1's hidden command).
LEGACY_COMMANDS = ("run", "rejudge", "review-page", "prune", "ls", "fetch", "backends",
                   "public", "reprofile")

_SECRET_OPTION = re.compile(r"key|secret|password|passwd|token|credential", re.I)


class _Parser(argparse.ArgumentParser):
    """Argument errors become :class:`UsageError` so they end in the error envelope."""

    def error(self, message):  # noqa: D401 - argparse API
        raise UsageError(f"{self.prog}: {message}")


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("curation")
    except Exception:  # noqa: BLE001 - not installed (development tree)
        return "unknown"


def _global_options(atomic: bool) -> argparse.ArgumentParser:
    """Design doc 02, section 2. The client commands take only --json / --log-level."""
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("global options")
    g.add_argument("--json", action="store_true",
                   help="one JSON document on stdout; logs go to stderr as JSON Lines")
    g.add_argument("--log-level", choices=LEVELS, default="info",
                   help="least severe log level to print (default info)")
    if atomic:
        g.add_argument("--config", metavar="PATH",
                       help="pipeline YAML over the factory defaults (default: $CURATION_CONFIG)")
        g.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="override one configuration value; repeatable")
        g.add_argument("--region", metavar="REGION", help="TOS region of input and output")
        g.add_argument("--input-region", metavar="REGION", help="TOS region of the input")
        g.add_argument("--output-region", metavar="REGION", help="TOS region of the output")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="curation",
        description="Curator: quality checks for robot datasets. Each command does one thing; "
                    "--json gives machines one JSON document on stdout.",
        epilog="v1 commands, unchanged (help in Chinese: curation <command> --help):\n"
               f"  {', '.join(c for c in LEGACY_COMMANDS if c != 'reprofile')}\n\n"
               "Credentials come from the environment only: CURATION_INPUT_TOS_ACCESS_KEY / "
               "_SECRET_KEY,\nCURATION_OUTPUT_TOS_ACCESS_KEY / _SECRET_KEY (TOS_ACCESS_KEY / "
               "TOS_SECRET_KEY\nwhen unset), CURATOR_USER / CURATOR_PASSWORD for curation task.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"curation {_version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    atomic = [_global_options(atomic=True)]
    preflight.add_parser(sub, atomic)
    plan.add_parser(sub, atomic)
    snapshot.add_parser(sub, atomic)
    autolabel.add_parser(sub, atomic)
    check.add_parser(sub, atomic)
    aggregate.add_parser(sub, atomic)
    adjudicate.add_parser(sub, atomic)
    verify.add_parser(sub, atomic)
    task_client.add_parser(sub, _global_options(atomic=False))
    return parser


def scrub(tokens: list[str]) -> str:
    """Unknown arguments for an error message, with anything secret-looking masked."""
    out, hide_next = [], False
    for tok in tokens:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if tok.startswith("-") and _SECRET_OPTION.search(tok):
            if "=" in tok:
                out.append(tok.split("=", 1)[0] + "=***")
            else:
                out.append(tok)
                hide_next = True
            continue
        out.append(tok)
    return " ".join(out)


def main(argv: list[str] | None = None) -> int:
    process_entry = argv is None
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in LEGACY_COMMANDS:
        from . import legacy

        return legacy.main(argv)
    json_mode = "--json" in argv
    try:
        args, extras = build_parser().parse_known_args(argv)
        if extras:
            raise UsageError(f"curation: unrecognized arguments: {scrub(extras)}")
    except UsageError as e:
        return emit_usage_error(e.message, json_mode=json_mode)
    except SystemExit as e:                        # --help / --version
        return e.code if isinstance(e.code, int) else 0
    rc = run_command(args.func, args)
    if process_entry and rc == EXIT_INTERRUPTED:
        # SIGINT means "stop now" (doc 02, section 4): model calls still in flight run on
        # pool threads the interpreter would join at exit, for up to a few timeouts. The
        # envelope is written and every result is already on disk, so leave right away.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        os._exit(rc)
    return rc
