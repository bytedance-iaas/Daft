"""caption 并发的保序与计数测试(2026-07-22)。

背景:caption 是 M7 的第一步,每条 episode 一次 VLM 调用,原本是纯串行 for 循环,
实测占 M7 的大头(10 条 DROID:caption 46s / M7 共 1.7min)。与漏斗 VLM 段同类
(等网络阻塞),故用同一套 _map_concurrent 并发化。

🔴 **保序是正确性要求,不是性能细节。**
下游 taxonomy.assign / audit_labels / skill_profile_two_level 全部按**下标**把
caption 与 episode 对上。错位一格 → 整份技能画像和标注-画面分歧全错,**而且不报错**,
只会安静地给出一份看起来很合理的错报告。

⚠️ 本文件的测试桩特意**按内容**产出 caption,不是按到达顺序编号。
2026-07-21 漏斗 VLM 段的并发测试就栽在这:桩按到达顺序发号,并发下与输入脱钩,
于是"保序测试"根本测不出乱序(假绿)。这里用 decode 出来的像素值编码行号,
caption 结果只由输入内容决定 —— 乱序就必然被抓到。
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from curation.dataset_level import caption as caption_mod


def _row(i: int) -> dict:
    return {"episode_id": f"ep{i:04d}",
            "video": {"cam0": {"path": f"/fake/{i}.mp4", "from_ts": 0.0, "to_ts": 1.0}}}


@pytest.fixture
def fake_decode(monkeypatch):
    """把 decode_window 换成"按路径里的行号造像素"——帧内容即行号,可被追溯。"""
    def _decode(path, from_ts, to_ts, *, sample_interval_s=None, max_side=None):
        i = int(str(path).split("/")[-1].split(".")[0])
        frames = [np.full((2, 2, 3), i, dtype=np.uint8) for _ in range(12)]
        return frames, np.arange(12, dtype=float)

    monkeypatch.setattr("curation.adapters.decode.decode_window", _decode)
    return _decode


def _content_captioner(delay_jitter: bool = True):
    """caption **只由帧内容决定**;完成顺序被随机延迟打乱。

    延迟用 (7 * i) % 5 这种确定性伪随机:既能把完成顺序搅乱,又不引入 flaky。
    """
    def cap(groups):
        i = int(groups[0][1][0][0, 0, 0])          # 首路首帧的内容=行号
        if delay_jitter:
            time.sleep(((7 * i) % 5) * 0.01)
        return f"task-{i}"
    return cap


def test_order_preserved_under_concurrency(fake_decode):
    """并发结果必须与输入一一对应——错位一格整份画像就废了。"""
    rows = [_row(i) for i in range(24)]
    caps = caption_mod.caption_episodes(rows, _content_captioner(), max_concurrency=8)
    assert caps == [f"task-{i}" for i in range(24)]


def test_concurrent_matches_serial_exactly(fake_decode):
    """并发与串行必须逐字相同——并发是纯性能改动,不许改变输出。"""
    rows = [_row(i) for i in range(16)]
    serial = caption_mod.caption_episodes(rows, _content_captioner(False), max_concurrency=1)
    conc = caption_mod.caption_episodes(rows, _content_captioner(False), max_concurrency=8)
    assert serial == conc


def test_stub_can_actually_detect_permutation(fake_decode):
    """反向验证:桩确实能抓出乱序——否则上面两条是假绿(2026-07-21 的教训)。

    故意用一个**按完成顺序发号**的桩(正是当年出问题的形态),证明
    "结果与输入脱钩"这件事本测试能识别。
    """
    counter = {"n": 0}
    lock = threading.Lock()

    def completion_order_captioner(groups):
        # ⚠️ 先延迟再发号:号码由**完成顺序**决定,与内容彻底脱钩。
        #   (第一版把发号写在 sleep 之前,线程仍按提交顺序拿号 → 反向验证自己失效了,
        #    正好又演示了一遍"测试桩写法决定了它能不能抓到 bug"。)
        time.sleep(((13 * int(groups[0][1][0][0, 0, 0])) % 7) * 0.01)
        with lock:
            counter["n"] += 1
            return f"task-{counter['n'] - 1}"

    rows = [_row(i) for i in range(24)]
    caps = caption_mod.caption_episodes(rows, completion_order_captioner, max_concurrency=8)
    assert caps != [f"task-{i}" for i in range(24)], (
        "按到达顺序发号的桩竟然产出了正确序列 —— 说明并发根本没生效,"
        "保序测试形同虚设(正是 2026-07-21 假绿的形态)")


def test_progress_called_once_per_row_under_concurrency(fake_decode):
    """并发下 on_progress 仍须每条恰好一次(多线程同时调,回调方自己保证线程安全)。"""
    rows = [_row(i) for i in range(20)]
    lock = threading.Lock()
    seen = {"n": 0}

    def tick():
        with lock:
            seen["n"] += 1

    caption_mod.caption_episodes(rows, _content_captioner(), on_progress=tick,
                                 max_concurrency=8)
    assert seen["n"] == 20


def test_failures_keep_position_and_dont_break_batch(fake_decode):
    """单条失败给空串且**留在原位**——不能把后面的 caption 挤到前一格。"""
    rows = [_row(0), {"episode_id": "bad"}, _row(2)]      # 中间那条没有 video 键
    caps = caption_mod.caption_episodes(rows, _content_captioner(), max_concurrency=4)
    assert caps == ["task-0", "", "task-2"]


def test_precomputed_cache_still_wins_under_concurrency(fake_decode):
    """命中缓存的条不该再调 captioner,且位置不变。"""
    rows = [_row(0), _row(1), _row(2)]
    called = []
    lock = threading.Lock()

    def cap(groups):
        with lock:
            called.append(int(groups[0][1][0][0, 0, 0]))
        return f"task-{int(groups[0][1][0][0, 0, 0])}"

    caps = caption_mod.caption_episodes(rows, cap, precomputed={"ep0001": "cached-1"},
                                        max_concurrency=4)
    assert caps == ["task-0", "cached-1", "task-2"]
    assert sorted(called) == [0, 2], "命中缓存的条不该调 captioner"
