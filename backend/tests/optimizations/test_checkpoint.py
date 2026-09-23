import copy
from pathlib import Path

import numpy as np
import pytest

from curation.pipeline.checkpoint import _Checkpoint, _identity, _open_checkpoint
from curation.pipeline.config import ConfigError, load_config
from curation.pipeline.streaming import _execute_funnel
from test_streaming import _df, _fake_checks, _norm, _rows


def test_journal_roundtrip_torn_tail_and_kind_isolation(tmp_path):
    journal = _Checkpoint(tmp_path, 'same')
    row = {'action': np.arange(6, dtype=np.float32).reshape(3, 2), 'verdict': None}
    journal.append('caption', 'ep1', '')
    journal.append('episode', 'ep1', row)
    with journal.path.open('ab') as fh:
        fh.write(b'{"broken":')
    journal.close()
    restored = _Checkpoint(tmp_path, 'same')
    assert restored.records[('caption', 'ep1')] == ''
    np.testing.assert_array_equal(restored.records[('episode', 'ep1')]['action'], row['action'])
    restored.append('caption', 'ep2', 'pick cup')
    restored.close()
    again = _Checkpoint(tmp_path, 'same')
    assert again.records[('caption', 'ep2')] == 'pick cup'
    again.close()


def test_resume_selection_and_lock(tmp_path):
    journal = _open_checkpoint(tmp_path, 'run', 'one', False)
    with pytest.raises(ConfigError, match='already in use'):
        _open_checkpoint(tmp_path, 'run', 'one', True)
    journal.close()
    with pytest.raises(ConfigError, match='found 0'):
        _open_checkpoint(tmp_path, None, 'different', True)
    resumed = _open_checkpoint(tmp_path, None, 'one', True)
    assert resumed.run_dir == journal.run_dir
    resumed.complete()
    resumed.close()
    with pytest.raises(ConfigError, match='found 0'):
        _open_checkpoint(tmp_path, None, 'one', True)
    fresh = _open_checkpoint(tmp_path, 'run', 'one', False)
    assert fresh.run_dir != journal.run_dir
    fresh.close()
    extra = _open_checkpoint(tmp_path, 'extra', 'one', False)
    extra.close()
    with pytest.raises(ConfigError, match='found 2'):
        _open_checkpoint(tmp_path, None, 'one', True)


def test_identity_normalizes_resume_but_not_input_or_policy(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'data').write_bytes(b'first')
    cfg = load_config()
    a = _identity(source, cfg, {})
    cfg['pipeline']['optimizations']['resume'] = True
    assert _identity(source, cfg, {}) == a
    cfg['pipeline']['optimizations']['dedicated_executor'] = True
    assert _identity(source, cfg, {}) != a
    cfg['pipeline']['optimizations']['dedicated_executor'] = False
    (source / 'data').write_bytes(b'changed')
    assert _identity(source, cfg, {}) != a


def test_interrupted_funnel_restores_rows_without_rechecking(monkeypatch, tmp_path):
    log = tmp_path / 'calls'
    _fake_checks(monkeypatch, str(log))
    cfg = load_config()
    source = _df(_rows())
    expected, es = _execute_funnel(source, cfg, None, lambda *args: [])
    journal = _Checkpoint(tmp_path, 'test')
    append = journal.append
    def interrupt(kind, eid, record):
        append(kind, eid, record)
        if len(journal.records) == 3:
            raise KeyboardInterrupt()
    monkeypatch.setattr(journal, 'append', interrupt)
    with pytest.raises(KeyboardInterrupt):
        _execute_funnel(source, cfg, None, lambda *args: [], checkpoint=journal)
    journal.close()
    log.write_text('')
    restored = _Checkpoint(tmp_path, 'test')
    result, stats = _execute_funnel(source, cfg, None, lambda *args: [], checkpoint=restored,
                                   order=[r['episode_id'] for r in _rows()])
    assert _norm(result) == _norm(expected)
    assert stats == es
    import json
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert sorted(i for stage, i in calls if stage == 'ts') == [3, 4, 5]
    log.write_text('')
    again, again_stats = _execute_funnel(source, cfg, None, lambda *args: [], checkpoint=restored,
                                       order=[r['episode_id'] for r in _rows()])
    assert _norm(again) == _norm(expected) and again_stats == es
    assert log.read_text() == ''
    restored.close()


