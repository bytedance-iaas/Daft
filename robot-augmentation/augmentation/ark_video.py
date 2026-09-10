"""方舟视频生成(Seedance)异步任务客户端。密钥 ARK_API_KEY,端点 ARK_BASE_URL。
文档:https://docs.volcengine.com/docs/82379/1520757(创建任务)/ 2607688(2.5 教程,含输入限制)。"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

BASE = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

MODELS = {
    "2.5": "doubao-seedance-2-5-260628",
    "2.0": "doubao-seedance-2-0-260128",
    "2.0-fast": "doubao-seedance-2-0-fast-260128",
    "2.0-mini": "doubao-seedance-2-0-mini-260615",
    "2.5short": "doubao-seedance-2-5-260628",  # 同一模型,只是切段计划不同(脑补实验用 7-17s 短段省钱)
}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['ARK_API_KEY']}", "Content-Type": "application/json"}


def create_task(
    model: str,
    prompt: str,
    video_urls: list[str],
    *,
    image_urls: list[str] | None = None,
    task_type: str = "edit",
    ratio: str = "adaptive",
    duration: int = -1,
    resolution: str = "480p",
    generate_audio: bool = False,
    seed: int = -1,
    camera_fixed: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 {"ok": bool, "status": int, "body": dict, "request": dict}。不抛异常,错误原样带回。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for u in image_urls or []:
        content.append({"type": "image_url", "image_url": {"url": u}, "role": "reference_image"})
    for u in video_urls:
        content.append({"type": "video_url", "video_url": {"url": u}, "role": "reference_video"})
    body: dict[str, Any] = {
        "model": MODELS.get(model, model),
        "content": content,
        "omni_reference_task_type": task_type,
        "ratio": ratio,
        "duration": duration,
        "resolution": resolution,
        "generate_audio": generate_audio,
        "watermark": False,
        "seed": seed,
    }
    if camera_fixed is not None:
        body["camera_fixed"] = camera_fixed
    if extra:
        body.update(extra)
    r = requests.post(f"{BASE}/contents/generations/tasks", headers=_headers(), json=body, timeout=60)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:2000]}
    # 记录时把 URL 里的签名去掉,免得日志里留长效链接
    redacted = dict(body)
    redacted["content"] = [
        {**c, "video_url": {"url": c["video_url"]["url"].split("?")[0]}} if c.get("type") == "video_url" else c
        for c in content
    ]
    return {"ok": r.status_code == 200, "status": r.status_code, "body": j, "request": redacted}


def get_task(task_id: str) -> dict[str, Any]:
    r = requests.get(f"{BASE}/contents/generations/tasks/{task_id}", headers=_headers(), timeout=60)
    try:
        return r.json()
    except Exception:
        return {"status": "unknown", "raw": r.text[:2000], "http": r.status_code}


def wait_task(task_id: str, *, poll_s: float = 15, timeout_s: float = 3600, log=print) -> dict[str, Any]:
    t0 = time.time()
    last = None
    while True:
        j = get_task(task_id)
        st = j.get("status")
        if st != last:
            log(f"[{task_id}] status={st} +{time.time() - t0:.0f}s")
            last = st
        if st in ("succeeded", "failed", "cancelled", "expired"):
            j["_wall_s"] = time.time() - t0
            return j
        if time.time() - t0 > timeout_s:
            j["_wall_s"] = time.time() - t0
            j["_timeout"] = True
            return j
        time.sleep(poll_s)


def download(url: str, path: str) -> int:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                n += len(chunk)
    return n
