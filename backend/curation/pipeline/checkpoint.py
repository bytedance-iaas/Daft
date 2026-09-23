"""Private SQLite checkpoints for standalone local runs."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np

from .config import ConfigError


def _encode(value):
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise TypeError('Object arrays cannot be checkpointed')
        a = np.ascontiguousarray(value)
        return {'__ndarray__': base64.b64encode(a.tobytes()).decode(),
                'dtype': a.dtype.str, 'shape': list(a.shape)}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {'__ndarray__', 'dtype', 'shape'}:
            dtype = np.dtype(value['dtype'])
            if dtype.hasobject:
                raise ValueError('Object arrays are not supported')
            return np.frombuffer(base64.b64decode(value['__ndarray__'], validate=True),
                                 dtype=dtype).reshape(value['shape']).copy()
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _identity(input_dir, cfg, options):
    root = Path(input_dir).resolve()
    if str(input_dir).startswith('tos://') or not root.is_dir():
        raise ConfigError('Checkpoint currently requires an existing local input directory')
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_file():
            st = path.stat()
            files.append((str(path.relative_to(root)), st.st_size, st.st_mtime_ns))
    config = copy.deepcopy(cfg)
    src = Path(__file__).resolve().parents[1]
    code = [(str(p.relative_to(src)), hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(src.rglob('*.py'))]
    return _digest({'format': 2, 'input': str(root), 'files': files,
                    'config': config, 'options': options, 'implementation': code})


class _Checkpoint:
    def __init__(self, run_dir, identity):
        import fcntl
        self.run_dir = str(run_dir)
        self.identity = identity
        self.directory = Path(run_dir) / '.curation-checkpoint'
        self.directory.mkdir(exist_ok=True)
        self._lock = (self.directory / 'lock').open('a')
        self._conn = None
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise ConfigError('Checkpoint is already in use by another run') from None
        self.records = {}
        self.path = self.directory / 'checkpoint.sqlite3'
        try:
            self._conn = sqlite3.connect(self.path)
            self._conn.execute('PRAGMA synchronous=FULL')
            self._conn.execute('CREATE TABLE IF NOT EXISTS metadata '
                               '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self._conn.execute('CREATE TABLE IF NOT EXISTS records '
                               '(kind TEXT NOT NULL, episode_id TEXT NOT NULL, '
                               'record TEXT NOT NULL, checksum TEXT NOT NULL, '
                               'PRIMARY KEY (kind, episode_id))')
            row = self._conn.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if row is None:
                if self._conn.execute('SELECT 1 FROM records LIMIT 1').fetchone():
                    raise ConfigError('Checkpoint identity is missing')
                self._conn.execute("INSERT INTO metadata (key, value) VALUES ('identity', ?)",
                                   (identity,))
                self._conn.commit()
            elif row[0] != identity:
                raise ConfigError('Checkpoint identity does not match')
            self._load()
        except BaseException:
            self.close()
            raise

    def _load(self):
        for kind, episode_id, raw, checksum in self._conn.execute(
                'SELECT kind, episode_id, record, checksum FROM records'):
            try:
                encoded = json.loads(raw)
                payload = {'identity': self.identity, 'kind': kind,
                           'episode_id': episode_id, 'record': encoded}
                if checksum != _digest(payload):
                    raise ValueError('Invalid checksum')
                self.records[(kind, episode_id)] = _decode(encoded)
            except (ValueError, TypeError, KeyError) as exc:
                raise ConfigError(f'Checkpoint record is invalid: {kind}/{episode_id}') from exc

    def append(self, kind, episode_id, record):
        payload = {'identity': self.identity, 'kind': kind, 'episode_id': episode_id,
                   'record': _encode(record)}
        raw = json.dumps(payload['record'], ensure_ascii=False, separators=(',', ':'))
        self._conn.execute('INSERT INTO records (kind, episode_id, record, checksum) '
                           'VALUES (?, ?, ?, ?) ON CONFLICT(kind, episode_id) DO UPDATE SET '
                           'record=excluded.record, checksum=excluded.checksum',
                           (kind, episode_id, raw, _digest(payload)))
        self._conn.commit()
        self.records[(kind, episode_id)] = record

    def validate_runtime(self, cfg, vlm_available, vlm_ready):
        config = copy.deepcopy(cfg)
        runtime = {'config': _digest(config), 'vlm_available': vlm_available,
                   'vlm_ready': vlm_ready}
        key = ('runtime', '')
        if key in self.records and self.records[key] != runtime:
            raise ConfigError('Checkpoint runtime configuration or VLM availability changed')
        if key not in self.records:
            self.append(*key, runtime)

    def complete(self):
        self._conn.execute("INSERT INTO metadata (key, value) VALUES ('complete', '1') "
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value')
        self._conn.commit()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if not self._lock.closed:
            self._lock.close()


def _matching_runs(delivery, name, identity):
    matches = []
    for path in Path(delivery).glob('*/.curation-checkpoint/checkpoint.sqlite3'):
        if name is not None and path.parent.parent.name != name:
            continue
        try:
            with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as conn:
                metadata = dict(conn.execute('SELECT key, value FROM metadata'))
        except sqlite3.Error:
            continue
        if metadata.get('identity') == identity and metadata.get('complete') != '1':
            matches.append(path.parent.parent)
    return matches


def _open_checkpoint(delivery, name, identity, resume):
    from ..delivery import allocate_run_dir
    if resume is None:
        matches = _matching_runs(delivery, None, identity)
        if matches:
            # When older interrupted runs coexist, continue the most recent one.
            return _Checkpoint(max(matches, key=lambda path: (
                path / '.curation-checkpoint' / 'checkpoint.sqlite3').stat().st_mtime_ns),
                               identity)
        resume = False
    if resume:
        matches = _matching_runs(delivery, name, identity)
        if len(matches) != 1:
            raise ConfigError(f'Resume requires exactly one matching incomplete run; found {len(matches)}')
        return _Checkpoint(matches[0], identity)
    os.makedirs(delivery, exist_ok=True)
    return _Checkpoint(allocate_run_dir(delivery, name), identity)


def _caption_with_checkpoint(rows, captioner, journal, *, n_frames, max_concurrency,
                             on_progress):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..dataset_level.caption import caption_episodes
    values = {}
    pending = []
    for row in rows:
        key = ('caption', row['episode_id'])
        if key in journal.records:
            values[row['episode_id']] = journal.records[key]
            on_progress()
        else:
            pending.append(row)
    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {pool.submit(caption_episodes, [row], captioner, n_frames=n_frames): row
                   for row in pending}
        for future in as_completed(futures):
            eid = futures[future]['episode_id']
            caption = future.result()[0]
            journal.append('caption', eid, caption)
            values[eid] = caption
            on_progress()
    return [values[row['episode_id']] for row in rows]
