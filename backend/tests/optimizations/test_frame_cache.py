import numpy as np

from curation.pipeline.frame_cache import _FrameCache, _CACHES, _cache_scope


def test_lossless_cache_identity_and_mutation_isolation(monkeypatch, tmp_path):
    from curation.adapters import decode
    video = tmp_path / 'video'
    video.write_bytes(b'v1')
    calls = []
    def decoder(*args, **kwargs):
        calls.append((args, kwargs))
        return [np.arange(12, dtype=np.uint8).reshape(2, 2, 3)], np.array([.5])
    monkeypatch.setattr(decode, 'decode_window', decoder)
    cache = _FrameCache(100)
    first = cache.decode(str(video), 0., 1., max_side=448)
    first[0][0][:] = 0
    second = cache.decode(str(video), 0., 1., max_side=448)
    np.testing.assert_array_equal(second[0][0], np.arange(12).reshape(2, 2, 3))
    assert len(calls) == 1
    cache.decode(str(video), 0., 1., max_side=224)
    assert len(calls) == 2
    video.write_bytes(b'changed')
    cache.decode(str(video), 0., 1., max_side=448)
    assert len(calls) == 3
    assert cache.size <= cache.budget


def test_budget_eviction_remote_bypass_and_scope(monkeypatch, tmp_path):
    from curation.adapters import decode
    calls = []
    monkeypatch.setattr(decode, 'decode_window', lambda *a, **kw:
                        (calls.append(a) or [np.zeros((2, 2, 3), dtype=np.uint8)], np.array([0.])))
    video = tmp_path / 'video'
    video.write_bytes(b'v')
    cache = _FrameCache(20)
    cache.decode(str(video), 0, 1)
    cache.decode(str(video), 1, 2)
    assert len(cache.entries) == 1 and cache.size == 20
    cache.decode(str(video), 0, 1)
    assert len(calls) == 3
    cache.decode('tos://bucket/video', 0, 1)
    cache.decode('tos://bucket/video', 0, 1)
    assert len(calls) == 5
    with _cache_scope() as key:
        assert key in _CACHES
    assert key not in _CACHES
