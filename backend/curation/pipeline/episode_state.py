"""SQLite index and durable episode handoff for the Daemon's episode pipeline.

The public check result files remain JSONL. This private database is the resume
source for a pipelined run: a stage commits its records and next destination in
one transaction after the corresponding result lines have been fsynced.

Two layouts. A run of plan 2.0 (design doc 17 §3) has two blocks side by side: the
Daemon names their streaming stages once (:meth:`EpisodeState.set_blocks`), every
episode has a position in each block (``block_progress``), and a stage hands every
episode on to the next one of its block - a finding or an execution error stops
nothing (D57). A run made before it (one chain, the funnel) keeps ``progress``: a
stage hands on its survivors only.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def state_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".orchestr" / "episodes.sqlite3"


def index_verdict(rec: dict, policy=None) -> str:
    """The verdict column of a record: 1.0's verdict, where ``fail`` means the funnel stops the episode
    under ``policy`` (the task's; the registry's default levels when None) - a record of either format."""
    from .records import is_error, legacy_verdict, passes_funnel

    if is_error(rec):
        return "error"
    if not passes_funnel(rec, policy):
        return "fail"
    verdict = legacy_verdict(rec)
    return "pass" if verdict == "fail" else verdict


def index_findings(rec: dict) -> int | None:
    """The findings column of a record: how many it reports (None: the module failed on the episode)."""
    from .records import is_error

    return None if is_error(rec) else len(rec.get("findings") or [])


#: a record's row: (module, episode, record, verdict, findings)
_PUT = "INSERT OR REPLACE INTO results (module, episode, record, verdict, findings) VALUES (?, ?, ?, ?, ?)"


class EpisodeState:
    def __init__(self, path: str | Path, policy=None):
        #: what stops an episode in the funnel (``records.passes_funnel``); the default levels when None
        self.policy = policy
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
                PRIMARY KEY (module, episode)
            );
            CREATE TABLE IF NOT EXISTS progress (
                episode INTEGER PRIMARY KEY,
                last_stage TEXT NOT NULL,
                next_stage TEXT NOT NULL,
                reason TEXT,
                updated_seq INTEGER NOT NULL DEFAULT 0
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
        if "updated_seq" not in {row[1] for row in self.db.execute("PRAGMA table_info(progress)")}:
            with self.db:
                self.db.execute("ALTER TABLE progress ADD COLUMN updated_seq INTEGER NOT NULL DEFAULT 0")
                self.db.execute("UPDATE progress SET updated_seq=episode+1")
                self.db.execute("UPDATE clock SET seq=(SELECT COALESCE(MAX(updated_seq), 0) FROM progress)")
        self.db.execute("CREATE INDEX IF NOT EXISTS progress_recent ON progress(updated_seq DESC)")
        if "verdict" not in {row[1] for row in self.db.execute("PRAGMA table_info(results)")}:
            self.db.execute("ALTER TABLE results ADD COLUMN verdict TEXT")
            with self.db:
                rows = self.db.execute("SELECT module, episode, record FROM results").fetchall()
                for module, ep, record in rows:
                    self.db.execute("UPDATE results SET verdict=? WHERE module=? AND episode=?",
                                    (index_verdict(json.loads(record), self.policy), module, ep))
        if "findings" not in {row[1] for row in self.db.execute("PRAGMA table_info(results)")}:
            self.db.execute("ALTER TABLE results ADD COLUMN findings INTEGER")
            with self.db:
                rows = self.db.execute("SELECT module, episode, record FROM results").fetchall()
                for module, ep, record in rows:
                    self.db.execute("UPDATE results SET findings=? WHERE module=? AND episode=?",
                                    (index_findings(json.loads(record)), module, ep))

        self._chains = self._load_chains()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ the two blocks (plan 2.0)
    def _load_chains(self) -> dict[str, list[tuple[str, list[str]]]] | None:
        row = self.db.execute("SELECT value FROM meta WHERE key='blocks'").fetchone()
        if row is None:
            return None
        return {block: [(stage, list(mods)) for stage, mods in chain] for block, chain in json.loads(row[0]).items()}

    def set_blocks(self, chains: dict[str, list[tuple[str, list[str]]]]) -> None:
        """A run of plan 2.0: each block's streaming stages in order, with their modules. Every episode then
        has a position in each block and every stage hands every episode on (design doc 17 §3.3)."""
        doc = {block: [[stage, list(mods)] for stage, mods in chain] for block, chain in chains.items()}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('blocks', ?)", (json.dumps(doc),))
        self._chains = self._load_chains()

    @property
    def blocks(self) -> dict[str, list[tuple[str, list[str]]]] | None:
        """The blocks' streaming stages (plan 2.0); None for a run with one chain (the funnel)."""
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

    def reopen_gate(self, stage: str, next_stage: str) -> list[int]:
        if self._chains is not None:                     # plan 2.0 has no gates: nothing was held back
            return []
        episodes = [row[0] for row in self.db.execute(
            "SELECT episode FROM progress WHERE last_stage=? AND reason='gate'", (stage,))]
        self.forward(stage, episodes, next_stage)
        return episodes

    def indexed(self, module: str) -> bool:
        return self.db.execute("SELECT 1 FROM indexed_modules WHERE module=?",
                               (module,)).fetchone() is not None

    def _reserve(self, count: int) -> int:
        """Reserve ordered change numbers within the caller's transaction."""
        self.db.execute("UPDATE clock SET seq=seq+? WHERE id=1", (count,))
        return self.db.execute("SELECT seq FROM clock WHERE id=1").fetchone()[0] - count + 1

    def _row(self, module: str, episode: int, record: dict, default) -> tuple:
        return (module, episode, json.dumps(record, ensure_ascii=False, default=default),
                index_verdict(record, self.policy), index_findings(record))

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

        if self._chains is not None:                     # plan 2.0: every episode goes on
            with self.db:
                self.db.executemany(_PUT, (self._row(module, episode, record, _json_default)
                                           for module, record in records.items()))
                self._place([episode], stage, next_stage, None)
            return
        terminal = any(index_verdict(r, self.policy) in ("fail", "error") for r in records.values())
        destination = "done" if terminal or next_stage == "done" else next_stage
        reason = "gate" if terminal else None
        with self.db:
            self.db.executemany(_PUT, (self._row(module, episode, record, _json_default)
                                       for module, record in records.items()))
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, ?, ?, ?)",
                            (episode, stage, destination, reason, self._reserve(1)))

    def put_result(self, record: dict) -> None:
        from .records import _json_default

        with self.db:
            self.db.execute(_PUT, self._row(record["module"], int(record["episode_index"]), record,
                                            _json_default))

    def missing(self, stage: str, episode: int) -> None:
        """Its source files are gone (D40): left out, in every later stage too."""
        with self.db:
            if self._chains is not None:
                for block in self._chains:
                    first = self._reserve(1)
                    self.db.execute("INSERT OR REPLACE INTO block_progress VALUES (?, ?, ?, 'done', 'missing', ?)",
                                    (episode, block, stage, first))
                return
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, 'done', 'missing', ?)",
                            (episode, stage, self._reserve(1)))

    def forward(self, stage: str, episodes: list[int], next_stage: str) -> None:
        """A whole module failed: the episodes go on without its records (a funnel run: its gate is open
        for every episode in the batch)."""
        if not episodes:
            return
        if self._chains is not None:
            with self.db:
                self._place(list(episodes), stage, next_stage, None)
            return
        with self.db:
            first = self._reserve(len(episodes))
            self.db.executemany("INSERT OR REPLACE INTO progress VALUES (?, ?, ?, NULL, ?)",
                                ((ep, stage, next_stage, first + i)
                                 for i, ep in enumerate(episodes)))

    def seed_progress(self, episodes: list[int], stages: list[tuple[str, list[str]]]) -> None:
        """Recover older runs whose result files predate the episode progress table."""
        existing = self.progress_for(episodes)
        missing = [ep for ep in episodes if ep not in existing]
        if not missing:
            return
        by_stage = []
        for stage, modules in stages:
            available: dict[int, list[str]] = {}
            for module in modules:
                for ep, verdict in self.db.execute(
                    "SELECT episode, verdict FROM results WHERE module=?", (module,)):
                    available.setdefault(ep, []).append(verdict)
            by_stage.append((stage, len(modules), available))
        with self.db:
            for ep in missing:
                for i, (stage, count, available) in enumerate(by_stage):
                    verdicts = available.get(ep, [])
                    if len(verdicts) != count:
                        break
                    terminal = any(v in ("fail", "error") for v in verdicts)
                    next_stage = "done" if terminal or i == len(by_stage) - 1 \
                        else by_stage[i + 1][0]
                    self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, ?, ?, ?)",
                                    (ep, stage, next_stage, "gate" if terminal else None,
                                     self._reserve(1)))
                    if terminal:
                        break

    def next_for(self, episodes: list[int], stage: str) -> list[int]:
        if not episodes:
            return []
        out = []
        for start in range(0, len(episodes), 900):
            chunk = episodes[start:start + 900]
            slots = ",".join("?" for _ in chunk)
            found = self.db.execute(
                f"SELECT episode FROM progress WHERE next_stage=? AND episode IN ({slots})",
                (stage, *chunk)).fetchall()
            out.extend(row[0] for row in found)
        return sorted(out)

    def progress_for(self, episodes: list[int]) -> dict[int, str]:
        out = {}
        for start in range(0, len(episodes), 900):
            chunk = episodes[start:start + 900]
            if not chunk:
                continue
            slots = ",".join("?" for _ in chunk)
            out.update(self.db.execute(
                f"SELECT episode, next_stage FROM progress WHERE episode IN ({slots})",
                chunk).fetchall())
        return out

    def _block_rows(self, sql_tail: str, args: tuple) -> list[dict]:
        """One row per episode of a two-block run: its latest change and the positions of its blocks."""
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
        if self._chains is not None:
            if before is not None:
                return self._block_rows("HAVING seq < ? ORDER BY seq DESC LIMIT ?", (before, limit))
            return self._block_rows("ORDER BY seq DESC LIMIT ?", (limit,))
        sql = "SELECT episode, last_stage, next_stage, reason, updated_seq FROM progress"
        args: tuple = ()
        if before is not None:
            sql += " WHERE updated_seq < ?"
            args = (before,)
        sql += " ORDER BY updated_seq DESC LIMIT ?"
        return [dict(zip(("episode_index", "last_stage", "next_stage", "reason", "updated_seq"), row))
                for row in self.db.execute(sql, (*args, limit))]

    def episode(self, index: int) -> dict | None:
        if self._chains is not None:
            rows = self._block_rows("HAVING episode = ?", (index,))
            return rows[0] if rows else None
        row = self.db.execute(
            "SELECT episode, last_stage, next_stage, reason, updated_seq FROM progress WHERE episode=?",
            (index,)).fetchone()
        if row is None:
            return None
        return dict(zip(("episode_index", "last_stage", "next_stage", "reason", "updated_seq"), row))

    def episode_records(self, index: int) -> dict[str, dict]:
        return {module: json.loads(record) for module, record in self.db.execute(
            "SELECT module, record FROM results WHERE episode=?", (index,))}

    def totals(self) -> dict[str, int]:
        if self._chains is not None:
            total, done = self.db.execute(
                "SELECT count(*), sum(finished) FROM (SELECT episode, sum(next_stage='done')=? AS finished "
                "FROM block_progress GROUP BY episode)", (len(self._chains),)).fetchone()
            return {"started": int(total or 0), "finished": int(done or 0)}
        total, done = self.db.execute(
            "SELECT count(*), sum(next_stage='done') FROM progress").fetchone()
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
        """Per module so far: the episodes it judged, the ones it failed on and, of the judged ones, those
        with a finding (the live module cards, design doc 17 §5.3)."""
        if not modules:
            return {}
        slots = ",".join("?" for _ in modules)
        out = {m: {"judged": 0, "error": 0, "flagged": 0} for m in modules}
        for module, total, errors, flagged in self.db.execute(
                "SELECT module, count(*), sum(verdict='error'), sum(findings > 0) "
                f"FROM results WHERE module IN ({slots}) GROUP BY module", tuple(modules)):
            out[module] = {"judged": int(total or 0) - int(errors or 0), "error": int(errors or 0),
                           "flagged": int(flagged or 0)}
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

    def survivors(self, modules: list[str]) -> list[int]:
        """A funnel run: the episodes every one of ``modules`` judged and none stopped (plan 2.0: every one with
        records, :meth:`completed`)."""
        if self._chains is not None:
            return self.completed(modules)
        if not modules:
            return []
        slots = ",".join("?" for _ in modules)
        return [row[0] for row in self.db.execute(
            "SELECT episode FROM results "
            f"WHERE module IN ({slots}) GROUP BY episode "
            "HAVING count(DISTINCT module)=? "
            "AND sum(verdict IN ('fail','error'))=0 "
            "ORDER BY episode", (*modules, len(modules)))]
