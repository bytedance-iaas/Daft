"""One committed result revision of a task (design doc 06 §1; C2 ``commit.json``).

``revisions/r<NNNN>/`` holds the funnel verdicts, the four lists, the merged label
audit, the report, the performance profile and the detail tables, with
``commit.json`` written last. Readers trust only a revision that has it, and the
Daemon serves only revisions up to ``task.result_rev`` - a newer committed one is
still being uploaded and verified before the CAS switch (D25).

Everything here reads files and caches what it parsed in the store's caches, keyed
by the commit file's identity: a committed revision never changes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from curation.contracts import modules as registry

from ..errors import ApiError
from ..repo import protocol as P
from . import records as R
from .files import cached_json, identity

if TYPE_CHECKING:
    from .store import ResultStore

log = logging.getLogger("daemon.results")

REVISIONS_DIR = "revisions"
COMMIT_NAME = "commit.json"
#: passed / reject / held are disjoint and cover every episode; review is a view (C2 final-list)
LISTS = ("passed", "reject", "held")
_KEY_FILES = (COMMIT_NAME, "report.json", "passed.json", "reject.json", "held.json", "review.json",
              "label_audit.json")


def revision_name(number: int) -> str:
    return f"r{int(number):04d}"


def revision_rel(number: int) -> str:
    return f"{REVISIONS_DIR}/{revision_name(number)}"


class Revision:
    """The files of revision ``number`` in ``run_dir``; built by :meth:`ResultStore.revision`."""

    def __init__(self, store: "ResultStore", task: P.Task, number: int, run_dir: Path):
        self.store = store
        self.task = task
        self.number = int(number)
        self.run_dir = Path(run_dir)
        self.dir = self.run_dir / REVISIONS_DIR / revision_name(number)
        # a committed revision never changes; the identities of the files derived data is
        # built from still go into the key, so files restored or rewritten by hand are seen
        self.key = (str(self.dir),) + tuple(identity(self.dir / name) for name in _KEY_FILES)

    # -- files ----------------------------------------------------------------------
    def _doc(self, name: str, *, required: bool = True) -> Any:
        try:
            return cached_json(self.store.docs, self.dir / name)
        except FileNotFoundError:
            if not required:
                return None
            raise ApiError("internal", f"结果版本 r{self.number} 缺少 {name}，这个版本的文件不完整",
                           details={"revision": self.number, "file": name}) from None
        except ValueError:
            raise ApiError("internal", f"结果版本 r{self.number} 的 {name} 不是合法的 JSON",
                           details={"revision": self.number, "file": name}) from None

    def commit(self) -> dict:
        doc = self._doc(COMMIT_NAME)
        return doc if isinstance(doc, dict) else {}

    def report(self) -> dict:
        return self._doc("report.json")

    def perf(self) -> dict | None:
        doc = self._doc("perf.json", required=False)
        return doc if isinstance(doc, dict) else None

    def final_list(self, name: str) -> dict:
        doc = self._doc(f"{name}.json")
        return doc if isinstance(doc, dict) else {"episodes": []}

    def label_audit(self) -> dict:
        doc = self._doc("label_audit.json", required=False)
        return doc if isinstance(doc, dict) else {}

    # -- derived ----------------------------------------------------------------------
    def _derived(self, what: str, make):
        return self.store.derived.get_or_make((what, self.key), make)

    def modules(self) -> list[str]:
        """The revision's modules in registry order: the report's sections."""
        def make():
            ids = [m.get("id") for m in self.report().get("modules") or [] if isinstance(m, dict)]
            order = {mid: i for i, mid in enumerate(registry.ids())}
            return sorted({i for i in ids if isinstance(i, str)},
                          key=lambda m: (order.get(m, len(order)), m))
        return self._derived("modules", make)

    def entries(self) -> dict[int, tuple[str, dict]]:
        """episode -> (``passed`` | ``reject`` | ``held``, its list entry)."""
        def make():
            out: dict[int, tuple[str, dict]] = {}
            for name in LISTS:
                for e in self.final_list(name).get("episodes") or []:
                    if isinstance(e, dict) and isinstance(e.get("episode_index"), int):
                        out[int(e["episode_index"])] = (name, e)
            return out
        return self._derived("entries", make)

    def review(self) -> dict[int, dict]:
        """episode -> its ``review.json`` entry."""
        def make():
            return {int(e["episode_index"]): e
                    for e in self.final_list("review").get("episodes") or []
                    if isinstance(e, dict) and isinstance(e.get("episode_index"), int)}
        return self._derived("review", make)

    def audit_entries(self) -> dict[int, dict]:
        """episode -> its label-audit entry (``label``, ``caption``, ``reason``...), first tier wins."""
        def make():
            out: dict[int, dict] = {}
            for tier in ("high", "mid_for_review", "low_caption_unstable"):
                for e in self.label_audit().get(tier) or []:
                    if not isinstance(e, dict):
                        continue
                    try:
                        ep = int(str(e.get("id")).lstrip("ep"))
                    except ValueError:
                        continue
                    out.setdefault(ep, e)
            return out
        return self._derived("audit", make)

    # -- module records -----------------------------------------------------------------
    def _parts(self, module: str) -> list[str]:
        parts = (self.commit().get("parts") or {}).get(module)
        return [str(p) for p in parts] if isinstance(parts, list) else []

    def _index(self, module: str, exact: bool) -> R.RecordIndex:
        files = R.module_files(self.run_dir, module, self._parts(module))
        key = ("records", self.key, module, R.files_key(files), exact)
        return self.store.derived.get_or_make(key, lambda: R.RecordIndex(files, exact=exact))

    def record(self, module: str, episode: int) -> dict | None:
        """``module``'s record of ``episode`` as this revision saw it (None: it has none)."""
        return R.lookup(lambda exact: self._index(module, exact), episode)

    # -- tables -------------------------------------------------------------------------
    def table_file(self, table_id: str) -> Path | None:
        """The Parquet file of a detail table the report lists, None when it lists none."""
        for sec in self.report().get("modules") or []:
            for t in (sec or {}).get("tables") or []:
                if isinstance(t, dict) and t.get("id") == table_id:
                    rel = str(t.get("file") or f"tables/{table_id}.parquet")
                    path = (self.dir / rel).resolve()
                    try:
                        path.relative_to(self.dir.resolve())
                    except ValueError:
                        return None
                    return path
        return None
