"""The ``curation`` command line.

``curation.cli:main`` is the console-script entry point (``backend/pyproject.toml``).
The v1 command line lives on unchanged in :mod:`curation.cli.legacy`.
"""
from __future__ import annotations

import os

# Keep Daft's terminal animation and query-id lines off unless asked for (same
# default as v1): they garble the progress lines on interactive terminals.
os.environ.setdefault("DAFT_PROGRESS_BAR", "0")
os.environ.setdefault("DAFT_SHOW_QUERY_ID", "0")

from .legacy import main  # noqa: E402

__all__ = ["main"]
