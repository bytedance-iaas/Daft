"""Detail tables: server-side slices of a revision's Parquet files (design doc 03 §6; C1 TableSpec).

``curation report`` writes one ``tables/<id>.parquet`` per C1 ``TableSpec``, sorted
by episode index. A page is read with pyarrow - the row groups that hold its rows,
nothing else - and never by starting a CLI process.

* **Sorting** only by the columns the registry whitelists for the table
  (``TableSpec.sortable``). The row order of a (table, column, direction) is computed
  once from that one column and cached; missing values (null or NaN) sort last in
  both directions, ties keep the file order (episode index), so the order is total
  and stable.
* **Cursors** carry the revision and the position of the last row returned; the
  filters (table, column, direction) are bound into them. A committed revision never
  changes, so a position in its order is as good as a key. A cursor issued for
  another revision than the one being read answers 409 ``result_changed`` (the client
  starts from the first page again, instead of mixing two revisions); a cursor of
  another table or sort is a 400.
"""
from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

from curation.contracts import modules as registry

from ..errors import ApiError
from ..pagination import CursorError, decode_cursor, encode_cursor
from .files import LRU, identity, json_safe
from .revision import Revision

CURSOR_KIND = "report_table"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def table_spec(table_id: str) -> registry.TableSpec | None:
    for m in registry.MODULES:
        for t in m.tables:
            if t.id == table_id:
                return t
    return None


class ParquetTable:
    """One Parquet file: its schema, row groups, a column, some rows (cached by bytes)."""

    def __init__(self, path: Path, cache: LRU):
        import pyarrow.parquet as pq

        self.path = path
        self.ident = identity(path)
        self._cache = cache
        self._file = pq.ParquetFile(path)
        meta = self._file.metadata
        self.num_rows = int(meta.num_rows)
        self._starts: list[int] = []
        start = 0
        for i in range(meta.num_row_groups):
            self._starts.append(start)
            start += meta.row_group(i).num_rows
        self.columns = [n for n in self._file.schema_arrow.names
                        if not n.startswith("__index_level_")]

    def _row_group(self, i: int):
        key = ("rg", str(self.path), self.ident, i)
        return self._cache.get_or_make(key, lambda: self._file.read_row_group(i, columns=self.columns),
                                       size_of=lambda t: int(t.nbytes))

    def column(self, name: str):
        """The whole column as one Arrow array (nulls when the file has no such column)."""
        import pyarrow as pa

        if name not in self.columns:
            return pa.nulls(self.num_rows)
        return self._file.read(columns=[name]).column(name).combine_chunks()

    def order(self, column: str, descending: bool) -> list[int]:
        """Row positions in the requested order (cached per file, column and direction)."""
        key = ("order", str(self.path), self.ident, column, descending)

        def make():
            import pyarrow as pa
            import pyarrow.compute as pc

            values = self.column(column)
            if pa.types.is_floating(values.type):
                values = pc.if_else(pc.is_nan(values), pa.scalar(None, values.type), values)
            idx = pc.sort_indices(pa.table({"k": values}),
                                  sort_keys=[("k", "descending" if descending else "ascending")],
                                  null_placement="at_end")
            return idx.to_pylist()

        return self._cache.get_or_make(key, make, size_of=lambda v: 8 * len(v))

    def rows(self, positions: list[int]) -> list[dict]:
        """The rows at ``positions`` (file row numbers), in that order."""
        by_group: dict[int, list[tuple[int, int]]] = {}
        for k, pos in enumerate(positions):
            g = bisect.bisect_right(self._starts, pos) - 1
            by_group.setdefault(g, []).append((k, pos - self._starts[g]))
        out: list[dict | None] = [None] * len(positions)
        for g, wanted in by_group.items():
            table = self._row_group(g)
            picked = table.take([local for _, local in wanted]).to_pylist()
            for (k, _), row in zip(wanted, picked):
                out[k] = row
        return [json_safe(r) for r in out if r is not None]


def open_table(revision: Revision, table_id: str) -> ParquetTable:
    """The table of the revision; 404 when the registry or the revision has no such table."""
    if table_spec(table_id) is None:
        known = [t.id for m in registry.MODULES for t in m.tables]
        raise ApiError("not_found", f"没有明细表 {table_id}：注册表里的明细表是 {'、'.join(known)}",
                       details={"table": table_id, "known": known})
    path = revision.table_file(table_id)
    if path is None:
        raise ApiError("not_found",
                       f"结果版本 r{revision.number} 没有明细表 {table_id}（对应的模块没有勾选，或这一版没有它）",
                       details={"table": table_id, "revision": revision.number})
    if not path.is_file():
        raise ApiError("not_found", f"结果版本 r{revision.number} 的明细表 {table_id} 文件不在本地工作目录里",
                       details={"table": table_id, "revision": revision.number,
                                "reason": "table_missing"})
    try:
        return ParquetTable(path, revision.store.tables)
    except Exception as exc:  # noqa: BLE001 - pyarrow raises several kinds
        raise ApiError("internal", f"明细表 {table_id} 读不了：{type(exc).__name__}",
                       details={"table": table_id, "revision": revision.number}) from None


def page(revision: Revision, table_id: str, *, sort: str | None, order: str,
         cursor: str | None, limit: int) -> dict:
    """C4 ``getReportTable``: ``{items, next_cursor, has_more, columns, revision}``."""
    table = open_table(revision, table_id)             # 404 for a table nobody declared
    spec = table_spec(table_id)
    sort = sort or spec.default_sort
    if sort not in spec.sortable:
        raise ApiError("validation_failed",
                       f"明细表 {table_id} 不能按 {sort} 排序（可以按：{'、'.join(spec.sortable)}）",
                       details={"errors": [{"field": "sort", "problem": "not in the whitelist"}],
                                "sortable": list(spec.sortable)})
    scope = {"task": revision.task.id, "table": table_id, "sort": sort, "order": order}
    start = 0
    if cursor:
        key = decode_cursor(cursor, CURSOR_KIND, scope=scope)
        if not (isinstance(key, list) and len(key) == 2 and all(
                isinstance(v, int) and not isinstance(v, bool) for v in key)):
            raise CursorError("cursor has no revision and position")
        rev, last = key
        if rev != revision.number:
            raise ApiError("result_changed",
                           f"结果版本已从 r{rev} 换成 r{revision.number}，明细表请从第一页重新加载",
                           details={"cursor_revision": rev, "revision": revision.number})
        if not -1 <= last < table.num_rows:
            raise CursorError("cursor position is outside the table")
        start = last + 1
    positions = table.order(sort, order == "desc")
    chunk = positions[start:start + limit]
    more = start + len(chunk) < table.num_rows
    next_cursor = encode_cursor(CURSOR_KIND, [revision.number, start + len(chunk) - 1],
                                scope=scope) if more else None
    items: list[Any] = table.rows(chunk)
    return {"items": items, "next_cursor": next_cursor, "has_more": more,
            "columns": list(table.columns), "revision": revision.number}
