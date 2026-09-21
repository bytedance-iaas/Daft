"""task_success 证据帧导出 + 报告 detail 保全测试(2026-07-27 U0 盘点补缺)。

盘点 droid-200 交付发现的三缺口之二在此守护:
① reject.json 里被拒条目只剩"拒绝"二字(detail 在 run.py 摘取时被丢)——
   check_entry 现在"有 detail 就带";
② probe_frames 只有帧号没有图——render_task_evidence 导出期落 JPEG。
"""
from __future__ import annotations

import json
import os

import pytest

import numpy as np

from curation.export.evidence import (flagged_for_evidence, probe_indices,
                                      render_task_evidence)
from curation.pipeline.run import check_entry


# ───────── check_entry:detail 保全 ─────────

def test_check_entry_keeps_detail_on_reject():
    """被拒条目的 VLM 证据必须进报告(旧逻辑只在弃权时保留 → 已修)。"""
    e = check_entry({"passed": False, "score": None,
                     "detail": '{"verdict": "failure", "reason": "末态 0.1"}'})
    assert "failure" in e["detail"]


def test_check_entry_keeps_detail_on_pass_and_abstain():
    assert "detail" in check_entry({"passed": True, "score": None, "detail": '{"voc": 0.9}'})
    assert "detail" in check_entry({"passed": None, "score": None, "detail": '{"reason": "x"}'})


def test_check_entry_drops_empty_placeholder():
    assert "detail" not in check_entry({"passed": True, "score": None, "detail": "{}"})
    assert "detail" not in check_entry({"passed": None, "score": 0.9, "detail": ""})


# ───────── flagged 选择 ─────────

def _pe(ts_passed, undecidable=(), detail='{"probe_frames": [0, 2]}'):
    return {"verdict": "drop" if ts_passed is False else "keep",
            "undecidable": list(undecidable),
            "checks": {"task_success": {"passed": ts_passed, "score": None,
                                        "detail": detail}}}


def test_flagged_selects_rejected_and_undecided_only():
    pes = {"ep_rej": _pe(False), "ep_und": _pe(None, undecidable=["task_success"]),
           "ep_ok": _pe(True), "ep_novlm": {"verdict": "keep", "checks": {}}}
    assert flagged_for_evidence(pes) == ["ep_rej", "ep_und"]
    assert flagged_for_evidence(pes, mode="off") == []
    assert set(flagged_for_evidence(pes, mode="all")) == {"ep_rej", "ep_und", "ep_ok"}


def test_probe_indices_parses_double_encoded_and_garbage():
    assert probe_indices({"detail": '{"probe_frames": [0, 4, 8]}'}) == [0, 4, 8]
    assert probe_indices({"detail": {"probe_frames": [1]}}) == [1]      # 已解开的也认
    assert probe_indices({"detail": "not json"}) == []
    assert probe_indices({}) == []


# ───────── 落盘(桩解码器,不碰真视频)─────────

def _fake_decode(path, f, t, sample_interval_s, max_side):
    frames = [np.full((8, 8, 3), i * 10, dtype=np.uint8) for i in range(5)]
    return frames, np.arange(5) * sample_interval_s


def test_render_writes_jpegs_for_flagged_only(tmp_path):
    pes = {"ep_rej": _pe(False), "ep_ok": _pe(True)}
    vids = {e: {"cam_a": {"path": "x.mp4", "from_ts": 0.0, "to_ts": 2.0}} for e in pes}
    got = render_task_evidence(pes, vids, str(tmp_path), interval=0.5,
                               max_side=448, decode_fn=_fake_decode)
    assert set(got) == {"ep_rej"}
    for rel in got["ep_rej"]:
        p = tmp_path / rel
        assert p.exists() and p.stat().st_size > 0        # 真 JPEG,非空文件
    assert not (tmp_path / "ep_ok").exists()              # 通过条目不建目录


def test_render_skips_out_of_range_and_bad_decode(tmp_path):
    pes = {"ep_oob": _pe(False, detail='{"probe_frames": [0, 99]}'),
           "ep_dead": _pe(False)}

    def _decode(path, *a, **k):
        if path == "dead.mp4":
            raise RuntimeError("decode fail")
        return _fake_decode(path, *a, **k)

    vids = {"ep_oob": {"c": {"path": "x.mp4", "from_ts": 0, "to_ts": 2}},
            "ep_dead": {"c": {"path": "dead.mp4", "from_ts": 0, "to_ts": 2}}}
    got = render_task_evidence(pes, vids, str(tmp_path), 0.5, 448, decode_fn=_decode)
    assert list(got) == ["ep_oob"]
    assert len(got["ep_oob"]) == 1                        # 帧号 99 越界被跳过,0 号存上
    assert "ep_dead" not in got                           # 解码炸了跳过不中断


def test_render_uses_first_camera_sorted(tmp_path):
    """证据与判定同一双眼睛:漏斗取 sorted(keys)[0],这里必须一致。"""
    seen = []

    def _decode(path, *a, **k):
        seen.append(path)
        return _fake_decode(path, *a, **k)

    pes = {"ep": _pe(False)}
    vids = {"ep": {"z_cam": {"path": "z.mp4", "from_ts": 0, "to_ts": 2},
                   "a_cam": {"path": "a.mp4", "from_ts": 0, "to_ts": 2}}}
    render_task_evidence(pes, vids, str(tmp_path), 0.5, 448, decode_fn=_decode)
    assert seen == ["a.mp4"]


def test_render_respects_cap_and_off(tmp_path):
    pes = {f"ep{i:02d}": _pe(False) for i in range(5)}
    vids = {e: {"c": {"path": "x.mp4", "from_ts": 0, "to_ts": 2}} for e in pes}
    got = render_task_evidence(pes, vids, str(tmp_path), 0.5, 448,
                               cap=2, decode_fn=_fake_decode)
    assert len(got) == 2
    assert render_task_evidence(pes, vids, str(tmp_path), 0.5, 448,
                                mode="off", decode_fn=_fake_decode) == {}


def test_audit_clip_encode_faststart(tmp_path):
    """片段编码:能写出可开箱的 mp4,且 moov 在文件头部(faststart——moov 在尾部时
    浏览器 <video> 会无报错地永久转圈,2026-08-03 人工审片工具实锤)。"""
    pytest.importorskip("av")
    import numpy as np

    from curation.export.evidence import _encode_mp4
    frames = [np.full((33, 47, 3), i * 8, dtype=np.uint8) for i in range(12)]  # 奇数宽高
    dst = tmp_path / "c.mp4"
    _encode_mp4(frames, str(dst), fps=4)
    head = dst.read_bytes()[:2048]
    assert b"moov" in head, "moov 不在头部:faststart 失效,浏览器播不了"


def test_write_audit_clips_skips_bad_video(tmp_path):
    """坏视频指针静默跳过(少一路不断链),好条目照写。"""
    pytest.importorskip("av")
    from curation.export.evidence import write_audit_clips
    n = write_audit_clips(["epX"], {"epX": {"cam": {"path": "/no/such.mp4",
                                                    "from_ts": 0, "to_ts": 1}}},
                          str(tmp_path))
    assert n == 0 and not (tmp_path / "details" / "audit_clips").exists()
