"""Exit codes and the errors that carry them (design doc 02, section 4; C2 1.1).

Every non-zero exit of a v2 command goes through one of these classes, so the
``--json`` error envelope (``docs/contracts/cli/error.schema.json``) always has
a matching ``exit_code`` / ``error.code`` pair:

====  ======================  ==========================================================
code  ``error.code``          meaning
====  ======================  ==========================================================
1     ``internal``            an unexpected exception: a bug, traceback on stderr
2     ``usage``               bad arguments or configuration
3     ``input_unreachable``   the input dataset cannot be read
3     ``output_unreachable``  the delivery location cannot be read or written
3     ``daemon_unreachable``  ``curation task``: no HTTP answer from the Daemon
4     ``module_failed``       the module as a whole cannot run (endpoint down, ...)
5     ``terminated``          SIGTERM: work in flight finished, nothing new started
6     ``source_changed``      the source differs from ``--source-manifest``
7     ``rejected``            ``curation task``: the Daemon answered with an error
8     ``wait_timeout``        ``curation task wait``: the task did not end in time
130   ``interrupted``         SIGINT: requests in flight abandoned
====  ======================  ==========================================================
"""
from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_INPUT_UNREACHABLE = 3
EXIT_UNREACHABLE = 3
EXIT_MODULE_FAILED = 4
EXIT_TERMINATED = 5
EXIT_SOURCE_CHANGED = 6
EXIT_REJECTED = 7
EXIT_WAIT_TIMEOUT = 8
EXIT_INTERRUPTED = 130

SCHEMA_VERSION = "1.0"


class CliError(Exception):
    """A command failure with a contract exit code; ``message`` is English for people."""

    exit_code = EXIT_MODULE_FAILED
    code = "module_failed"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or None

    def envelope(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        return {"schema_version": SCHEMA_VERSION, "exit_code": self.exit_code, "error": err}


class Internal(CliError):
    """An unexpected exception inside the command: a bug (the traceback goes to stderr)."""

    exit_code = EXIT_INTERNAL
    code = "internal"


class UsageError(CliError):
    """Bad arguments or configuration: a bug when the Daemon is the caller."""

    exit_code = EXIT_USAGE
    code = "usage"


class InputUnreachable(CliError):
    """The input data cannot be read: missing path, rejected key, network."""

    exit_code = EXIT_UNREACHABLE
    code = "input_unreachable"


class OutputUnreachable(CliError):
    """The delivery location cannot be read or written (``verify``, ``export --output``)."""

    exit_code = EXIT_UNREACHABLE
    code = "output_unreachable"


class DaemonUnreachable(CliError):
    """``curation task``: the Daemon gave no HTTP answer (connection, TLS, not JSON)."""

    exit_code = EXIT_UNREACHABLE
    code = "daemon_unreachable"


class ModuleFailed(CliError):
    """The command as a whole could not do its job (a whole module cannot run)."""

    exit_code = EXIT_MODULE_FAILED
    code = "module_failed"


class Terminated(CliError):
    """SIGTERM: the work in flight was finished and persisted, nothing new was started."""

    exit_code = EXIT_TERMINATED
    code = "terminated"


class SourceChanged(CliError):
    """A source object differs from the ``--source-manifest`` fixed at task start (D27)."""

    exit_code = EXIT_SOURCE_CHANGED
    code = "source_changed"


class Rejected(CliError):
    """``curation task``: the Daemon answered with an error; its body is in ``details``."""

    exit_code = EXIT_REJECTED
    code = "rejected"


class WaitTimeout(CliError):
    """``curation task wait``: the timeout passed; the last task state is in ``details``."""

    exit_code = EXIT_WAIT_TIMEOUT
    code = "wait_timeout"


class Interrupted(CliError):
    """SIGINT: requests in flight were abandoned, finished results are kept."""

    exit_code = EXIT_INTERRUPTED
    code = "interrupted"


def unreachable(role: str, message: str, details: dict[str, Any] | None = None) -> CliError:
    """``input_unreachable`` or ``output_unreachable`` for a storage role."""
    cls = OutputUnreachable if role == "output" else InputUnreachable
    return cls(message, details)
