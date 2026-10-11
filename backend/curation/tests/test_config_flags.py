"""The pipeline configuration's switches: no engineering switches, no thinking switch."""
import pytest

from curation.pipeline.config import ConfigError, load_config, validate_config


def test_pipeline_has_no_thinking_switch():
    cfg = load_config()
    assert 'optimizations' not in cfg['pipeline']
    validate_config(cfg)


def test_old_engineering_switches_are_rejected():
    with pytest.raises(ConfigError, match='removed'):
        validate_config({'pipeline': {'optimizations': {'frame_cache': False}}})


def test_pipeline_thinking_setting_reaches_visual_request(monkeypatch):
    """The production scorer judges continuous video (design 13); with
    ``pipeline.thinking`` unset, its request carries no ``thinking`` field."""
    import json

    import requests
    from curation.adapters.video_input import VideoClip
    from curation.adapters.vlm_client import vlm_completion_from_config

    sent = []
    answer = {'verdict': 'success', 'task_type': 'transient', 'completion': 0.5,
              'reason': 'the cup is visibly lifted',
              'evidence': [{'camera': 'cam', 'start_s': 0.0, 'end_s': 1.0,
                            'observation': 'the gripper lifts the cup'}],
              # the one judgement answers per camera too (D71)
              'cameras': {'cam': {'verdict': 'success', 'reason': 'the cup is lifted',
                                  'camera_check': {}}}}

    class Response:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': json.dumps(answer)}}]}

    monkeypatch.setattr(requests, 'post', lambda _url, **kw:
                        (sent.append(kw['json']) or Response()))
    cfg = load_config()
    cfg['checks']['task_success']['vlm']['model'] = 'ark-glm5.2'
    assess = vlm_completion_from_config(cfg)
    clip = VideoClip('cam', 'data:video/mp4;base64,YWJj', 'abc', 0, 2, 20, 3)
    assert assess([clip], 'pick up the cup')['completion'] == 0.5
    assert len(sent) == 1 and 'thinking' not in sent[0]
