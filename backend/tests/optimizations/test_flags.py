import pytest

from curation.pipeline.config import ConfigError, apply_overrides, load_config
from curation.pipeline.optimizations import FLAGS, _cli_overrides, _flags, _validate_execution


def test_defaults_and_disable_all():
    cfg = load_config()
    assert not any(_flags(cfg).values())
    cfg['pipeline']['optimizations'] = {'skip_review': True}
    apply_overrides(cfg, _cli_overrides([], ['all']))
    assert not any(_validate_execution(cfg).values())


@pytest.mark.parametrize('on,off', [(['missing'], []), (['all'], []),
                                   (['frame_cache'], ['frame_cache']),
                                   (['frame_cache'], ['all'])])
def test_invalid_cli(on, off):
    with pytest.raises(ConfigError):
        _cli_overrides(on, off)


@pytest.mark.parametrize('values', [{'unknown': False}, {'frame_cache': 'false'},
                                   {'frame_cache': 1}, None])
def test_strict_types(values):
    with pytest.raises(ConfigError):
        _flags({'pipeline': {'optimizations': values}})


def test_cli_overrides_win_without_mutating_inputs():
    cfg = load_config()
    apply_overrides(cfg, ['pipeline.optimizations.frame_cache=true'] +
                    _cli_overrides([], ['frame_cache']))
    assert _flags(cfg)['frame_cache'] is False


@pytest.mark.parametrize('values,match', [({'resume': True}, 'requires'),
    ({'skip_review': True, 'review_merge': True}, 'Conflicting'),
    ({'skip_strong_review': True}, 'unavailable')])
def test_unsupported_and_dependencies(values, match):
    with pytest.raises(ConfigError, match=match):
        _validate_execution({'pipeline': {'optimizations': values}})


def test_cli_rejects_before_creating_output(tmp_path):
    from curation.cli.legacy import main
    out = tmp_path / 'output'
    assert main(['run', '--input', '/missing', '--output', str(out),
                 '--enable', 'skip_strong_review']) == 2
    assert not out.exists()


def test_python_rejects_before_creating_output(tmp_path):
    from curation.pipeline.run import run_pipeline
    with pytest.raises(ConfigError, match='unavailable'):
        run_pipeline(None, '/missing', str(tmp_path / 'out'),
                     set_overrides=['pipeline.optimizations.skip_strong_review=true'])
    assert not (tmp_path / 'out').exists()


def test_v2_rejects_opt_in(monkeypatch):
    from curation.pipeline import optimizations
    monkeypatch.setattr(optimizations, 'IMPLEMENTED', frozenset({'frame_cache'}))
    cfg = {'pipeline': {'optimizations': {'frame_cache': True}}}
    assert _validate_execution(cfg)['frame_cache']
    with pytest.raises(ConfigError, match='unavailable'):
        _validate_execution(cfg, v2=True)


@pytest.mark.parametrize('concurrency', [0, -1, True, 1.5, '8'])
def test_active_execution_rejects_invalid_concurrency(concurrency):
    with pytest.raises(ConfigError, match='positive integer'):
        _validate_execution({'pipeline': {'vlm_episode_concurrency': concurrency,
                                         'optimizations': {'streaming_funnel': True}}})


def test_formal_snapshot_does_not_expose_private_flags():
    from curation.pipeline.run import _sanitize_config_snapshot
    cfg = load_config()
    cfg['pipeline']['optimizations']['streaming_funnel'] = True
    assert 'optimizations' not in _sanitize_config_snapshot(cfg)['pipeline']
    assert cfg['pipeline']['optimizations']['streaming_funnel'] is True
