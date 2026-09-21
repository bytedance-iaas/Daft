"""慢步骤不许静默:可数的重活必须能报 n/total,且**失败条也计数**。

防的是 2026-08-14 用户实见的那类"像卡死":技能画像跑完之后界面一动不动,
其实是相机体检在逐条解帧(已并进视觉质量那一遍);同一形状的还有人工裁决视频
片段切割、证据帧导出、同步诊断图渲染 —— 都是逐条几秒、总数明确,却一声不吭。

这里钉的是回调契约本身:**跳过/失败的条目也要 tick**。漏掉它们的话进度会停在
9/12 再也不动,那比没有进度更像死机。
"""
from __future__ import annotations

import pytest


def _counter():
    box = {"n": 0}
    return box, lambda: box.__setitem__("n", box["n"] + 1)


@pytest.mark.parametrize("concurrency", [1, 4])
def test_audit_clips_progress_counts_every_episode(tmp_path, concurrency):
    """视频片段:每条 episode 一次 tick —— 解码失败的(这里全是坏路径)也要计。"""
    from curation.export.evidence import write_audit_clips

    box, tick = _counter()
    eids = ["ep000000", "ep000001", "ep000002"]
    videos = {e: {"cam": {"path": str(tmp_path / "不存在.mp4"),
                          "from_ts": 0.0, "to_ts": 1.0}} for e in eids}
    n_ok = write_audit_clips(eids, videos, str(tmp_path), on_progress=tick,
                             concurrency=concurrency)
    assert n_ok == 0                      # 全失败:少一段视频而已,不许抛
    assert box["n"] == len(eids)


def test_audit_clips_concurrency_does_not_change_the_result(tmp_path, monkeypatch):
    """并发只改快慢:片段之间互不影响,写出的段数与串行一致(判定更是一个字不动)。"""
    import curation.adapters.decode as decode_mod
    from curation.export import evidence

    monkeypatch.setattr(decode_mod, "decode_window",
                        lambda path, a, b, **kw: ([object()], None))
    monkeypatch.setattr(evidence, "_encode_mp4",
                        lambda frames, path, fps: open(path, "wb").write(b"mp4"))
    eids = [f"ep{i:06d}" for i in range(6)]
    videos = {e: {"observation.images.cam_a": {"path": "x", "from_ts": 0.0,
                                               "to_ts": 1.0}} for e in eids}
    n_serial = evidence.write_audit_clips(eids, videos, str(tmp_path / "串行"),
                                          concurrency=1)
    n_par = evidence.write_audit_clips(eids, videos, str(tmp_path / "并发"),
                                       concurrency=4)
    assert n_serial == n_par == 6
    for sub in ("串行", "并发"):
        assert len(list((tmp_path / sub / "details" / "audit_clips").iterdir())) == 6


def test_evidence_progress_counts_skipped_episodes(tmp_path):
    """证据帧:没视频/没 probe 帧的条目一样 tick,进度才走得到 100%。"""
    from curation.export.evidence import render_task_evidence

    box, tick = _counter()
    per_episode = {
        # 被拒 → 入选,但没给视频指针(跳过)
        "ep000000": {"checks": {"task_success": {"passed": False, "detail": "{}"}}},
        "ep000001": {"checks": {"task_success": {"passed": False, "detail": "{}"}}},
        "ep000002": {"checks": {"task_success": {"passed": True, "detail": "{}"}}},
    }
    out = render_task_evidence(per_episode, {}, str(tmp_path), interval=0.5,
                               max_side=224, on_progress=tick,
                               decode_fn=lambda *a, **k: ([], None))
    assert out == {}
    assert box["n"] == 2                   # 只有被拒的两条入选,通过的那条不该进来


def test_sync_plot_progress_counts_failed_renders(tmp_path):
    """同步诊断图:单张画崩不拖垮整批,但那一张也要计数(否则进度卡在半截)。"""
    pytest.importorskip("matplotlib")
    from curation.export.sync_plots import render_sync_plots

    box, tick = _counter()
    rows = [("ep000000", "这不是 json"), ("ep000001", "{}")]
    assert render_sync_plots(rows, str(tmp_path), on_progress=tick) == []
    assert box["n"] == 2
