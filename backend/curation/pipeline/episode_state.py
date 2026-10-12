"""SQLite index and durable episode handoff for the Daemon's episode pipeline.

The public check result files remain JSONL. This private database is the resume
source for a pipelined run: a stage commits its records and next destination in
one transaction after the corresponding result lines have been fsynced.

A run has two blocks side by side (design doc 17 §3): the Daemon names their streaming
stages once (:meth:`EpisodeState.set_blocks`), every episode has a position in each block
(``block_progress``), and a stage hands every episode on to the next one of its block - a
finding or an execution error stops nothing (D57).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def state_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".orchestr" / "episodes.sqlite3"


def index_verdict(rec: dict) -> str:
    """The verdict column of a record: ``error`` when the module failed on the episode, ``skipped`` when it wrote
    the record without judging the episode (D94), else ``ok`` (the live module cards count them,
    :meth:`EpisodeState.module_counts`)."""
    from .records import is_error, skipped_reason

    return "error" if is_error(rec) else "skipped" if skipped_reason(rec) else "ok"


def index_findings(rec: dict) -> int | None:
    """The findings column of a record: how many it reports (None: the module failed on the episode)."""
    from .records import is_error

    return None if is_error(rec) else len(rec.get("findings") or [])


#: a record's row: (module, episode, record, verdict, findings)
_PUT = "INSERT OR REPLACE INTO results (module, episode, record, verdict, findings) VALUES (?, ?, ?, ?, ?)"


def read_record(path: str | Path, module: str, episode: int) -> dict | None:
    """One record of the store at ``path``, read on a connection of its own: safe from any thread (an
    :class:`EpisodeState` connection belongs to the thread that opened it). The check stage's episode workers read
    data integrity's record with it (D92, design doc 28 §1)."""
    con = sqlite3.connect(str(path), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        row = con.execute("SELECT record FROM results WHERE module=? AND episode=?",
                          (module, int(episode))).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


class EpisodeState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS indexed_modules (
                module TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS results (
                module TEXT NOT NULL,
                episode INTEGER NOT NULL,
                record TEXT NOT NULL,
                verdict TEXT,
                findings INTEGER,
                PRIMARY KEY (module, episode)
            );
            CREATE TABLE IF NOT EXISTS clock (
                id INTEGER PRIMARY KEY CHECK (id=1),
                seq INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO clock VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS failed_stages (
                stage TEXT PRIMARY KEY,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS block_progress (
                episode INTEGER NOT NULL,
                block TEXT NOT NULL,
                last_stage TEXT NOT NULL,
                next_stage TEXT NOT NULL,
                reason TEXT,
                updated_seq INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (episode, block)
            );
            CREATE INDEX IF NOT EXISTS block_progress_recent ON block_progress(updated_seq DESC);
        """)
        self._chains = self._load_chains()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ the two blocks
    def _load_chains(self) -> dict[str, list[tuple[str, list[str]]]] | None:
        row = self.db.execute("SELECT value FROM meta WHERE key='blocks'").fetchone()
        if row is None:
            return None
        return {block: [(stage, list(mods)) for stage, mods in chain] for block, chain in json.loads(row[0]).items()}

    def set_blocks(self, chains: dict[str, list[tuple[str, list[str]]]]) -> None:
        """Each block's streaming stages in order, with their modules. Every episode then has a position in
        each block and every stage hands every episode on (design doc 17 §3.3)."""
        doc = {block: [[stage, list(mods)] for stage, mods in chain] for block, chain in chains.items()}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('blocks', ?)", (json.dumps(doc),))
        self._chains = self._load_chains()

    @property
    def blocks(self) -> dict[str, list[tuple[str, list[str]]]] | None:
        """The blocks' streaming stages; None until the Daemon names them (:meth:`set_blocks`)."""
        return self._chains

    def block_of(self, stage: str) -> str | None:
        for block, chain in (self._chains or {}).items():
            if any(sid == stage for sid, _ in chain):
                return block
        return None

    def _place(self, episodes: list[int], stage: str, next_stage: str, reason: str | None) -> None:
        """Within the caller's transaction: the episodes' position in ``stage``'s block."""
        block = self.block_of(stage)
        if block is None:
            raise ValueError(f"stage {stage!r} is in no block of this run")
        first = self._reserve(len(episodes))
        self.db.executemany("INSERT OR REPLACE INTO block_progress VALUES (?, ?, ?, ?, ?, ?)",
                            ((ep, block, stage, next_stage, reason, first + i) for i, ep in enumerate(episodes)))

    def positions(self, episodes: list[int], block: str) -> dict[int, str]:
        """``episode -> the stage it waits for`` in ``block`` (``done`` at its end); an episode not yet in the
        block is absent (it starts at the block's first stage)."""
        out = {}
        for start in range(0, len(episodes), 900):
            chunk = episodes[start:start + 900]
            if not chunk:
                continue
            slots = ",".join("?" for _ in chunk)
            out.update(self.db.execute(
                f"SELECT episode, next_stage FROM block_progress WHERE block=? AND episode IN ({slots})",
                (block, *chunk)).fetchall())
        return out

    def seed_blocks(self, episodes: list[int]) -> None:
        """Positions from the records alone, for episodes a block has none for (records written outside the
        pipeline): past every stage all of whose modules have a record of the episode."""
        with self.db:
            for block, chain in (self._chains or {}).items():
                known = self.positions(episodes, block)
                have = {}
                for stage, mods in chain:
                    for module in mods:
                        for (ep,) in self.db.execute("SELECT episode FROM results WHERE module=?", (module,)):
                            have.setdefault((stage, ep), set()).add(module)
                for ep in episodes:
                    if ep in known:
                        continue
                    last = None
                    for i, (stage, mods) in enumerate(chain):
                        if have.get((stage, ep), set()) != set(mods):
                            break
                        last = i
                    if last is not None:
                        nxt = chain[last + 1][0] if last + 1 < len(chain) else "done"
                        self._place([ep], chain[last][0], nxt, None)

    def completed(self, modules: list[str]) -> list[int]:
        """Episodes with a record (any status) from every one of ``modules``."""
        if not modules:
            return []
        slots = ",".join("?" for _ in modules)
        return [row[0] for row in self.db.execute(
            f"SELECT episode FROM results WHERE module IN ({slots}) GROUP BY episode "
            "HAVING count(DISTINCT module)=? ORDER BY episode", (*modules, len(modules)))]

    def stage_states(self, episode: int) -> dict[str, str]:
        """``stage -> done | error | waiting`` over the blocks' streaming stages (C4 ``PipelineEpisode.stages``):
        a stage the episode is past is done, or error when one of its modules failed on it."""
        out: dict[str, str] = {}
        records = self.episode_records(episode)
        for block, chain in (self._chains or {}).items():
            pos = self.positions([episode], block).get(episode)
            past = len(chain) if pos == "done" else next((i for i, (sid, _) in enumerate(chain) if sid == pos), 0)
            for i, (stage, mods) in enumerate(chain):
                if i >= past:
                    out[stage] = "waiting"
                else:
                    failed = any((records.get(m) or {}).get("status") == "error" for m in mods)
                    out[stage] = "error" if failed else "done"
        return out

    def failed_stages(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT stage, message FROM failed_stages"))

    def fail_stage(self, stage: str, message: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO failed_stages VALUES (?, ?)",
                            (stage, message))

    def indexed(self, module: str) -> bool:
        return self.db.execute("SELECT 1 FROM indexed_modules WHERE module=?",
                               (module,)).fetchone() is not None

    def _reserve(self, count: int) -> int:
        """Reserve ordered change numbers within the caller's transaction."""
        self.db.execute("UPDATE clock SET seq=seq+? WHERE id=1", (count,))
        return self.db.execute("SELECT seq FROM clock WHERE id=1").fetchone()[0] - count + 1

    def _row(self, module: str, episode: int, record: dict, default) -> tuple:
        return (module, episode, json.dumps(record, ensure_ascii=False, default=default),
                index_verdict(record), index_findings(record))

    def bootstrap(self, run_dir: str, modules: list[str]) -> None:
        """Index an existing run once, before parallel stage processes start."""
        from .records import load_parts, module_dir, read_jsonl, RESULTS_NAME, _json_default

        for module in modules:
            if self.indexed(module):
                continue
            existing = {ep: rec for ep, (_, rec) in load_parts(run_dir, module).items()}
            if not existing:
                existing = {int(rec["episode_index"]): rec for rec in read_jsonl(
                    str(Path(module_dir(run_dir, module)) / RESULTS_NAME))}
            with self.db:
                self.db.executemany(_PUT, (self._row(module, ep, rec, _json_default)
                                           for ep, rec in existing.items()))
                self.db.execute("INSERT INTO indexed_modules VALUES (?)", (module,))

    def finish(self, stage: str, episode: int, records: dict[str, dict],
               next_stage: str) -> None:
        from .records import _json_default

        with self.db:                                    # every episode goes on (D57)
            self.db.executemany(_PUT, (self._row(module, episode, record, _json_default)
                                       for module, record in records.items()))
            self._place([episode], stage, next_stage, None)

    def put_result(self, record: dict) -> None:
        from .records import _json_default

        with self.db:
            self.db.execute(_PUT, self._row(record["module"], int(record["episode_index"]), record,
                                            _json_default))

    def missing(self, stage: str, episode: int) -> None:
        """Its source files are gone (D40): left out, in every later stage too."""
        with self.db:
            for block in self._chains or {}:
                first = self._reserve(1)
                self.db.execute("INSERT OR REPLACE INTO block_progress VALUES (?, ?, ?, 'done', 'missing', ?)",
                                (episode, block, stage, first))

    def forward(self, stage: str, episodes: list[int], next_stage: str) -> None:
        """A whole module failed: the episodes go on without its records."""
        if not episodes:
            return
        with self.db:
            self._place(list(episodes), stage, next_stage, None)

    def _block_rows(self, sql_tail: str, args: tuple) -> list[dict]:
        """One row per episode: its latest change and the positions of its blocks."""
        rows = self.db.execute(
            "SELECT episode, MAX(updated_seq) AS seq FROM block_progress GROUP BY episode " + sql_tail, args).fetchall()
        out = []
        for ep, seq in rows:
            blocks = {b: {"last_stage": last, "next_stage": nxt, "reason": reason} for b, last, nxt, reason in
                      self.db.execute("SELECT block, last_stage, next_stage, reason FROM block_progress "
                                      "WHERE episode=?", (ep,))}
            done = all((blocks.get(b) or {}).get("next_stage") == "done" for b in self._chains or {})
            missing = any(v.get("reason") == "missing" for v in blocks.values())
            out.append({"episode_index": ep, "updated_seq": seq, "blocks": blocks, "done": done,
                        "reason": "missing" if missing else None})
        return out

    def recent(self, *, before: int | None = None, limit: int = 50) -> list[dict]:
        if before is not None:
            return self._block_rows("HAVING seq < ? ORDER BY seq DESC LIMIT ?", (before, limit))
        return self._block_rows("ORDER BY seq DESC LIMIT ?", (limit,))

    def episode(self, index: int) -> dict | None:
        rows = self._block_rows("HAVING episode = ?", (index,))
        return rows[0] if rows else None

    def episode_records(self, index: int) -> dict[str, dict]:
        return {module: json.loads(record) for module, record in self.db.execute(
            "SELECT module, record FROM results WHERE episode=?", (index,))}

    def totals(self) -> dict[str, int]:
        total, done = self.db.execute(
            "SELECT count(*), sum(finished) FROM (SELECT episode, sum(next_stage='done')=? AS finished "
            "FROM block_progress GROUP BY episode)", (len(self._chains or {}),)).fetchone()
        return {"started": int(total or 0), "finished": int(done or 0)}

    def records(self, module: str, episodes: list[int] | None = None) -> dict[int, dict]:
        if episodes is None:
            return {ep: json.loads(data) for ep, data in self.db.execute(
                "SELECT episode, record FROM results WHERE module=?", (module,))}
        out = {}
        for start in range(0, len(episodes), 900):
            chunk = episodes[start:start + 900]
            slots = ",".join("?" for _ in chunk)
            out.update((ep, json.loads(data)) for ep, data in self.db.execute(
                f"SELECT episode, record FROM results WHERE module=? AND episode IN ({slots})",
                (module, *chunk)))
        return out

    def module_counts(self, modules: list[str]) -> dict[str, dict[str, int]]:
        """Per module so far: the episodes it judged, the ones it failed on, those it skipped without judging
        (D94: no task text, data integrity rejected them) and, of the judged ones, those with a finding (the live
        module cards, design doc 17 §5.3)."""
        if not modules:
            return {}
        slots = ",".join("?" for _ in modules)
        out = {m: {"judged": 0, "error": 0, "skipped": 0, "flagged": 0} for m in modules}
        for module, total, errors, skipped, flagged in self.db.execute(
                "SELECT module, count(*), sum(verdict='error'), sum(verdict='skipped'), "
                f"sum(findings > 0 AND verdict='ok') FROM results WHERE module IN ({slots}) GROUP BY module",
                tuple(modules)):
            out[module] = {"judged": int(total or 0) - int(errors or 0) - int(skipped or 0),
                           "error": int(errors or 0), "skipped": int(skipped or 0), "flagged": int(flagged or 0)}
        return out

    def counts(self, module: str) -> tuple[int, int]:
        total, errors = self.db.execute(
            "SELECT count(*), sum(verdict='error') "
            "FROM results WHERE module=?", (module,)).fetchone()
        return int(total or 0), int(errors or 0)

    def stage_inputs(self, modules: list[str]) -> list[int]:
        if not modules:
            return []
        slots = ",".join("?" for _ in modules)
        return [row[0] for row in self.db.execute(
            f"SELECT DISTINCT episode FROM results WHERE module IN ({slots}) ORDER BY episode",
            modules)]
