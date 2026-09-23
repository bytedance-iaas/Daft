import pytest

from curation.pipeline.config import ConfigError, apply_overrides, load_config, validate_config


def test_pipeline_has_no_thinking_switch():
    cfg = load_config()
    assert 'optimizations' not in cfg['pipeline']
    validate_config(cfg)


def test_old_engineering_switches_are_rejected():
    with pytest.raises(ConfigError, match='removed'):
        validate_config({'pipeline': {'optimizations': {'frame_cache': False}}})


def test_cli_has_no_legacy_thinking_switch():
    from curation.cli.legacy import build_parser
    parser = build_parser()
    args = parser.parse_args(['run', '--input', '/missing', '--output', '/tmp/unused',
                              '--set', 'pipeline.vlm_episode_concurrency=1'])
    assert not hasattr(args, 'thinking')
    with pytest.raises(SystemExit):
        parser.parse_args(['run', '--input', '/missing', '--output', '/tmp/unused',
                           '--enable', 'frame_cache'])


@pytest.mark.parametrize('concurrency', [0, -1, True, 1.5, '8'])
def test_active_execution_rejects_invalid_concurrency(concurrency):
    with pytest.raises(ConfigError, match='positive integer'):
        validate_config({'pipeline': {'vlm_episode_concurrency': concurrency}})


def test_formal_snapshot_does_not_add_thinking_policy():
    from curation.pipeline.run import _sanitize_config_snapshot
    cfg = load_config()
    snapshot = _sanitize_config_snapshot(cfg)
    assert 'optimizations' not in snapshot['pipeline']
    assert 'thinking' not in snapshot['pipeline']


def test_legacy_thinking_argument_is_ignored_by_chat_api(monkeypatch):
    import requests
    from curation.adapters.vlm_client import make_llm_ask

    sent = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}

    def post(url, **kwargs):
        sent.append((url, kwargs['json']))
        return Response()

    monkeypatch.setattr(requests, 'post', post)
    assert make_llm_ask('https://example.test/v3', 'doubao-seed-2-0-pro-260215', thinking=False)('hello') == 'ok'
    payload = {
        'model': 'doubao-seed-2-0-pro-260215', 'temperature': 0.0, 'max_tokens': 8192,
        'messages': [{'role': 'user', 'content': 'hello'}],
    }
    assert sent == [('https://example.test/v3/chat/completions', payload)]


def test_pipeline_thinking_setting_reaches_visual_request(monkeypatch):
    import numpy as np
    import requests
    from curation.adapters.vlm_client import vlm_completion_from_config

    sent = []

    class Response:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': '50'}}]}

    monkeypatch.setattr(requests, 'post', lambda _url, **kw:
                        (sent.append(kw['json']) or Response()))
    cfg = load_config()
    cfg['checks']['task_success']['vlm']['model'] = 'ark-glm5.2'
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    complete = vlm_completion_from_config(cfg)
    assert complete([('cam', frame)], [[('cam', frame)]], 'pick up the cup') == [0.5]
    assert len(sent) == 1 and 'thinking' not in sent[0]
