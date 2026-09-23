"""SQLite index and durable episode handoff for the Daemon funnel.

The public check result files remain JSONL. This private database is the resume
source for a pipelined run: a stage commits its records and next destination in
one transaction after the corresponding result lines have been fsynced.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def state_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / ".orchestr" / "episodes.sqlite3"


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
                                    (json.loads(record)["verdict"], module, ep))

    def close(self) -> None:
        self.db.close()

    def failed_stages(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT stage, message FROM failed_stages"))

    def fail_stage(self, stage: str, message: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO failed_stages VALUES (?, ?)",
                            (stage, message))

    def reopen_gate(self, stage: str, next_stage: str) -> list[int]:
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
                self.db.executemany("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)",
                                    ((module, ep, json.dumps(rec, ensure_ascii=False,
                                                              default=_json_default),
                                      rec["verdict"])
                                     for ep, rec in existing.items()))
                self.db.execute("INSERT INTO indexed_modules VALUES (?)", (module,))

    def finish(self, stage: str, episode: int, records: dict[str, dict],
               next_stage: str) -> None:
        from .records import _json_default

        terminal = any(r["verdict"] in ("fail", "error") for r in records.values())
        destination = "done" if terminal or next_stage == "done" else next_stage
        reason = "gate" if terminal else None
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)",
                                ((module, episode, json.dumps(record, ensure_ascii=False,
                                                               default=_json_default),
                                  record["verdict"])
                                 for module, record in records.items()))
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, ?, ?, ?)",
                            (episode, stage, destination, reason, self._reserve(1)))

    def put_result(self, record: dict) -> None:
        from .records import _json_default

        with self.db:
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)",
                            (record["module"], int(record["episode_index"]),
                             json.dumps(record, ensure_ascii=False, default=_json_default),
                             record["verdict"]))

    def missing(self, stage: str, episode: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, 'done', 'missing', ?)",
                            (episode, stage, self._reserve(1)))

    def forward(self, stage: str, episodes: list[int], next_stage: str) -> None:
        """A whole module failed; its gate is open for every episode in the batch."""
        if not episodes:
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

    def recent(self, *, before: int | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT episode, last_stage, next_stage, reason, updated_seq FROM progress"
        args: tuple = ()
        if before is not None:
            sql += " WHERE updated_seq < ?"
            args = (before,)
        sql += " ORDER BY updated_seq DESC LIMIT ?"
        return [dict(zip(("episode_index", "last_stage", "next_stage", "reason", "updated_seq"), row))
                for row in self.db.execute(sql, (*args, limit))]

    def episode(self, index: int) -> dict | None:
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
        if not modules:
            return []
        slots = ",".join("?" for _ in modules)
        return [row[0] for row in self.db.execute(
            "SELECT episode FROM results "
            f"WHERE module IN ({slots}) GROUP BY episode "
            "HAVING count(DISTINCT module)=? "
            "AND sum(verdict IN ('fail','error'))=0 "
            "ORDER BY episode", (*modules, len(modules)))]
