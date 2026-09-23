import ast
import copy
import inspect
import json

import daft
import numpy as np
import pytest

from curation.pipeline import funnel as f
from curation.pipeline.config import load_config
from curation.pipeline.streaming import _build_funnel_chain


def _rows():
    return [{'episode_id': f'ep{i:06d}', 'action': np.full((3, 2), i, dtype=np.float32),
             'proprio_state': np.zeros((3, 2), dtype=np.float32),
             'timestamps': np.array([i, i + .1, i + .2]), 'fps': 10.,
             'embodiment_id': 'test', 'video': {'cam': {'path': str(i)}},
             'instruction': str(i)} for i in range(6)]


def _df(rows):
    from curation.ingest.lerobot_reader import rows_to_daft
    return rows_to_daft(rows)


def _norm(df):
    return json.loads(json.dumps(df.to_pydict(), default=lambda x: x.tolist(), sort_keys=True))


def _fake_checks(monkeypatch, path):
    def record(stage, idx, passed=True):
        with open(path, 'a') as fh:
            fh.write(json.dumps([stage, int(idx)]) + chr(10))
        return {'passed': passed, 'score': .8, 'detail': json.dumps({'idx': int(idx)})}
    monkeypatch.setattr(f, 'make_timestamp_check', lambda cfg:
                        lambda ts, fps: record('ts', ts[0], ts[0] != 1))
    monkeypatch.setattr(f, 'make_kinematic_check', lambda cfg, registry:
                        lambda a, *args: record('kin', a[0][0], a[0][0] not in (1, 2)))
    monkeypatch.setattr(f, 'make_motion_check', lambda cfg, registry:
                        lambda a, *args: record('motion', a[0][0], None))
    def frame(video, *args):
        idx = int(video['cam']['path'])
        return {'visual': record('visual', idx, None),
                'sync': record('sync', idx, idx != 3), 'curves': '{}'}
    monkeypatch.setattr(f, 'make_frame_checks', lambda cfg, registry, **kw: frame)
    monkeypatch.setattr(f, 'build_endstate_voter', lambda cfg: None)
    monkeypatch.setattr(f, 'build_arbitration_deps', lambda cfg: None)
    monkeypatch.setattr(f, 'task_check_episode', lambda cfg, reg, deps, video, *args:
                        record('task', int(video['cam']['path'])))


@pytest.mark.parametrize('mode', ['mixed', 'empty', 'all_drop', 'disabled'])
def test_legacy_parity_schema_order_and_call_counts(monkeypatch, tmp_path, mode):
    path = str(tmp_path / 'calls')
    _fake_checks(monkeypatch, path)
    cfg = load_config()
    if mode == 'disabled':
        for check in cfg['checks'].values():
            check['enable'] = False
    rows = _rows()
    source = _df(rows)
    if mode == 'empty':
        source = source.filter(daft.lit(False))
    elif mode == 'all_drop':
        source = _df(rows[1:3])
    before, bs = f.run_funnel(source, cfg, None, lambda *args: [])
    (tmp_path / 'calls').write_text('')
    cfg['pipeline']['optimizations'] = {'streaming_funnel': True, 'dedicated_executor': True}
    after, ats = f.run_funnel(source, cfg, None, lambda *args: [])
    assert str(after.schema()) == str(before.schema())
    assert _norm(after) == _norm(before)
    assert ats == bs
    calls = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    if mode == 'mixed':
        for stage in ('ts', 'kin', 'motion'):
            assert sorted(i for s, i in calls if s == stage) == list(range(6))
        assert sorted(i for s, i in calls if s == 'sync') == [0, 3, 4, 5]
        assert sorted(i for s, i in calls if s == 'task') == [0, 4, 5]
    elif mode in ('empty', 'disabled'):
        assert calls == []
    # Re-reading the returned DataFrame cannot rerun a check.
    after.count_rows()
    after.to_pydict()
    assert len((tmp_path / 'calls').read_text().splitlines()) == len(calls)


def test_builder_has_no_terminal_actions():
    tree = ast.parse(inspect.getsource(_build_funnel_chain))
    forbidden = {'collect', 'count_rows', 'to_pydict', 'show', 'iter_rows'}
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr in forbidden]


def test_all_checks_with_real_video_and_fake_vlm(tmp_path):
    from parity.fixtures import make_mini_lerobot
    from parity.fakevlm import FakeVlm
    from parity.vlm_tape import TapeHooks, read_tape
    from curation.adapters import vlm_client
    from curation.pipeline.run import _try_build_vlm
    from curation.ingest.daft_source import read_lerobot_lazy
    from curation.registry.registry import EmbodimentRegistry
    source = make_mini_lerobot(str(tmp_path / 'input'))
    cfg = load_config()
    cfg['checks']['task_success']['vlm'].update(endpoint='http://fake-vlm.local/v1', model='fake-vlm')
    hooks = TapeHooks('record', tape_out=str(tmp_path / 'tape.jsonl.gz'),
                      transport=FakeVlm('fake-vlm').transport())
    hooks.install(vlm_client)
    try:
        vlm, note = _try_build_vlm(cfg)
        before, bs = f.run_funnel(read_lerobot_lazy(source), cfg, EmbodimentRegistry(), vlm)
        hooks.uninstall()
        _, entries = read_tape(str(tmp_path / 'tape.jsonl.gz'))
        hooks = TapeHooks('replay', replay_entries=entries, sticky_tags=('models',))
        hooks.install(vlm_client)
        cfg['pipeline']['optimizations'] = {'streaming_funnel': True,
                                           'dedicated_executor': True, 'frame_cache': True}
        after, ats = f.run_funnel(read_lerobot_lazy(source), cfg, EmbodimentRegistry(), vlm)
        assert str(after.schema()) == str(before.schema())
        assert _norm(after) == _norm(before)
        assert ats == bs
        assert hooks.store.misses == []
        assert not hooks.store.remaining()
    finally:
        hooks.uninstall()
