"""v1's rows of single episodes for the data integrity module (design doc 14 §2.3, D51).

The funnel's row sources (``pipeline.rows``) resolve the dataset semantics first, from
the data of the first hundred episodes - a read that one corrupt early episode can break
for every episode, and that has no bearing on what ``validate_episode_row`` checks. This
source builds each episode's row with the same v1 readers and helpers, without the
semantics, so one broken episode stays that episode's finding.

The row keeps its reader's exception as ``__cause__`` of :class:`EpisodeReadError`: the
judge tells v1's validation errors (a finding, D51) from everything else.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

from ...pipeline.rows import EpisodeMissingSource, EpisodeReadError, _readers


class IntegrityRows:
    """``get(episode)`` -> the validated row; ``release(row)`` when done with it."""

    def __init__(self, src, episodes, *, max_episodes: int | None = None):
        from ...ingest import lerobot_reader as lr

        self.src, self.fmt, self.input_dir = src, src.kind, src.input_dir
        self._lock = threading.Lock()
        self._groups: OrderedDict = OrderedDict()
        wanted = {int(e) for e in episodes}
        if self.fmt == "lerobot":
            self.info = lr._load_info(self.input_dir)
            self.v3 = str(self.info.get("codebase_version", "")).startswith("v3")
            if self.v3:
                meta = lr._v3_ep_meta(self.input_dir, max_episodes, episode_indices=wanted)
                self._group_of, self._group_meta = {}, {}
                for key, grp in lr._v3_file_groups(meta):
                    self._group_meta[key] = grp
                    for idx in grp["episode_index"]:
                        self._group_of[int(idx)] = key
            else:
                self._eps = {int(ep["episode_index"]): ep for ep in
                             lr._v2_episode_list(self.input_dir, max_episodes, episode_indices=wanted)}
        else:
            self._read = _readers(self.fmt)[0]
            self._serial = threading.Lock() if self.fmt == "lance" else None

    def get(self, episode_index: int) -> dict:
        from ...ingest.validate import validate_episode_row

        idx = int(episode_index)
        try:
            row = self._lerobot(idx) if self.fmt == "lerobot" else self._container(idx)
            validate_episode_row(row)
            return row
        except EpisodeReadError:
            raise
        except Exception as e:  # noqa: BLE001 - the cause is kept for the judge
            raise EpisodeReadError(f"{type(e).__name__}: {e}") from e

    def _lerobot(self, idx: int) -> dict:
        from ...ingest import lerobot_reader as lr
        from ...pipeline.skipped import relative_key

        if not self.v3:
            ep = self._eps.get(idx)
            if ep is None:
                raise EpisodeReadError(f"episode {idx} is not in the episode table")
            data_path, videos = lr._v2_episode_paths(self.input_dir, self.info, ep)
            if lr._v2_missing(data_path, videos):
                raise EpisodeMissingSource([
                    relative_key(self.input_dir, p)
                    for p in [data_path] + [v["path"] for v in videos.values()]
                    if not lr.dsfs.exists(p)])
            return lr._row_v2(self.input_dir, self.info, ep)
        key = self._group_of.get(idx)
        if key is None:
            raise EpisodeReadError(f"episode {idx} is not in the episode table")
        with self._lock:
            rows = self._groups.get(key)
            if rows is None:
                rows = lr._rows_v3_group(self.input_dir, self.info, self._group_meta[key])
                self._groups[key] = rows
                while len(self._groups) > 2:            # a couple of data files in memory
                    self._groups.popitem(last=False)
            else:
                self._groups.move_to_end(key)
        for r in rows:
            if r["episode_id"] == f"ep{idx:06d}":
                return r
        raise EpisodeReadError(f"episode {idx} missing from its data file")

    def _container(self, idx: int) -> dict:
        self.src.fetch([idx])
        kw = dict(episode_indices={idx}, embodiment_id=None, validate=False)
        if self._serial is None:
            got = self._read(self.input_dir, **kw)
        else:
            with self._serial:
                got = self._read(self.input_dir, **kw)
        if not got:
            raise EpisodeReadError(f"episode {idx} is not in the dataset")
        return got[0]

    def release(self, row: dict | None) -> None:
        """mcap: the videos v1's reader muxed for this row (lance's are shared: kept)."""
        if row is None or self.fmt != "mcap":
            return
        from ...ingest import mcap_reader

        base = mcap_reader._VIDEO_DIRS.get(os.path.abspath(self.input_dir))
        if not base:
            return
        for v in (row.get("video") or {}).values():
            path = str(v.get("path") or "")
            if os.path.dirname(os.path.abspath(path)) == os.path.abspath(base):
                try:
                    os.unlink(path)
                except OSError:
                    pass
