"""An episode's frame columns from its parquet rows, and min / max thinning (design doc 18 §4.3).

LeRobot v2 keeps one parquet per episode; v3 keeps many episodes per file, so
:func:`read_episode_columns` picks the row groups whose ``index`` statistics overlap the episode's
global frame window (``dataset_from_index`` .. ``dataset_to_index``; ``episode_index`` statistics
when there are no ``index`` ones) and reads only the columns asked for - on TOS through a
:class:`~curation.streams.rangefile.RangeFile`, so a 300 MB file costs a footer read and the
episode's column chunks, never the whole object.

:func:`thin` keeps the envelope: each bucket of raw samples becomes two samples (its first and last
time) carrying the bucket's minimum and maximum of every line, in the order they occur, so peaks
survive at any zoom.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def read_episode_columns(fileobj, columns: list[str], *, from_index: int | None = None,
                         to_index: int | None = None, episode_index: int | None = None) -> dict[str, Any]:
    """``{column: values}`` of one episode: 2-D float arrays for fixed-size numeric lists, 1-D arrays
    for numeric scalars, Python lists otherwise. Columns the file does not have are left out.
    ``from_index`` / ``to_index`` (global frame indices, ``to`` exclusive) or ``episode_index``
    select the rows of a multi-episode (v3) file; without them every row is the episode's."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(fileobj)
    names = set(pf.schema_arrow.names)
    wanted = [c for c in dict.fromkeys(columns) if c in names]
    by_window = from_index is not None and to_index is not None and "index" in names
    by_episode = episode_index is not None and "episode_index" in names
    groups = list(range(pf.metadata.num_row_groups))
    if by_window or by_episode:
        # a row group is read when the episode table's window or the rows' own episode index say so:
        # a wrong window in the table (an injected or broken one) still finds the episode's rows
        groups = [g for g in groups
                  if (by_window and _overlaps(pf, g, "index", from_index, to_index, None))
                  or (by_episode and _overlaps(pf, g, "episode_index", None, None, episode_index))]
    read_cols = list(dict.fromkeys(wanted + (["episode_index"] if by_episode else [])
                                   + (["index"] if by_window and not by_episode else [])))
    if not groups or not read_cols:
        return {c: [] for c in wanted}
    table = pf.read_row_groups(groups, columns=read_cols)
    if by_episode:                       # the rows' own label wins over the table's window
        table = table.filter(pc.equal(table.column("episode_index"), episode_index))
    elif by_window:
        idx = table.column("index")
        table = table.filter(pc.and_(pc.greater_equal(idx, from_index), pc.less(idx, to_index)))
    return {c: _values(table.column(c)) for c in wanted}


def _overlaps(pf, group: int, column: str, lo, hi, episode) -> bool:
    meta = pf.metadata.row_group(group)
    for i in range(meta.num_columns):
        col = meta.column(i)
        if col.path_in_schema != column:
            continue
        st = col.statistics
        if st is None or not st.has_min_max:
            return True
        if column == "index":
            return not (st.max < lo or st.min >= hi)
        return st.min <= episode <= st.max
    return True


def _values(col):
    import pyarrow as pa

    t = col.type
    if pa.types.is_fixed_size_list(t) or pa.types.is_list(t) or pa.types.is_large_list(t):
        inner = t.value_type
        if pa.types.is_floating(inner) or pa.types.is_integer(inner) or pa.types.is_boolean(inner):
            rows = col.to_pylist()
            width = max((len(r) for r in rows if r is not None), default=0)
            out = np.full((len(rows), width), np.nan, dtype=np.float64)
            for i, r in enumerate(rows):
                if r:
                    out[i, :len(r)] = [np.nan if v is None else float(v) for v in r]
            return out
        return col.to_pylist()
    if pa.types.is_floating(t) or pa.types.is_integer(t) or pa.types.is_boolean(t):
        return np.asarray(col.to_numpy(zero_copy_only=False), dtype=np.float64)
    return col.to_pylist()


def episode_times(columns: dict[str, Any], fps: float | None, n: int) -> np.ndarray:
    """Episode time of every frame: ``timestamp`` from its first value, else ``frame_index / fps``,
    else the row number at ``fps`` (30 when unknown). A clock that goes backwards is not used
    (:func:`clock_problem` says why), since the player's time must only move forward."""
    ts = columns.get("timestamp")
    if isinstance(ts, np.ndarray) and ts.ndim == 1 and len(ts) == n and n and clock_problem(ts) is None:
        return ts - ts[0]
    rate = float(fps) if fps else 30.0
    fi = columns.get("frame_index")
    if isinstance(fi, np.ndarray) and fi.ndim == 1 and len(fi) == n and n and clock_problem(fi) is None:
        return (fi - fi[0]) / rate
    return np.arange(n, dtype=np.float64) / rate


def clock_problem(values: np.ndarray) -> str | None:
    """Why a per-frame clock column cannot be the episode clock, or None."""
    if not np.all(np.isfinite(values)):
        return "有非数值"
    if len(values) > 1 and np.any(np.diff(values) < 0):
        return "不单调"
    return None


def window(t: np.ndarray, start: float | None, end: float | None) -> slice:
    """Rows whose time lies in [start, end]."""
    a = 0 if start is None else int(np.searchsorted(t, start, side="left"))
    b = len(t) if end is None else int(np.searchsorted(t, end, side="right"))
    return slice(a, max(a, b))


def thin(t: np.ndarray, lines: list[np.ndarray], points: int) -> tuple[np.ndarray, list[np.ndarray], bool]:
    """At most ``points`` samples per line; (times, lines, thinned)."""
    n = len(t)
    if n <= points or points < 2:
        return t, lines, False
    buckets = max(1, points // 2)
    edges = np.linspace(0, n, buckets + 1).astype(int)
    out_t = np.empty(2 * buckets, dtype=np.float64)
    outs = [np.empty(2 * buckets, dtype=np.float64) for _ in lines]
    for b in range(buckets):
        lo, hi = edges[b], max(edges[b] + 1, edges[b + 1])
        out_t[2 * b] = t[lo]
        out_t[2 * b + 1] = t[hi - 1]
        for k, line in enumerate(lines):
            seg = line[lo:hi]
            finite = np.isfinite(seg)
            if not finite.any():
                outs[k][2 * b] = outs[k][2 * b + 1] = np.nan
                continue
            vals = np.where(finite, seg, np.nan)
            i_min, i_max = int(np.nanargmin(vals)), int(np.nanargmax(vals))
            first, second = (i_min, i_max) if i_min <= i_max else (i_max, i_min)
            outs[k][2 * b], outs[k][2 * b + 1] = vals[first], vals[second]
    return out_t, outs, True


def json_values(values: np.ndarray, digits: int = 5) -> list:
    """Floats rounded for the wire; NaN / inf as null."""
    out = []
    for v in values.tolist():
        if v is None or not math.isfinite(v):
            out.append(None)
        else:
            out.append(round(v, digits))
    return out
