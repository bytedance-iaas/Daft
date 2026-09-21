"""VLM 闲置看门狗(2026-07-16 用户定"折中方案"):忙时保持热灶,闲够时长自动打烊。

背景:32B 模型冷启动 3-5 分钟 → 服务设计为常驻复用;代价是无人用时白占一整卡
141GB(实测挂了 7 天)。折中 = 起服务时旁挂本守护进程:轮询 vLLM 的 /metrics
请求计数,连续 idle_timeout_s 没有新请求 → 杀服务并自行退出。

可独立运行: python -m curation.adapters.vlm_watchdog <endpoint> <server_pid> [idle_s]
核心循环依赖全部可注入(测试用假时钟/假计数器,不起真模型)。
"""
from __future__ import annotations

import os
import re
import signal
import sys
import time


def parse_metrics(text: str) -> float:
    """metrics 文本 → 请求活动计数(成功累计+运行中;纯函数,测试友好)。"""
    total = 0.0
    for pat in (r"vllm:request_success_total\{[^}]*\}\s+([0-9.e+]+)",
                r"vllm:num_requests_running\{[^}]*\}\s+([0-9.e+]+)"):
        for m in re.finditer(pat, text):
            total += float(m.group(1))
    return total


def read_request_counter(endpoint: str, timeout: float = 5.0) -> float | None:
    """vLLM /metrics 里的累计请求数。None=服务无响应(无响应≠闲置,不重置计时)。"""
    import requests

    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        return parse_metrics(requests.get(base + "/metrics", timeout=timeout).text)
    except Exception:  # noqa: BLE001
        return None


def watch(endpoint: str, server_pid: int, idle_timeout_s: float = 7200.0,
          poll_s: float = 60.0, *,
          _now=time.time, _sleep=time.sleep,
          _counter=read_request_counter, _kill=None) -> str:
    """看门狗主循环。返回退出原因(测试断言用)。

    - 计数器变化 → 重置闲置计时;
    - 连续 idle_timeout_s 无变化 → 杀 server_pid(整个进程组)→ 退出;
    - 服务器进程已不在(被人手动杀了)→ 退出,不做多余的事。
    """
    def _default_kill(pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)   # vllm 是进程组,杀组防残留
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass

    kill = _kill or _default_kill
    last_counter = None
    last_change = _now()
    while True:
        try:
            os.kill(server_pid, 0)                        # 只探测存活,不发信号
        except OSError:
            return "server_gone"                          # 已被别人关掉,功成身退
        c = _counter(endpoint)
        now = _now()
        if c is not None and c != last_counter:
            last_counter = c
            last_change = now
        if now - last_change >= idle_timeout_s:
            kill(server_pid)
            return "idle_killed"
        _sleep(poll_s)


def main() -> None:
    endpoint, pid = sys.argv[1], int(sys.argv[2])
    idle = float(sys.argv[3]) if len(sys.argv) > 3 else 7200.0
    poll = float(sys.argv[4]) if len(sys.argv) > 4 else 60.0
    reason = watch(endpoint, pid, idle_timeout_s=idle, poll_s=poll)
    print(f"[vlm_watchdog] 退出: {reason}", flush=True)


if __name__ == "__main__":
    main()
