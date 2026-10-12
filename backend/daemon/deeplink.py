"""Deep links from the ReRun viewer (design doc 07, section 2.1; 09 §2.4).

The viewer's "质检" button opens ``{base}/?dataset=tos://...&region=...``. The new-task page lives at
``{base}/tasks/new``: the old entry answers with a 302 there, query string untouched, and the page parses
the link and prefills the form (``frontend/src/lib/deeplink.ts``). The server only has to recognise one:
a key that is present must get a visible answer on the page even when its value is empty or invalid, so
the redirect happens whenever any of the keys is present, whatever the values.
"""
from __future__ import annotations

from typing import Mapping

DEEPLINK_KEYS = ("dataset", "dataset_url", "url")
REGION_KEY = "region"
ENDPOINT_KEYS = ("endpoint", "tos_endpoint")
SOURCE_KEY = "source"
#: Any of these in the query of ``{base}/`` makes it a deep link.
ENTRY_KEYS = DEEPLINK_KEYS + (REGION_KEY,) + ENDPOINT_KEYS + (SOURCE_KEY,)


def _values(qp, key: str) -> list:
    """Starlette ``QueryParams`` (``getlist``) or a plain dict (tests)."""
    if hasattr(qp, "getlist"):
        return list(qp.getlist(key))
    if key not in qp:
        return []
    value = qp[key]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def is_entry(qp: Mapping) -> bool:
    """Is a request for ``{base}/`` a deep link (any of the keys present)?"""
    return any(_values(qp, key) for key in ENTRY_KEYS)
