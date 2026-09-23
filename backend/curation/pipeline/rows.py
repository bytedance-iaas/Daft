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

mcap and lance (D44, v1's PR #155) are read by v1's readers for those formats,
chosen by v1's own sniffing (``run.py``: lance, then mcap, else LeRobot) and with
the same arguments v1 gives them: :class:`ContainerRowSource`, :func:`read_rows`,
:func:`meta_rows`. The mcap topic mapping is the site configuration's
``ingest.mcap_mapping`` (:func:`configure_ingest`).
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from functools import partial

from ..ingest import lerobot_reader as lr

#: ``ingest.mcap_mapping`` of the command's configuration (v1 passes it to every mcap read)
_INGEST: dict = {"mcap_mapping": None}


def configure_ingest(cfg: dict | None) -> None:
    """The configuration's ingest settings for this process: v1's format switches
    (``ingest.lance_enabled`` / ``ingest.mcap_enabled``, default on) and the mcap mapping."""
    from ..ingest import lance_reader, mcap_reader

    lance_reader.apply_config(cfg)
    mcap_reader.apply_config(cfg)
    _INGEST["mcap_mapping"] = ((cfg or {}).get("ingest") or {}).get("mcap_mapping") or None


def input_format(input_dir: str) -> str:
    """v1's sniffing (``pipeline/run.py``): ``lance``, ``mcap``, else ``lerobot``. A
    ``tos://`` input is LeRobot: remote mcap / lance are read from a local copy."""
    if str(input_dir).startswith("tos://"):
        return "lerobot"
    from ..ingest.lance_reader import is_lance_dataset
    from ..ingest.mcap_reader import is_mcap_dataset

    if is_lance_dataset(input_dir):
        return "lance"
    if is_mcap_dataset(input_dir):
        return "mcap"
    return "lerobot"


def _readers(fmt: str) -> tuple[Callable, Callable]:
    """(read_rows, read_meta) of a format, bound the way v1's ``run_pipeline`` binds them."""
    if fmt == "mcap":
        from ..ingest import mcap_reader as m

        mp = _INGEST["mcap_mapping"]
        return partial(m.read_mcap_rows, mapping=mp), partial(m.read_mcap_meta, mapping=mp)
    if fmt == "lance":
        from ..ingest import lance_reader as m

        return m.read_lance_rows, m.read_lance_meta
    return lr.read_lerobot_rows, lr.read_lerobot_meta


def read_rows(input_dir: str, **kw) -> list[dict]:
    """v1's eager rows for the input's format (dedup reads its chunks through this)."""
    return _readers(input_format(input_dir))[0](input_dir, **kw)


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


#: the dataset-level values v1's readers attach to every row of one read
#: (``lerobot_reader._attach_semantics``, plus the mcap reader's row-level robot type)
SEMANTICS_COLUMNS = ("embodiment_id", "control_mode", "action_space", "proprio_space",
                     "gripper_dims", "angle_dims", "euler_triplet", "stuck_strategy", "unit",
                     "semantics_source", "semantics_extras")


class ContainerRowSource:
    """Per-episode rows of an mcap / lance dataset, exactly as v1 reads the selection.

    v1 reads the task's whole selection in one go (``read_mcap_lazy`` /
    ``read_lance_lazy``) and resolves the dataset semantics - control mode, action and
    proprio spaces, gripper columns, the velocity calibration - on its first 100 rows.
    A v2 stage reads only its own episodes (the survivors of the stage before), so the
    semantics are resolved here on those same rows - the first ``min(100,
    max_episodes)`` of ``selection``, the task's selection - and attached to every row
    this source returns; everything else in a row does not depend on which other
    episodes were read with it. One episode that cannot be read is that episode's
    error (D24), as in :class:`RowSource`; so is one whose action width differs from
    the sample's (v1 refuses the whole read for it).

    ``fetch(episodes)`` makes the episodes' source objects local first (a remote
    dataset's source cache). Reads of lance are serialised: its reader extracts shared
    video files that two threads must not write at once.
    """

    def __init__(self, fmt: str, input_dir: str, episodes: Iterable[int], *,
                 embodiment_id: str | None = None, max_episodes: int | None = None,
                 selection: Iterable[int] | None = None,
                 fetch: Callable[[list[int]], None] | None = None):
        import numpy as np

        self.fmt, self.input_dir, self.embodiment_id = fmt, input_dir, embodiment_id
        self._read = _readers(fmt)[0]
        self._fetch = fetch or (lambda eps: None)
        self._lock = threading.Lock() if fmt == "lance" else None
        wanted = sorted({int(e) for e in episodes})
        chosen = sorted({int(e) for e in (selection if selection is not None else wanted)})
        n = (min(lr.SEMANTICS_VOTE_EPISODES, int(max_episodes)) if max_episodes
             else lr.SEMANTICS_VOTE_EPISODES)
        sample = chosen[:n] or wanted[:n]
        if fmt == "lance":
            from ..ingest.lance_reader import lance_dataset_info
            from ..ingest.validate import validate_info

            self._fetch(sample)
            validate_info(lance_dataset_info(input_dir), input_dir)  # v1: validate=True
        rows = self._rows(sample)
        if not rows:
            raise lr.NotADatasetError(f"'{input_dir}': none of the episodes {sample[:8]} "
                                      f"could be read")
        self.semantics = {k: rows[0][k] for k in SEMANTICS_COLUMNS if k in rows[0]}
        self._widths = {np.asarray(r["action"]).shape[1] for r in rows
                        if np.asarray(r["action"]).ndim == 2}
        wanted_set = set(wanted)
        self._pending = {index_of(r["episode_id"]): r for r in rows
                         if index_of(r["episode_id"]) in wanted_set}
        for r in rows:
            if index_of(r["episode_id"]) not in wanted_set:
                self._drop_videos(r)
        self.order = wanted

    @property
    def available(self) -> set[int]:
        return set(self.order)

    def _rows(self, episodes: list[int]) -> list[dict]:
        self._fetch(list(episodes))
        kw = dict(episode_indices=set(episodes), embodiment_id=self.embodiment_id,
                  validate=False)
        if self._lock is None:
            return self._read(self.input_dir, **kw)
        with self._lock:
            return self._read(self.input_dir, **kw)

    def get(self, episode_index: int) -> dict:
        """The row of one episode; raises :class:`EpisodeReadError`."""
        import numpy as np

        from ..ingest.validate import validate_episode_row

        idx = int(episode_index)
        try:
            row = self._pending.pop(idx, None)
            if row is None:
                got = self._rows([idx])
                if not got:
                    raise EpisodeReadError(f"episode {idx} is not in the dataset")
                row = got[0]
                row.update(self.semantics)
            validate_episode_row(row)
            width = np.asarray(row["action"]).shape[1]
            if self._widths and width not in self._widths:
                raise EpisodeReadError(
                    f"action 维度跨 episode 不一致: {sorted(self._widths | {width})}"
                    f"(同一数据集应同构;本条 {width} 维)")
            return row
        except EpisodeReadError:
            raise
        except Exception as e:  # noqa: BLE001 - reader errors are many; one episode fails
            raise EpisodeReadError(f"{type(e).__name__}: {e}") from e

    def release(self, row: dict | None) -> None:
        """An episode is done: its mcap videos (muxed for this read) can go."""
        if row is not None:
            self._drop_videos(row)

    def _drop_videos(self, row: dict) -> None:
        if self.fmt != "mcap":
            return                              # lance's mp4s are shared by several episodes
        from ..ingest import mcap_reader

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


def open_row_source(input_dir: str, episodes: Iterable[int], *,
                    embodiment_id: str | None = None, max_episodes: int | None = None,
                    selection: Iterable[int] | None = None,
                    fetch: Callable[[list[int]], None] | None = None, fmt: str | None = None):
    """The per-episode row source of the input's format (``fmt``: the caller knows it -
    a remote dataset's local copy may not hold the files v1 sniffs by yet)."""
    fmt = fmt or input_format(input_dir)
    if fmt == "lerobot":
        return RowSource(input_dir, episodes, embodiment_id=embodiment_id,
                         max_episodes=max_episodes)
    return ContainerRowSource(fmt, input_dir, episodes, embodiment_id=embodiment_id,
                              max_episodes=max_episodes, selection=selection, fetch=fetch)


def cleanup(input_dir: str) -> None:
    """Remove the temporary videos v1's mcap / lance readers made for ``input_dir`` (v1
    calls the same functions at the end of a run); idempotent, a no-op for LeRobot."""
    from ..ingest import lance_reader, mcap_reader

    mcap_reader.cleanup_video_cache(input_dir)
    lance_reader.cleanup_video_cache(input_dir)


def meta_rows(input_dir: str, episodes: Iterable[int] | None, *,
              embodiment_id: str | None = None, max_episodes: int | None = None) -> list[dict]:
    """v1's light metadata rows (instruction, video pointers, length) for ``episodes``."""
    wanted = None if episodes is None else {int(e) for e in episodes}
    fmt = input_format(input_dir)
    if fmt != "lerobot":
        return _readers(fmt)[1](input_dir, max_episodes=max_episodes, episode_indices=wanted,
                                embodiment_id=embodiment_id, skip_missing=True)
    return lr.read_lerobot_meta(input_dir, max_episodes=max_episodes,
                                episode_indices=wanted, embodiment_id=embodiment_id,
                                skip_missing=True)


def index_of(episode_id: str) -> int:
    return int(str(episode_id).lstrip("ep"))


def column(row: dict, name: str, default):
    """A row value with v1's fallback for a column the source did not produce."""
    return row[name] if name in row else default
