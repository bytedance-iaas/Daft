"""Source rows for the v2 stages, built exactly as v1's lazy scan builds them.

v1's funnel reads the dataset through ``ingest.daft_source.LeRobotDataSource``:
dataset semantics resolved once (on the first ``min(100, max_episodes)``
episodes, whatever the selection), then per episode ``_row_v2`` (v2) or
``_rows_v3_group`` (v3, one data file at a time), ``_attach_semantics`` and the
structural validation. The check stages use the same source object and the same
helpers, so every value a check sees - the float32 action, the float64
timestamps, the video pointers, the semantics columns - is what v1's UDFs saw
(daft's struct/tensor round trip returns the same values, verified in W3).

Differences on purpose, all about isolating one bad episode (D24): a failed
validation or an unreadable parquet is an error of that episode, not an
exception that ends the whole run. An episode whose source files are missing
is left out, as v1 does (D40): :class:`EpisodeMissingSource`.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable

from ..ingest import lerobot_reader as lr


class EpisodeReadError(Exception):
    """This episode's source data cannot be read (reported as a ``read`` incident)."""


class EpisodeMissingSource(EpisodeReadError):
    """v1's ``_v2_missing``: the data parquet or a camera's video is not there. The
    episode is left out like v1 does (D40): no result line, listed as skipped."""

    def __init__(self, missing: list[str]):
        super().__init__(f"missing source file(s): {missing[:3]}")
        self.missing = list(missing)


class RowSource:
    """Per-episode rows of one LeRobot v2 / v3 dataset (thread-safe, v3 groups cached)."""

    def __init__(self, input_dir: str, episodes: Iterable[int], *,
                 embodiment_id: str | None = None, max_episodes: int | None = None):
        from ..ingest.daft_source import LeRobotDataSource

        wanted = {int(e) for e in episodes}
        self.input_dir = input_dir
        self.embodiment_id = embodiment_id
        # the very object v1's lazy scan builds (semantics resolved the same way)
        self.src = LeRobotDataSource(input_dir, max_episodes, embodiment_id, validate=True,
                                     skip_missing=True, episode_indices=wanted)
        self.info = self.src._info
        self.v3 = self.src._v3
        self._lock = threading.Lock()
        self._groups: OrderedDict = OrderedDict()
        if self.v3:
            meta = lr._v3_ep_meta(input_dir, max_episodes, episode_indices=wanted)
            self._group_of: dict[int, tuple] = {}
            self._group_meta: dict[tuple, object] = {}
            for key, grp in lr._v3_file_groups(meta):
                self._group_meta[key] = grp
                for idx in grp["episode_index"]:
                    self._group_of[int(idx)] = key
            self.order = [int(i) for i in meta["episode_index"]]
        else:
            self._eps = {int(ep["episode_index"]): ep
                         for ep in lr._v2_episode_list(input_dir, max_episodes,
                                                       episode_indices=wanted)}
            self.order = list(self._eps)

    @property
    def available(self) -> set[int]:
        return set(self.order)

    def _finish(self, rows: list[dict]) -> list[dict]:
        lr._attach_semantics(rows, self.src._sem, self.embodiment_id)
        return rows

    def get(self, episode_index: int) -> dict:
        """The row of one episode; raises :class:`EpisodeReadError`."""
        from ..ingest.validate import validate_episode_row

        idx = int(episode_index)
        try:
            if self.v3:
                row = self._v3_row(idx)
            else:
                ep = self._eps.get(idx)
                if ep is None:
                    raise EpisodeReadError(f"episode {idx} is not in the episode table")
                data_path, videos = lr._v2_episode_paths(self.input_dir, self.info, ep)
                if lr._v2_missing(data_path, videos):
                    from .skipped import relative_key

                    raise EpisodeMissingSource([
                        relative_key(self.input_dir, p)
                        for p in [data_path] + [v["path"] for v in videos.values()]
                        if not lr.dsfs.exists(p)])
                row = self._finish([lr._row_v2(self.input_dir, self.info, ep)])[0]
            validate_episode_row(row)
            return row
        except EpisodeReadError:
            raise
        except Exception as e:  # noqa: BLE001 - reader errors are many; one episode fails
            raise EpisodeReadError(f"{type(e).__name__}: {e}") from e

    def _v3_row(self, idx: int) -> dict:
        key = self._group_of.get(idx)
        if key is None:
            raise EpisodeReadError(f"episode {idx} is not in the episode table")
        with self._lock:
            rows = self._groups.get(key)
            if rows is not None:
                self._groups.move_to_end(key)
        if rows is None:
            rows = self._finish(lr._rows_v3_group(self.input_dir, self.info,
                                                  self._group_meta[key]))
            with self._lock:
                self._groups[key] = rows
                while len(self._groups) > 2:           # a couple of data files in memory
                    self._groups.popitem(last=False)
        for r in rows:
            if r["episode_id"] == f"ep{idx:06d}":
                return r
        raise EpisodeReadError(f"episode {idx} missing from its data file")


def meta_rows(input_dir: str, episodes: Iterable[int] | None, *,
              embodiment_id: str | None = None, max_episodes: int | None = None) -> list[dict]:
    """v1's light metadata rows (instruction, video pointers, length) for ``episodes``."""
    wanted = None if episodes is None else {int(e) for e in episodes}
    return lr.read_lerobot_meta(input_dir, max_episodes=max_episodes,
                                episode_indices=wanted, embodiment_id=embodiment_id,
                                skip_missing=True)


def index_of(episode_id: str) -> int:
    return int(str(episode_id).lstrip("ep"))


def column(row: dict, name: str, default):
    """A row value with v1's fallback for a column the source did not produce."""
    return row[name] if name in row else default
