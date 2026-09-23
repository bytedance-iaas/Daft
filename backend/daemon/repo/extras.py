"""Helpers around the repository queries that joined C5 in 1.3 (overview, dataset lookup).

The queries themselves are now part of :class:`daemon.repo.protocol.Repository`;
``RepositoryExtras`` remains as an alias for older imports.

C5 (``protocol.py``) is frozen, so the few reads the overview page and the task
configuration need are declared here until a contract revision takes them in.
They are read-only aggregates an RDS implementation can answer with one query
each. ``SqliteRepository`` implements them, and ``tests/daemon/test_repo_extras.py``
is their conformance suite, run on every implementation like the C5 one.

What the overview's figures mean (design doc 07, section 4.3) is fixed here, so
every implementation counts the same things:

* **adjudication backlog** - tasks whose ``summary.pending_adjudication`` (or,
  until W5 writes that key, ``summary.review``) is a positive integer, and the
  sum of those numbers; soft-deleted tasks do not count;
* **delivery pending** - finished tasks with a result (``result_rev >= 1``)
  whose delivered dataset is stale or was never exported (``export_fingerprint``
  is null); soft-deleted tasks do not count;
* **finished results** - tasks that ended ``succeeded`` or
  ``completed_with_errors`` at or after ``since``, with the sums of their
  ``summary.total`` and ``summary.passed``; stopped and failed tasks are not
  "finished" here, soft-deleted ones do not count;
* **unfinished subtasks** - retries, resumes, adjudication runs and re-exports
  not in a terminal state, with their parent task (newest first); their parents
  are finished, so the task states alone never show this work;
* **token timeline** - the actual ledger only (never the attributed one, which
  splits the same requests), ``prompt_tokens + completion_tokens`` (reasoning
  tokens are part of completion, cached ones part of prompt, as the model
  reports them), summed per owner into 15-minute UTC slots by the time
  ``add_usage`` was called; slots cover every UTC offset in use, so days can be
  cut in any time zone. Tokens stay counted whatever happens to their task later.
"""
from __future__ import annotations

from .protocol import DEFAULT_OWNER, Dataset, FinishedResults, Repository, Subtask, Task  # noqa: F401

#: Width of one token-timeline slot.
TOKEN_SLOT_MS = 15 * 60 * 1000

#: Values of C4 ``DatasetFormat`` (mcap and lance since C4 1.11, D44).
DATASET_FORMATS = ("lerobot_v2", "lerobot_v3", "mcap", "lance", "unsupported")


def dataset_format(preflight: dict | None) -> str:
    """C4 ``DatasetFormat`` of a preflight result (C2 ``preflight.schema.json``).

    A supported LeRobot v2 / v3 dataset is ``lerobot_v2`` / ``lerobot_v3``, a supported
    mcap / lance one (D44) ``mcap`` / ``lance``; anything the preflight could not use
    (other formats, invalid metadata, a format the site switched off) is ``unsupported``.
    """
    fmt = preflight.get("format") if isinstance(preflight, dict) else None
    if not isinstance(fmt, dict) or fmt.get("supported") is not True:
        return "unsupported"
    if fmt.get("kind") == "lerobot" and fmt.get("version") in ("v2", "v3"):
        return f"lerobot_{fmt['version']}"
    if fmt.get("kind") in ("mcap", "lance"):
        return str(fmt["kind"])
    return "unsupported"


def token_slot(at: int) -> int:
    """Start of the timeline slot that holds epoch millisecond ``at``."""
    return int(at) - int(at) % TOKEN_SLOT_MS


#: C5 1.3 took these queries into the protocol.
RepositoryExtras = Repository
