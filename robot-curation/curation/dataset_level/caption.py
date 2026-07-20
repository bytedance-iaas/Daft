"""M7 第一步:VLM 逐条 caption(整条管线唯一"看视频"的环节)。

captioner 注入式(与 M4c 同哲学:模型只在 YAML 一处;测试注入假函数)。
下游(taxonomy/audit/profile)只处理这些句子,不再碰视频。
"""
from __future__ import annotations

from typing import Callable

import numpy as np

CAPTION_PROMPT = (
    "These frames are from ONE robot episode in temporal order. Describe the task "
    "in ONE short imperative phrase (e.g. 'put the cup in the sink'). Answer ONLY the phrase.")

# captioner(frames: list[np.ndarray]) -> str
Captioner = Callable[[list], str]


def caption_episodes(rows: list[dict], captioner: Captioner,
                     n_frames: int = 8, max_side: int = 448,
                     precomputed: dict | None = None) -> list[str]:
    """每条 episode:解码→均匀 n_frames 帧→captioner→一句话。失败条给空串(不崩批)。

    precomputed: {episode_id: caption} 缓存(漏斗前为无标注条目生成过的,不重复调用)。
    """
    from ..adapters.decode import decode_window

    caps: list[str] = []
    for r in rows:
        if precomputed and r.get("episode_id") in precomputed:
            caps.append(precomputed[r["episode_id"]])
            continue
        try:
            cam = sorted(r["video"])[0]
            v = r["video"][cam]
            frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"], max_side=max_side)
            idx = np.unique(np.linspace(0, len(frames) - 1, min(n_frames, len(frames)), dtype=int))
            caps.append(str(captioner([frames[i] for i in idx])).strip().strip('."'))
        except Exception:  # noqa: BLE001  单条失败不拖垮整批,空串=未获 caption
            caps.append("")
    return caps


def make_vlm_captioner(endpoint: str, model: str, timeout_s: float = 600.0,
                       api_key_env: str | None = None) -> Captioner:
    """openai 兼容端点 → captioner(生产用;模型/端点来自 YAML)。"""
    import requests

    from ..adapters.vlm_client import _frame_to_data_uri, auth_headers, strip_reasoning

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def captioner(frames: list) -> str:
        content = [{"type": "text", "text": CAPTION_PROMPT}]
        for f in frames:
            content.append({"type": "image_url",
                            "image_url": {"url": _frame_to_data_uri(np.asarray(f))}})
        r = requests.post(url, json={"model": model, "temperature": 0.0, "max_tokens": 512,
                                     "messages": [{"role": "user", "content": content}]},
                          headers=headers, timeout=timeout_s)
        r.raise_for_status()
        return strip_reasoning(r.json()["choices"][0]["message"]["content"])

    return captioner
