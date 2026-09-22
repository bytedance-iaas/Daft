"""W5a - the CLI executor (design doc 00 §2.1, 02 §4 and §5; F2.3).

* :mod:`.runner` - one ``curation`` command in its own process group: environment,
  ``oom_score_adj``, the one JSON document on stdout, stderr lines as they come,
  SIGTERM / SIGINT with SIGKILL after 90 s / 10 s, exit codes mapped per 02 §4;
* :mod:`.c3` - the progress protocol lines (C3), tolerant of anything else;
* :mod:`.usage` - usage increments batched into the two token ledgers.

The orchestration (:mod:`daemon.orchestr`) decides what an outcome means for a task.
"""
from .c3 import log_line, parse
from .runner import (
    CliCommand,
    CliOutcome,
    CliProcess,
    Executor,
    default_program,
    set_oom_score_adj,
)
from .usage import UsageAccumulator

__all__ = ["CliCommand", "CliOutcome", "CliProcess", "Executor", "UsageAccumulator",
           "default_program", "log_line", "parse", "set_oom_score_adj"]
