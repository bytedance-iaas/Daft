"""Diff of the previous export against the new passed list (design doc 06, section 4.3).

Every episode of the new export falls into exactly one class; episodes of the
previous export that are not delivered any more are ``drop``:

========  =====================================================  ==================================
class     condition                                              what the exporter does
========  =====================================================  ==================================
keep      same source content, same numbering, same task         nothing: not a byte is touched
relabel   same content and numbering, the task changed           rewrites the frame table's
                                                                 ``task_index``; videos untouched
renumber  same content, ``episode_index`` or the global frame     rewrites the frame table; v2 moves
          offset changed                                          (renames) the videos, v3 leaves them
add       not in the previous export, or its source changed      exports it from the source
drop      in the previous export only                            deletes it (v3: rebuilds the video
                                                                 file it shared with others)
========  =====================================================  ==================================

"The task changed" covers the task text and its source (a human relabel, a caption)
and also a shifted ``task_index``: the task table is rebuilt in first-appearance
order like v1 does, so a new text can move other texts' numbers.  Both only rewrite
the small frame table, never a video.  An episode that is renumbered and relabelled
counts as ``renumber``.

Episodes are matched by their source ``episode_index`` and trusted only when the
``content_key`` (the source files' identity) is unchanged; a changed source episode
is a drop plus an add.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

CLASSES = ("keep", "relabel", "renumber", "add", "drop")


@dataclass(frozen=True)
class EpisodeKey:
    """What the diff compares.  ``index_from`` and ``task_index`` come from the detail
    sidecar; ``None`` (manifest only) means "unknown" and is not compared."""

    episode_index: int
    new_index: int
    content_key: str
    task_key: str
    index_from: int | None = None
    task_index: int | None = None


@dataclass
class Diff:
    classes: dict[int, str] = field(default_factory=dict)   # source episode_index -> class
    dropped: list[int] = field(default_factory=list)         # source episode_index, old order

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in CLASSES}
        for c in self.classes.values():
            out[c] += 1
        out["drop"] = len(self.dropped)
        return out

    def of(self, cls: str) -> list[int]:
        return [i for i, c in self.classes.items() if c == cls]


def _differs(a: int | None, b: int | None) -> bool:
    return a is not None and b is not None and a != b


def diff_episodes(old: Iterable[EpisodeKey] | None, new: Iterable[EpisodeKey]) -> Diff:
    """Classify ``new`` against ``old`` (``None`` = nothing exported yet: all ``add``)."""
    new = list(new)
    seen: set[int] = set()
    for e in new:
        if e.episode_index in seen:
            raise ValueError(f"episode {e.episode_index} appears twice in the new export")
        seen.add(e.episode_index)
    old_by = {e.episode_index: e for e in (old or ())}
    out = Diff()
    for e in new:
        o = old_by.get(e.episode_index)
        if o is None or o.content_key != e.content_key:
            cls = "add"
        elif o.new_index != e.new_index or _differs(o.index_from, e.index_from):
            cls = "renumber"
        elif o.task_key != e.task_key or _differs(o.task_index, e.task_index):
            cls = "relabel"
        else:
            cls = "keep"
        out.classes[e.episode_index] = cls
    new_by = {e.episode_index: e for e in new}
    for i, o in old_by.items():
        n = new_by.get(i)
        if n is None or n.content_key != o.content_key:
            out.dropped.append(i)
    return out