def test_caption_checkpoint_skips_completed_including_abstention(monkeypatch, tmp_path):
    from curation.dataset_level import caption
    from curation.pipeline.checkpoint import _caption_with_checkpoint
    calls = []
    def caps(rows, capper, **kwargs):
        calls.append(rows[0]['episode_id'])
        return ['new caption']
    monkeypatch.setattr(caption, 'caption_episodes', caps)
    journal = _Checkpoint(tmp_path, 'test')
    journal.append('caption', 'ep000000', '')
    ticks = []
    result = _caption_with_checkpoint(_rows()[:2], None, journal, n_frames=8,
                                      max_concurrency=2, on_progress=lambda: ticks.append(1))
    assert result == ['', 'new caption']
    assert calls == ['ep000001'] and len(ticks) == 2
    journal.close()


def test_pipeline_checkpoint_matches_legacy_on_real_fixture(tmp_path):
    from parity.fixtures import make_mini_lerobot
    from curation.pipeline.run import run_pipeline
    data = make_mini_lerobot(str(tmp_path / 'input'))
    common = dict(lite=True, report_only=True, only_checks='timestamp_check,motion_quality')
    before = run_pipeline(None, data, str(tmp_path / 'base'), **common)
    after = run_pipeline(None, data, str(tmp_path / 'optimized'), **common, set_overrides=[
        'pipeline.optimizations.streaming_funnel=true',
        'pipeline.optimizations.checkpoint=true'])
    assert before['verdicts'] == after['verdicts']
    assert before['stats'] == after['stats']
    assert list((tmp_path / 'optimized').glob('*/.curation-checkpoint/complete'))


def test_pipeline_resumes_after_downstream_failure(monkeypatch, tmp_path):
    from parity.fixtures import make_mini_lerobot
    from curation.pipeline.run import run_pipeline
    from curation.export import report
    from curation.pipeline import funnel
    data = make_mini_lerobot(str(tmp_path / 'input'))
    sets = ['pipeline.optimizations.streaming_funnel=true',
            'pipeline.optimizations.checkpoint=true']
    common = dict(lite=True, report_only=True, only_checks='timestamp_check')
    original = report.save_report
    def fail(*args, **kwargs):
        raise RuntimeError('interrupted report')
    monkeypatch.setattr(report, 'save_report', fail)
    with pytest.raises(RuntimeError, match='interrupted report'):
        run_pipeline(None, data, str(tmp_path / 'delivery'), **common, set_overrides=sets)
    monkeypatch.setattr(report, 'save_report', original)
    def check_must_not_run(cfg):
        def check(*args):
            raise AssertionError('restored episode was rechecked')
        return check
    monkeypatch.setattr(funnel, 'make_timestamp_check', check_must_not_run)
    result = run_pipeline(None, data, str(tmp_path / 'delivery'), **common,
                          set_overrides=sets + ['pipeline.optimizations.resume=true'])
    assert result['stats']['input'] == 8
    assert len(list((tmp_path / 'delivery').glob('*/.curation-checkpoint'))) == 1
    assert list((tmp_path / 'delivery').glob('*/.curation-checkpoint/complete'))


def test_identity_mismatch_does_not_truncate_valid_journal(tmp_path):
    journal = _Checkpoint(tmp_path, 'original')
    journal.append('caption', 'ep1', 'keep me')
    original = journal.path.read_bytes()
    journal.close()
    with pytest.raises(ConfigError, match='identity'):
        _Checkpoint(tmp_path, 'wrong')
    assert (tmp_path / '.curation-checkpoint/records.jsonl').read_bytes() == original
    reopened = _Checkpoint(tmp_path, 'original')
    assert reopened.records[('caption', 'ep1')] == 'keep me'
    reopened.close()


def test_resume_refuses_changed_vlm_availability(tmp_path):
    journal = _Checkpoint(tmp_path, 'same')
    cfg = load_config()
    journal.validate_runtime(cfg, True, True)
    cfg['pipeline']['optimizations']['resume'] = True
    journal.validate_runtime(cfg, True, True)
    with pytest.raises(ConfigError, match='VLM availability'):
        journal.validate_runtime(cfg, False, False)
    journal.close()
