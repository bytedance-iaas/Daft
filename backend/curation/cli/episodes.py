"""``--episodes``: integer indices, v1's syntax plus ``@file`` (design doc 02, section 2).

``34`` / ``10-20`` / ``3,10-12`` are parsed by v1's ``episode_select.parse_episodes``
(negative numbers, reversed ranges and ranges wider than a million are refused
there). ``@path`` reads one expression per line; blank lines and ``#`` comments
are skipped.
"""
from __future__ import annotations

from .errors import UsageError


def parse(expr: str | None) -> set[int] | None:
    """The selected indices, or None for "all"."""
    from ..episode_select import parse_episodes

    if expr is None or not str(expr).strip():
        return None
    expr = str(expr).strip()
    if expr.startswith("@"):
        path = expr[1:]
        try:
            with open(path, encoding="utf-8") as fh:
                lines = [ln.split("#", 1)[0].strip() for ln in fh]
        except OSError as e:
            raise UsageError(f"--episodes {expr}: cannot read the file: {e}") from None
        parts = [ln for ln in lines if ln]
        if not parts:
            raise UsageError(f"--episodes {expr}: the file lists no episode")
        expr_for_parse = ",".join(parts)
    else:
        expr_for_parse = expr
    try:
        return parse_episodes(expr_for_parse)
    except ValueError as e:
        raise UsageError(f"--episodes {expr!r}: {e}") from None


def reconcile(requested: set[int] | None, available, what: str = "the dataset"):
    """(selected, warning) with v1's ``reconcile_episodes`` rule, in English.

    Nothing requested -> all (None). None of the requested indices exists ->
    usage error (never an empty run). Some missing -> run the intersection and
    say how many of the requested ones were dropped.
    """
    if requested is None:
        return None, ""
    avail = {int(x) for x in available}
    kept = requested & avail
    if not kept:
        span = (f"{len(avail)} episodes, indices {min(avail)}-{max(avail)}" if avail
                else "no episode at all")
        raise UsageError(f"--episodes: none of the requested episodes exists; {what} has "
                         f"{span}, requested {preview(requested)}")
    missing = requested - avail
    if not missing:
        return kept, ""
    return kept, (f"{len(missing)} of the {len(requested)} requested episodes do not exist "
                  f"({preview(missing)}); using the {len(kept)} that do")


def preview(indices, k: int = 8) -> str:
    xs = sorted(indices)
    return ", ".join(str(x) for x in xs[:k]) + (", ..." if len(xs) > k else "")
