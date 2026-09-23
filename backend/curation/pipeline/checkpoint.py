"""Private, checksummed JSONL journals for opt-in local v1 runs."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
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
    config.setdefault('pipeline', {}).setdefault('optimizations', {}).pop('resume', None)
    # Missing and explicit false flags are the same execution policy.
    from .optimizations import _flags
    flags = _flags(config)
    flags.pop('resume')
    config['pipeline']['optimizations'] = flags
    src = Path(__file__).resolve().parents[1]
    code = [(str(p.relative_to(src)), hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(src.rglob('*.py'))]
    return _digest({'format': 1, 'input': str(root), 'files': files,
                    'config': config, 'options': options, 'implementation': code})


class _Checkpoint:
    def __init__(self, run_dir, identity):
        import fcntl
        self.run_dir = str(run_dir)
        self.identity = identity
        self.directory = Path(run_dir) / '.curation-checkpoint'
        self.directory.mkdir(exist_ok=True)
        self._lock = (self.directory / 'lock').open('a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise ConfigError('Checkpoint is already in use by another run') from None
        self.records = {}
        self.path = self.directory / 'records.jsonl'
        header = self.directory / 'identity.json'
        try:
            if header.exists():
                if json.loads(header.read_text()).get('identity') != identity:
                    raise ConfigError('Checkpoint identity does not match')
            else:
                with header.open('x') as fh:
                    json.dump({'identity': identity}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
            self._load()
        except BaseException:
            self.close()
            raise

    def _load(self):
        if not self.path.exists():
            return
        valid_end = 0
        with self.path.open('rb') as fh:
            for line in fh:
                if not line.endswith(b'\n'):
                    break
                try:
                    entry = json.loads(line)
                    payload = entry['payload']
                    if entry['sha256'] != _digest(payload) or payload['identity'] != self.identity:
                        raise ValueError('Invalid checkpoint checksum or identity')
                    item = _decode(payload['record'])
                    self.records[(payload['kind'], payload['episode_id'])] = item
                except (ValueError, KeyError, TypeError):
                    break
                valid_end = fh.tell()
        # Discard only the invalid tail, retaining all verified durable records.
        with self.path.open('r+b') as fh:
            fh.truncate(valid_end)
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, kind, episode_id, record):
        payload = {'identity': self.identity, 'kind': kind, 'episode_id': episode_id,
                   'record': _encode(record)}
        entry = {'payload': payload, 'sha256': _digest(payload)}
        with self.path.open('a') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        self.records[(kind, episode_id)] = record

    def validate_runtime(self, cfg, vlm_available, vlm_ready):
        config = copy.deepcopy(cfg)
        config.setdefault('pipeline', {}).setdefault('optimizations', {}).pop('resume', None)
        runtime = {'config': _digest(config), 'vlm_available': vlm_available,
                   'vlm_ready': vlm_ready}
        key = ('runtime', '')
        if key in self.records and self.records[key] != runtime:
            raise ConfigError('Checkpoint runtime configuration or VLM availability changed')
        if key not in self.records:
            self.append(*key, runtime)

    def complete(self):
        with (self.directory / 'complete').open('w') as fh:
            fh.write(self.identity)
            fh.flush()
            os.fsync(fh.fileno())

    def close(self):
        self._lock.close()


def _open_checkpoint(delivery, name, identity, resume):
    from ..delivery import allocate_run_dir
    if resume:
        matches = []
        for header in Path(delivery).glob('*/.curation-checkpoint/identity.json'):
            if name is not None and header.parent.parent.name != name:
                continue
            try:
                matches_identity = json.loads(header.read_text()).get('identity') == identity
            except (ValueError, OSError):
                continue
            if matches_identity and not (header.parent / 'complete').exists():
                matches.append(header.parent.parent)
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
