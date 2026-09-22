"""Module result records as one committed revision saw them (C2 ``result-record``).

``checks/<module>/results.jsonl`` is the *current* compacted state and moves on with
every later ``check`` call, so it cannot answer "what did revision N see". The
revision's ``commit.json`` lists, per module, the parts its results came from; the
records of revision N are exactly those parts read the CLI's way (``records.load_parts``):
parts in ascending order, later lines winning, the highest part winning. A run
directory restored from the delivery location may only have the compacted file; then
that file is read instead.

An index maps each episode to the byte range of its winning line, so a lookup reads
one line instead of every part. Building it finds ``"episode_index": N`` in each line
without parsing it; a line where the key appears more than once (nested in
``details``) is parsed. Every lookup parses its line and checks it, and a mismatch
rebuilds the index with full parsing.

This is exact as long as a part is never appended to after a revision that used it
was committed - the CLI writes a new part for every ``check`` call.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from .files import identity

log = logging.getLogger("daemon.results")

CHECKS_DIR = "checks"
PARTS_DIR = "parts"
RESULTS_NAME = "results.jsonl"

_EP_RE = re.compile(rb'"episode_index"\s*:\s*(\d+)')


def module_files(run_dir: Path, module: str, parts: list[str] | None) -> list[Path]:
    """The files that hold ``module``'s records for a revision, lowest part first."""
    if not parts:
        return []
    mdir = run_dir / CHECKS_DIR / module
    files = [mdir / PARTS_DIR / f"{p}.jsonl" for p in sorted(parts)]
    if all(f.is_file() for f in files):
        return files
    compacted = mdir / RESULTS_NAME
    log.info("parts of %s missing under %s; reading %s instead", module, mdir, RESULTS_NAME)
    return [compacted] if compacted.is_file() else [f for f in files if f.is_file()]


def files_key(files: list[Path]) -> tuple:
    return tuple((str(f), identity(f)) for f in files)


class RecordIndex:
    """episode -> (file, offset, length) of its winning line, over ``files`` in order."""

    def __init__(self, files: list[Path], *, exact: bool = False):
        self.exact = exact
        self.entries: dict[int, tuple[Path, int, int]] = {}
        for path in files:
            self._scan(path)

    def _scan(self, path: Path) -> None:
        try:
            fh = open(path, "rb")
        except OSError:
            return
        with fh:
            offset = 0
            for line in fh:
                ep = self._episode_of(line)
                if ep is not None:
                    self.entries[ep] = (path, offset, len(line))
                offset += len(line)

    def _episode_of(self, line: bytes) -> int | None:
        if not line.strip():
            return None
        if not self.exact:
            matches = _EP_RE.findall(line)
            if len(matches) == 1:                        # unambiguous; several = nested keys
                return int(matches[0])
        try:
            rec = json.loads(line)
        except ValueError:
            return None                                  # a torn last line (killed mid-write)
        ep = rec.get("episode_index") if isinstance(rec, dict) else None
        return ep if isinstance(ep, int) and not isinstance(ep, bool) else None

    def episodes(self) -> list[int]:
        return sorted(self.entries)

    def read(self, episode: int):
        """The record; None when the episode has none; False when the index was wrong."""
        hit = self.entries.get(int(episode))
        if hit is None:
            return None
        path, offset, length = hit
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                rec = json.loads(fh.read(length))
        except (OSError, ValueError):
            return False
        if not isinstance(rec, dict) or rec.get("episode_index") != int(episode):
            return False
        return rec


def lookup(index_of: Callable[[bool], RecordIndex], episode: int) -> dict | None:
    """``index_of(exact)`` returns the (cached) index; one exact rebuild when it was wrong."""
    rec = index_of(False).read(episode)
    if rec is False:
        rec = index_of(True).read(episode)
    return rec if isinstance(rec, dict) else None
