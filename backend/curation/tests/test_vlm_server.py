"""VLM 服务自动管理验收:空闲卡识别(家规)/端点复用/--lite 不碰 GPU。"""
from __future__ import annotations

import curation.adapters.vlm_server as S


def test_find_idle_gpu_skips_busy(monkeypatch):
    """有 compute 进程的卡(uuid 在 busy 集)绝不选——家规。"""
    def fake_run(cmd, *a, **k):
        class R:
            pass
        r = R()
        if "compute-apps" in " ".join(cmd):
            r.stdout = "GPU-aaa\n"                     # GPU0 有进程
        else:
            r.stdout = ("0, GPU-aaa, 3000, 141000\n"   # 有进程 → 跳过
                        "1, GPU-bbb, 500, 141000\n"    # 空闲 → 选中
                        "7, GPU-ggg, 90000, 141000\n") # 显存被占 → 跳过
        return r
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S.find_idle_gpu() == 1


def test_find_idle_gpu_none_when_all_busy(monkeypatch):
    def fake_run(cmd, *a, **k):
        class R:
            pass
        r = R()
        if "compute-apps" in " ".join(cmd):
            r.stdout = "GPU-aaa\nGPU-bbb\n"
        else:
            r.stdout = "0, GPU-aaa, 3000, 141000\n1, GPU-bbb, 3000, 141000\n"
        return r
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    assert S.find_idle_gpu() is None


def test_ensure_vlm_reuses_live_endpoint(monkeypatch):
    """端点已在线 → 直接复用,不找 GPU、不起服。"""
    monkeypatch.setattr(S, "endpoint_alive", lambda e, m, **k: True)
    called = {"gpu": False}
    monkeypatch.setattr(S, "find_idle_gpu",
                        lambda **k: called.__setitem__("gpu", True) or 0)
    ok, note = S.ensure_vlm("http://localhost:8000/v1", "x/y")
    assert ok and "复用" in note and called["gpu"] is False


def test_ensure_vlm_remote_endpoint_no_autostart(monkeypatch):
    monkeypatch.setattr(S, "endpoint_alive", lambda e, m, **k: False)
    ok, note = S.ensure_vlm("http://10.0.0.5:8000/v1", "x/y")
    assert not ok and "非本机" in note


def test_ensure_vlm_no_idle_gpu_degrades(monkeypatch):
    monkeypatch.setattr(S, "endpoint_alive", lambda e, m, **k: False)
    monkeypatch.setattr(S, "find_idle_gpu", lambda **k: None)
    monkeypatch.setattr(S.os.path, "exists", lambda p: True)
    ok, note = S.ensure_vlm("http://localhost:8000/v1", "x/y")
    assert not ok and "无空闲 GPU" in note
