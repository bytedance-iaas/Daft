"""VLM 闲置看门狗 + 防双开验收(2026-07-16 用户定折中方案)。全假件,不起真模型。"""
from __future__ import annotations

from curation.adapters.vlm_watchdog import parse_metrics, watch

METRICS = """
vllm:num_requests_running{model_name="m"} 0.0
vllm:request_success_total{model_name="m",finished_reason="stop"} 41.0
vllm:request_success_total{model_name="m",finished_reason="length"} 1.0
"""


def test_parse_metrics():
    assert parse_metrics(METRICS) == 42.0
    assert parse_metrics("") == 0.0


def _fake_env(counter_seq, server_alive_steps=10**9):
    """可编程环境:假时钟(每 poll 前进 60s)、按序吐计数、记录 kill。"""
    state = {"t": 0.0, "i": 0, "killed": [], "alive": server_alive_steps}

    def now():
        return state["t"]

    def sleep(s):
        state["t"] += s

    def counter(_ep):
        v = counter_seq[min(state["i"], len(counter_seq) - 1)]
        state["i"] += 1
        return v

    def kill(pid):
        state["killed"].append(pid)

    return state, dict(_now=now, _sleep=sleep, _counter=counter, _kill=kill)


def test_idle_kills_after_timeout():
    """计数恒定(无请求)→ 到点杀服务。"""
    state, env = _fake_env([42.0] * 100)
    reason = watch("http://x/v1", server_pid=99999999, idle_timeout_s=300,
                   poll_s=60, **env)
    # pid 99999999 不存在 → 会先 server_gone;用自己的 pid 测存活路径
    import os
    state, env = _fake_env([42.0] * 100)
    reason = watch("http://x/v1", server_pid=os.getpid(), idle_timeout_s=300,
                   poll_s=60, **env)
    assert reason == "idle_killed"
    assert state["killed"] == [os.getpid()]
    assert state["t"] >= 300


def test_activity_resets_timer():
    """前期有请求(计数递增)→ 计时重置;之后静默才杀。"""
    import os
    seq = [1.0, 2.0, 3.0, 4.0, 5.0] + [5.0] * 100     # 5 次活动后归于沉寂
    state, env = _fake_env(seq)
    reason = watch("http://x/v1", server_pid=os.getpid(), idle_timeout_s=300,
                   poll_s=60, **env)
    assert reason == "idle_killed"
    assert state["t"] >= 4 * 60 + 300                  # 活动期 4 分钟 + 完整闲置期


def test_metrics_unreachable_not_treated_as_idle_reset():
    """服务无响应(None)≠ 有活动:不重置计时,照样到点杀(防僵尸永生)。"""
    import os
    state, env = _fake_env([None] * 100)
    reason = watch("http://x/v1", server_pid=os.getpid(), idle_timeout_s=300,
                   poll_s=60, **env)
    assert reason == "idle_killed"


def test_server_gone_exits_quietly():
    """服务已被人手动杀 → 看门狗功成身退,不乱杀。"""
    state, env = _fake_env([42.0] * 100)
    reason = watch("http://x/v1", server_pid=999999999, idle_timeout_s=300,
                   poll_s=60, **env)
    assert reason == "server_gone"
    assert state["killed"] == []


def test_ensure_vlm_waits_for_loading_process(monkeypatch):
    """防双开:已有同模型进程在加载(端口未活)→ 等待,绝不再起一个。"""
    from curation.adapters import vlm_server as vs
    calls = {"popen": 0}
    alive_after = {"n": 0}

    monkeypatch.setattr(vs, "_serving_pid", lambda m: 12345)
    def fake_alive(ep, m, timeout=3.0):
        alive_after["n"] += 1
        return alive_after["n"] >= 3            # 第三次探测时"加载完成"
    monkeypatch.setattr(vs, "endpoint_alive", fake_alive)
    monkeypatch.setattr(vs.time, "sleep", lambda s: None)
    monkeypatch.setattr(vs.subprocess, "Popen",
                        lambda *a, **k: calls.__setitem__("popen", calls["popen"] + 1))
    ok, note = vs.ensure_vlm("http://localhost:8000/v1", "nvidia/Cosmos-Reason2-32B")
    assert ok and "等到了" in note
    assert calls["popen"] == 0                   # 一个新进程都没起


def test_watchdog_real_process_integration(tmp_path):
    """真进程集成:sleep 冒充服务,真启动看门狗模块(与 ensure_vlm 同款命令),
    闲置 6s 后服务被真杀、看门狗自行退出。不碰 GPU。"""
    import os
    import subprocess
    import sys
    import time

    dummy = subprocess.Popen(["sleep", "600"], start_new_session=True)
    log = tmp_path / "wd.log"
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__import__("curation").__file__)))) or "."
    with open(log, "w") as lf:
        wd = subprocess.Popen(
            [sys.executable, "-m", "curation.adapters.vlm_watchdog",
             "http://127.0.0.1:59999/v1", str(dummy.pid), "6", "2"],
            stdout=lf, stderr=lf, cwd="/data03/hao/curation-project",
            env=dict(os.environ, PYTHONPATH="/data03/hao/curation-project"))
    try:
        wd.wait(timeout=40)
        deadline = time.time() + 10
        while dummy.poll() is None and time.time() < deadline:
            time.sleep(0.5)
        assert dummy.poll() is not None, "闲置超时后假服务仍活着"
        assert "idle_killed" in open(log).read()
    finally:
        for p in (dummy, wd):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
