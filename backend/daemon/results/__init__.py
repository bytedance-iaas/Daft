"""W5b - reading a task's results out of its run directory (design docs 03 §6-§7, 06).

The CLI writes a task's results into its run directory (``CURATOR_WORK_DIR/<task_id>/``)
as immutable result revisions, ``revisions/r<NNNN>/`` with ``commit.json`` last; the
Daemon points ``task.result_rev`` at the one in force (D25). This package serves them:

* :mod:`.store` - :class:`ResultStore` (one per Daemon, :func:`store_of`): which
  revision a request reads (``?rev=N``), the caches, and two hooks for the
  orchestration: ``backfill`` (bring a cleaned run directory back from the delivery
  location) and ``open_input`` (read the input dataset's metadata);
* :mod:`.revision` - one committed revision: report, lists, label audit, records;
* :mod:`.tables` - detail tables from Parquet, the sort whitelist, revision-bound cursors;
* :mod:`.episode` / :mod:`.videos` - one episode across every module, its evidence and
  where each camera can be played from;
* :mod:`.perf` - the performance profile, all / main run / one subtask;
* :mod:`.adjudication` - the adjudication queue: questions, cards, counts, decisions,
  the CSV copy in ``human-decisions/`` and ``summary.pending_adjudication``.

For the orchestration (W5a): after switching ``result_rev`` call
:func:`refresh_summary` (the summary and its ``pending_adjudication`` follow the new
revision); after ``curation adjudicate-apply`` rewrote ``human-decisions/`` with the
applied decisions only, :func:`write_copies` puts every recorded decision back.
"""
from .adjudication import Queue, refresh_summary, submit, write_copies
from .revision import Revision
from .store import ResultStore, store_of

__all__ = ["Queue", "ResultStore", "Revision", "refresh_summary", "store_of", "submit",
           "write_copies"]
