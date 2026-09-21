"""Exit codes and the errors that carry them (design doc 02, section 4).

Every non-zero exit of a v2 command goes through one of these classes, so the
``--json`` error envelope (``docs/contracts/cli/error.schema.json``) always has
a matching ``exit_code`` / ``error.code`` pair.
"""
from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT_UNREACHABLE = 3
EXIT_MODULE_FAILED = 4
EXIT_TERMINATED = 5
EXIT_SOURCE_CHANGED = 6
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


class UsageError(CliError):
    """Bad arguments or configuration: a bug when the Daemon is the caller."""

    exit_code = EXIT_USAGE
    code = "usage"


class InputUnreachable(CliError):
    """The data (or the service) cannot be read: missing path, rejected key, network."""

    exit_code = EXIT_INPUT_UNREACHABLE
    code = "input_unreachable"


class ModuleFailed(CliError):
    """The command as a whole could not do its job (includes unexpected exceptions)."""

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


class Interrupted(CliError):
    """SIGINT: requests in flight were abandoned, finished results are kept."""

    exit_code = EXIT_INTERRUPTED
    code = "interrupted"
