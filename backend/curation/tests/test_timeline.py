"""三态时间线测试(D2,2026-07-28)。装配纯函数 + UI HTML 渲染。"""
from __future__ import annotations

import json

from curation.export.timeline import build_episode_timeline, timeline_totals
from curation.ui.manifest import timeline_html


def test_plain_episode_all_normal():
    segs = build_episode_timeline(10.0)
    assert segs == [{"start_s": 0.0, "end_s": 10.0, "state": "normal"}]


def test_head_tail_idle_and_event_segments():
    ev = [{"start_s": 3.0, "end_s": 4.0, "state": "stuck"},
          {"start_s": 4.0, "end_s": 4.5, "state": "idle"}]
    segs = build_episode_timeline(10.0, idle_head_s=1.0, idle_tail_s=2.0,
                                  event_segments=ev)
    assert segs[0] == {"start_s": 0.0, "end_s": 1.0, "state": "idle"}
    assert {"start_s": 3.0, "end_s": 4.0, "state": "stuck"} in segs
    assert segs[-1] == {"start_s": 8.0, "end_s": 10.0, "state": "idle"}
    # 铺满无缝:相邻段首尾相接
    for a, b in zip(segs, segs[1:]):
        assert a["end_s"] == b["start_s"]
    tot = timeline_totals(segs)
    assert tot["stuck"] == 1.0 and tot["idle"] == 3.5 and tot["normal"] == 5.5


def test_stuck_overrides_idle_overlap():
    """重叠时 stuck(定罪)压过 idle(停手)。"""
    segs = build_episode_timeline(6.0, idle_head_s=4.0,
                                  event_segments=[{"start_s": 2.0, "end_s": 3.0,
                                                   "state": "stuck"}])
    states = {(s["start_s"], s["end_s"]): s["state"] for s in segs}
    assert states[(2.0, 3.0)] == "stuck"
    assert states[(0.0, 2.0)] == "idle" and states[(3.0, 4.0)] == "idle"


def test_edge_cases():
    assert build_episode_timeline(0) == []
    segs = build_episode_timeline(5.0, idle_tail_s=99)      # 越界钳到时长
    assert segs == [{"start_s": 0.0, "end_s": 5.0, "state": "idle"}]


def test_html_render_and_sort():
    tl = {"note": "口径说明", "episodes": {
        "ep_b": {"duration_s": 10.0, "totals": {"stuck": 0, "idle": 1, "normal": 9},
                 "segments": [{"start_s": 0.0, "end_s": 1.0, "state": "idle"},
                              {"start_s": 1.0, "end_s": 10.0, "state": "normal"}]},
        "ep_a": {"duration_s": 10.0, "totals": {"stuck": 2.0, "idle": 0, "normal": 8},
                 "segments": [{"start_s": 0.0, "end_s": 2.0, "state": "stuck"},
                              {"start_s": 2.0, "end_s": 10.0, "state": "normal"}]}}}
    tl["episodes"]["ep_clean"] = {
        "duration_s": 8.0, "totals": {"stuck": 0, "idle": 0, "normal": 8.0},
        "segments": [{"start_s": 0.0, "end_s": 8.0, "state": "normal"}]}
    html = timeline_html(tl)
    assert html.index("ep_a") < html.index("ep_b")          # 默认按 episode 序号
    assert "ep_clean" not in html                            # 默认只列有 stuck/idle 的
    assert "另有 1 条" in html                               # 被筛掉的条数注明
    assert "ep_clean" in timeline_html(tl, show="all")       # 筛选放开全列
    # 排序:按卡顿时长时 stuck 多的顶到最前(ep_a 2.0s > ep_b 1.0s,与序号序同向,
    # 所以再拿一份倒过来的名字来验,免得两种排序看不出差别)
    rev = {"episodes": {"ep_a": tl["episodes"]["ep_b"], "ep_b": tl["episodes"]["ep_a"]}}
    assert timeline_html(rev).index("ep_a") < timeline_html(rev).index("ep_b")
    by_stuck = timeline_html(rev, sort="stuck")
    assert by_stuck.index("ep_b") < by_stuck.index("ep_a")   # 卡顿长的在前
    # 只看 idle:ep_b 有 idle、ep_a 没有
    only_idle = timeline_html(tl, show="idle")
    assert "ep_b" in only_idle and "ep_a" not in only_idle
    only_stuck = timeline_html(tl, show="stuck")
    assert "ep_a" in only_stuck
    assert "#F53F3F" in html and "#FF7D00" in html and "#00B42A" in html   # Arco 三态色
    assert 'title="卡顿(指令在推而不动) 0.0–2.0s"' in html  # 悬停精确起止
    assert ">2</span>" in html and ">1</span>" in html       # 所有分界都标
    assert ">10s</span>" in html                             # 末端带 s 后缀
    dense = {"episodes": {"ep_d": {"duration_s": 10.0,
        "totals": {"stuck": 0.6, "idle": 0, "normal": 9.4},
        "segments": [{"start_s": 0.0, "end_s": 0.3, "state": "stuck"},
                     {"start_s": 0.3, "end_s": 0.6, "state": "idle"},
                     {"start_s": 0.6, "end_s": 10.0, "state": "normal"}]}}}
    dh = timeline_html(dense)
    assert ">0.3</span>" in dh and ">0.6</span>" in dh       # 挤也不丢标
    assert "tl-above" in dh                                  # 挤的标签上移 bar 上方\n    sparse = timeline_html({"episodes": {"ep_s": {"duration_s": 10.0,\n        "totals": {"stuck": 1.0, "idle": 0, "normal": 9.0},\n        "segments": [{"start_s": 0.0, "end_s": 5.0, "state": "normal"},\n                     {"start_s": 5.0, "end_s": 6.0, "state": "stuck"},\n                     {"start_s": 6.0, "end_s": 10.0, "state": "normal"}]}}})\n    assert "tl-above" not in sparse                          # 不挤则全在同一水平线
    assert "卡顿 2.0s" in html                               # 行标签带总量
    assert "没有卡顿时间线数据" in timeline_html({"episodes": {}})  # 老交付优雅降级
    only_clean = {"episodes": {"ep_clean": tl["episodes"]["ep_clean"]}}
    assert "录制卫生良好" in timeline_html(only_clean)       # 全干净的友好提示


