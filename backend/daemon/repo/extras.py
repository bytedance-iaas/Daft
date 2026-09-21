"""Repository queries the Daemon needs beyond C5 1.2 (proposed for C5 1.3).

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
* **token timeline** - the actual ledger only (never the attributed one, which
  splits the same requests), ``prompt_tokens + completion_tokens`` (reasoning
  tokens are part of completion, cached ones part of prompt, as the model
  reports them), summed per owner into 15-minute UTC slots by the time
  ``add_usage`` was called; slots cover every UTC offset in use, so days can be
  cut in any time zone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .protocol import DEFAULT_OWNER, Dataset

#: Width of one token-timeline slot.
TOKEN_SLOT_MS = 15 * 60 * 1000

#: Values of C4 ``DatasetFormat``.
DATASET_FORMATS = ("lerobot_v2", "lerobot_v3", "unsupported")


def dataset_format(preflight: dict | None) -> str:
    """C4 ``DatasetFormat`` of a preflight result (C2 ``preflight.schema.json``).

    A supported LeRobot v2 / v3 dataset is ``lerobot_v2`` / ``lerobot_v3``; anything
    the preflight could not use (other formats, invalid metadata) is ``unsupported``.
    """
    fmt = preflight.get("format") if isinstance(preflight, dict) else None
    if isinstance(fmt, dict) and fmt.get("supported") is True and fmt.get("kind") == "lerobot" \
            and fmt.get("version") in ("v2", "v3"):
        return f"lerobot_{fmt['version']}"
    return "unsupported"


def token_slot(at: int) -> int:
    """Start of the timeline slot that holds epoch millisecond ``at``."""
    return int(at) - int(at) % TOKEN_SLOT_MS


@dataclass(frozen=True)
class FinishedResults:
    tasks: int          # tasks that finished succeeded / completed_with_errors
    episodes: int       # sum of their summary.total
    passed: int         # sum of their summary.passed


class RepositoryExtras(Protocol):
    def find_dataset(self, *, source: str, uri: str, region: str | None,
                     owner: str = DEFAULT_OWNER) -> Dataset | None:
        """The registration of this address - the key ``register_dataset`` gets or creates
        by, with an empty region and no region being the same - or None."""

    def adjudication_backlog(self, *, owner: str = DEFAULT_OWNER) -> tuple[int, int]:
        """(tasks, episodes) waiting for human judgement (see the module docstring)."""

    def delivery_pending_count(self, *, owner: str = DEFAULT_OWNER) -> int:
        """Finished tasks whose delivery is stale or was never exported."""

    def finished_results(self, *, since: int, owner: str = DEFAULT_OWNER) -> FinishedResults:
        """What finished at or after ``since`` (epoch ms)."""

    def token_timeline(self, *, since: int, until: int,
                       owner: str = DEFAULT_OWNER) -> list[tuple[int, int]]:
        """``(slot start, tokens)`` for the slots in ``[since, until)`` that have tokens,
        oldest first; ``add_usage`` feeds it."""
