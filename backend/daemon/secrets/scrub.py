"""Keep secret values out of any text that is stored, returned or logged (design doc 08, section 3).

Providers echo things back: a TOS error may quote the access key id, an OpenAI-compatible
server may answer ``invalid api key sk-...``. Every piece of provider text goes through a
:class:`Scrubber` built from the secrets that were used for the call before it is kept in
``last_verify_error``, put into an error message, a precheck reason or a log line.

Two layers: the known values themselves (and their URL-encoded forms) become ``***``, then
the generic patterns of :func:`daemon.logconfig.redact` (``Authorization: Bearer ...``,
``secret_key=...``) catch what nobody told us about.
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import quote, quote_plus

from ..logconfig import redact

MASK = "***"

#: Values shorter than this are not searched for: replacing two-letter strings would mangle
#: the text and such a "secret" protects nothing anyway.
MIN_SECRET_LEN = 4

#: Provider text kept for people is one line of at most this many characters.
MAX_DETAIL = 200


class Scrubber:
    """``scrub(text)`` -> the text with every known secret replaced by ``***``."""

    def __init__(self, secrets: Iterable[str | None] = ()):
        values: set[str] = set()
        for raw in secrets:
            if not isinstance(raw, str):
                continue
            for s in {raw, raw.strip()}:
                if len(s) < MIN_SECRET_LEN:
                    continue
                values.update({s, quote(s, safe=""), quote_plus(s)})
        # longest first, so a value that contains another is masked as a whole
        self._values = tuple(sorted(values, key=len, reverse=True))

    def extend(self, secrets: Iterable[str | None]) -> "Scrubber":
        return Scrubber((*self._values, *secrets))

    def __call__(self, text) -> str:
        if text is None:
            return ""
        out = str(text)
        for value in self._values:
            if value in out:
                out = out.replace(value, MASK)
        return redact(out)


def clip(text: str, limit: int = MAX_DETAIL) -> str:
    """One line, trimmed to ``limit`` characters (with an ellipsis when cut)."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


NO_SECRETS = Scrubber()
