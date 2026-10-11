"""The ``curation`` command line (Curator v2, work package W3).

``curation.cli:main`` is the console-script entry point (``backend/pyproject.toml``)
and ``python -m curation.cli`` runs the same. The commands live in this package
(the atomic commands, the REST client ``task`` and the ops commands ``ls``, ``prune``,
``fetch``). See ``README.md`` next to this file.
"""
from __future__ import annotations

import os

# Keep Daft's terminal animation and query-id lines off unless asked for: they garble
# the progress lines on interactive terminals.
os.environ.setdefault("DAFT_PROGRESS_BAR", "0")
os.environ.setdefault("DAFT_SHOW_QUERY_ID", "0")

from .app import main  # noqa: E402

__all__ = ["main"]
