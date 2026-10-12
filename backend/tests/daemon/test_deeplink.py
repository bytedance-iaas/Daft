"""Deep links on ``{base}/``: the server recognises one and redirects; the page parses it."""
from __future__ import annotations

from daemon import deeplink as dl


class QP(dict):
    """A stand-in for Starlette's QueryParams (``getlist``)."""

    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v is not None else [])


def test_entry_detection_covers_every_key():
    for key in ("dataset", "dataset_url", "url", "region", "endpoint", "tos_endpoint", "source"):
        assert dl.is_entry(QP({key: ""})), key
        assert dl.is_entry({key: "x"}), key
    assert not dl.is_entry(QP({"utm_source": "x"}))
    assert not dl.is_entry({})
