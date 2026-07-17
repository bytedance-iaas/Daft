"""VLM 服务自动管理(2026-07-09):CLI 满血自动化。

规则:
- 端点已可达(手动起的/上次留驻的)→ 直接复用,不动任何东西;
- 不可达且端点是本机 → 找空闲 GPU 自动拉起 vLLM 并等就绪;
- ⚠️ GPU 家规:有任何 compute 进程的卡 = 已被占用/预约,绝不使用;
- 全机无空闲卡 / 端点非本机 / 启动超时 → 返回失败,调用方降级精简版(报告注明)。
服务启动后留驻(后续运行复用);关闭: pkill -f 'vllm serve'。
"""
from __future__ import annotations

import os
import subprocess
import time

# 容器/云环境用环境变量覆盖;默认值=H200 开发机(云上 VLM=独立服务,本段自动失活)
VLLM_BIN = os.environ.get("CURATION_VLLM_BIN", "/data03/hao/venv/vllm/bin/vllm")
VLLM_LOG = os.environ.get("CURATION_VLLM_LOG", "/data03/hao/.vllm_auto.log")


def endpoint_alive(endpoint: str, model: str, timeout: float = 3.0) -> bool:
    import requests

    try:
        r = requests.get(endpoint.rstrip("/") + "/models", timeout=timeout)
        return model.split("/")[-1] in r.text
    except Exception:  # noqa: BLE001
        return False


def find_idle_gpu(min_free_mb: int = 100_000) -> int | None:
    """空闲 = 无任何 compute 进程 且 显存占用近零。返回卡号,无则 None。"""
    try:
        busy = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15).stdout.strip().splitlines()
        busy_uuids = {u.strip() for u in busy if u.strip()}
        gpus = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout.strip().splitlines()
    except Exception:  # noqa: BLE001
        return None
    for line in gpus:
        idx, uuid, used, total = [x.strip() for x in line.split(",")]
        if uuid in busy_uuids:
            continue                          # 有进程 = 已被预约,家规不碰
        if int(total) - int(used) >= min_free_mb:
            return int(idx)
    return None


def _serving_pid(model: str) -> int | None:
    """已有的 'vllm serve <model>' 主进程 pid(防双开:2026-07-16 实测曾双开
    两个 32B 各占一卡——端点探测超时被误判为'无服务'后又起了一个)。"""
    try:
        out = subprocess.run(["pgrep", "-f", f"vllm serve {model}"],
                             capture_output=True, text=True, timeout=10).stdout
        pids = [int(x) for x in out.split()]
        return min(pids) if pids else None
    except Exception:  # noqa: BLE001
        return None


def ensure_vlm(endpoint: str, model: str, wait_s: int = 1200,
               idle_timeout_s: float = 7200.0) -> tuple[bool, str]:
    """确保 VLM 服务可用。返回 (是否可用, 说明)。"""
    if endpoint_alive(endpoint, model):
        return True, "VLM 服务已在线(复用)"
    if _serving_pid(model) is not None:
        # 进程在、端口还没活 = 正在加载(冷启动 3-5 分钟):等它,绝不双开
        print(f"[curation] 检测到 {model} 服务进程已存在(加载中),等待就绪…", flush=True)
        t0 = time.time()
        while time.time() - t0 < wait_s:
            if endpoint_alive(endpoint, model):
                return True, "VLM 服务已在线(等到了正在加载的已有进程)"
            if _serving_pid(model) is None:
                break                          # 它死了,走正常启动流程
            time.sleep(15)
    host = endpoint.split("//")[-1].split(":")[0].split("/")[0]
    if host not in ("localhost", "127.0.0.1"):
        return False, f"端点 {endpoint} 非本机且不可达,无法代起服务"
    if not os.path.exists(VLLM_BIN):
        return False, f"未找到 vllm({VLLM_BIN})"
    gpu = find_idle_gpu()
    if gpu is None:
        return False, "无空闲 GPU(有进程的卡按规矩不碰)——自动降级精简版"
    port = endpoint.rsplit(":", 1)[-1].split("/")[0]
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(gpu),
               HF_HOME=os.environ.get("HF_HOME", "/data03/hao/.hf_home"),
               PATH=os.path.dirname(VLLM_BIN) + ":" + os.environ.get("PATH", ""))
    with open(VLLM_LOG, "a") as logf:
        proc = subprocess.Popen(
            [VLLM_BIN, "serve", model, "--port", port,
             "--max-model-len", "16384", "--gpu-memory-utilization", "0.92"],
            stdout=logf, stderr=logf, env=env, start_new_session=True)
        # 闲置看门狗:连续 idle_timeout_s 无请求 → 自动关服务释放显存(折中方案:
        # 忙时热灶复用,闲够打烊;曾无人值守挂 7 天白占一整卡)
        import sys
        _repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        subprocess.Popen(
            [sys.executable, "-m", "curation.adapters.vlm_watchdog",
             endpoint, str(proc.pid), str(idle_timeout_s)],
            stdout=logf, stderr=logf, start_new_session=True,
            cwd=_repo, env=dict(env, PYTHONPATH=_repo))
    print(f"[curation] VLM 服务启动中(GPU{gpu},模型加载约 8-12 分钟,日志 {VLLM_LOG})…",
          flush=True)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if endpoint_alive(endpoint, model):
            return True, (f"VLM 服务已自动拉起(GPU{gpu});留驻复用,闲置 "
                          f"{int(idle_timeout_s / 3600)}h 自动关闭;手动关: pkill -f 'vllm serve'")
        time.sleep(15)
        print(f"[curation]   …等待模型加载({int(time.time() - t0)}s)", flush=True)
    return False, f"VLM 启动超时({wait_s}s),详见 {VLLM_LOG}——本次降级精简版"