def test_tail_gap_extends_previous_state():
    """尾帧无证据区(T 帧只有 T-1 个间隔)延续前一段,不再默认 normal。

    真例 bridge ep167:36 帧@5fps → 时长 7.2s,idle 判到 7.0s,尾巴 0.2s 原先
    画成 normal(彩条末端一小节青绿 + 边界数字 7 和 7.2 挤在一起)。"""
    ev = [{"start_s": 4.8, "end_s": 7.0, "state": "idle"}]
    segs = build_episode_timeline(7.2, event_segments=ev, tail_gap_s=1 / 5)
    assert segs == [{"start_s": 0.0, "end_s": 4.8, "state": "normal"},
                    {"start_s": 4.8, "end_s": 7.2, "state": "idle"}]
    assert timeline_totals(segs)["idle"] == 2.4          # 2.2 + 一帧 0.2
    assert timeline_totals(segs)["normal"] == 4.8
    # 不传 tail_gap_s = 旧行为(默认参数 0,老调用方不受影响)
    assert build_episode_timeline(7.2, event_segments=ev)[-1]["state"] == "normal"


def test_tail_gap_extends_stuck_too():
    """stuck 贴尾同理:延续的是"前一段是什么",不特判态。"""
    segs = build_episode_timeline(6.2, event_segments=[{"start_s": 3.0, "end_s": 6.0,
                                                        "state": "stuck"}],
                                  tail_gap_s=1 / 5)
    assert segs[-1] == {"start_s": 3.0, "end_s": 6.2, "state": "stuck"}
    assert timeline_totals(segs)["stuck"] == 3.2


def test_tail_gap_keeps_real_normal_tail():
    """真实的 normal 尾巴(>1 帧,有帧间隔证据)原样保留——语义边界不能越。"""
    ev = [{"start_s": 2.0, "end_s": 5.0, "state": "idle"}]
    segs = build_episode_timeline(7.2, event_segments=ev, tail_gap_s=1 / 5)
    assert segs[-1] == {"start_s": 5.0, "end_s": 7.2, "state": "normal"}
    assert timeline_totals(segs)["normal"] == 4.2
    # 贴着边界的另一侧:2.2 帧的尾巴仍是真尾巴
    segs2 = build_episode_timeline(5.44, event_segments=[{"start_s": 1.0, "end_s": 5.0,
                                                          "state": "idle"}],
                                   tail_gap_s=1 / 15)
    assert segs2[-1] == {"start_s": 5.0, "end_s": 5.44, "state": "normal"}


def test_tail_gap_single_normal_episode_untouched():
    """整条只有一段 normal:没有"前一段"可延续,原样不动(不许被吃成空)。"""
    assert build_episode_timeline(10.0, tail_gap_s=1 / 5) == [
        {"start_s": 0.0, "end_s": 10.0, "state": "normal"}]
    # 极短片(整条不足一帧宽)也不能被吃掉
    assert build_episode_timeline(0.2, tail_gap_s=1 / 5) == [
        {"start_s": 0.0, "end_s": 0.2, "state": "normal"}]


def test_dataset_note_rendered_only_when_present():
    """数据集注记(2026-07-29):profile 的 extras.note 原样渲染在彩条上方;
    没有注记的数据集(如 droid)一个像素都不占。"""
    eps = {"ep0": {"duration_s": 10.0, "totals": {"stuck": 0, "idle": 1, "normal": 9},
                   "segments": [{"start_s": 0.0, "end_s": 1.0, "state": "idle"},
                                {"start_s": 1.0, "end_s": 10.0, "state": "normal"}]}}
    note = "state 由 action 累加合成,指令-实际无独立信息"
    html = timeline_html({"episodes": eps, "dataset_note": note})
    assert f"数据集注记:{note}" in html
    assert html.index("数据集注记") < html.index("在干活")   # 在图例/彩条之前
    assert "数据集注记" not in timeline_html({"episodes": eps})          # 无注记不占位
    assert "数据集注记" not in timeline_html({"episodes": eps, "dataset_note": ""})
    # 全干净的交付也要带上注记(前提说明与有没有 stuck/idle 无关)
    clean = {"ep0": {"duration_s": 8.0, "totals": {"stuck": 0, "idle": 0, "normal": 8.0},
                     "segments": [{"start_s": 0.0, "end_s": 8.0, "state": "normal"}]}}
    ch = timeline_html({"episodes": clean, "dataset_note": note})
    assert "数据集注记" in ch and "录制卫生良好" in ch
