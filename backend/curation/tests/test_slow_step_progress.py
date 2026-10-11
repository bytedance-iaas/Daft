"""慢步骤不许静默:可数的重活必须能报 n/total,且**失败条也计数**。

防的是 2026-08-14 用户实见的那类"像卡死":技能画像跑完之后界面一动不动,
其实是相机体检在逐条解帧(已并进视觉质量那一遍);同一形状的还有人工裁决视频
片段切割、证据帧导出、同步诊断图渲染 —— 都是逐条几秒、总数明确,却一声不吭。

这里钉的是回调契约本身:**跳过/失败的条目也要 tick**。漏掉它们的话进度会停在
9/12 再也不动,那比没有进度更像死机。
"""
from __future__ import annotations



def _counter():
    box = {"n": 0}
    return box, lambda: box.__setitem__("n", box["n"] + 1)


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


