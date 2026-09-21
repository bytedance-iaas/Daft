"""The ``curation`` command line (Curator v2, work package W3).

``curation.cli:main`` is the console-script entry point (``backend/pyproject.toml``)
and ``python -m curation.cli`` runs the same. v2 commands live in this package
(``preflight``, ``snapshot``, ``verify``, ``task``); the v1 command line lives on
unchanged in :mod:`curation.cli.legacy` and gets its subcommands handed over.
See ``README.md`` next to this file.
"""
from __future__ import annotations

import os

# Keep Daft's terminal animation and query-id lines off unless asked for (same
# default as v1): they garble the progress lines on interactive terminals.
os.environ.setdefault("DAFT_PROGRESS_BAR", "0")
os.environ.setdefault("DAFT_SHOW_QUERY_ID", "0")

from .app import main  # noqa: E402

__all__ = ["main"]
