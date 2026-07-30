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
                     precomputed: dict | None = None,
                     on_progress=None,
                     max_concurrency: int = 1) -> list[str]:
    """每条 episode:解码→均匀 n_frames 帧→captioner→一句话。失败条给空串(不崩批)。

    precomputed: {episode_id: caption} 缓存(漏斗前为无标注条目生成过的,不重复调用)。
    on_progress: 每条(含命中缓存的)完成后调用一次,无参。给调用方报进度用——
      本模块不自己打印,免得把 CLI 表现层耦合进数据集级逻辑。
    max_concurrency: 同时处理几条。单条几乎全是等 VLM 响应(网络阻塞),并发收益大。
      **1 = 串行**(排障/对照基线)。

    ⚠️ **保序是正确性要求,不是性能细节**:返回的第 i 条必须对应 rows[i]。下游
    taxonomy.assign / audit_labels / skill_profile_two_level 全都按下标把 caption 与
    episode 对上——错位一格,整份技能画像和标注审计就全错,而且**不会报错**,
    只会安静地给出一份看起来合理的错报告。用 _map_concurrent(ThreadPoolExecutor.map
    保序),并由 test_caption_concurrency 用"内容可区分的假 captioner"钉死。
    """
    from ..adapters.decode import decode_window
    from ..adapters.vlm_client import _map_concurrent

    def _one(r: dict) -> str:
        try:
            if precomputed and r.get("episode_id") in precomputed:
                return precomputed[r["episode_id"]]
            cam = sorted(r["video"])[0]
            v = r["video"][cam]
            frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"], max_side=max_side)
            idx = np.unique(np.linspace(0, len(frames) - 1, min(n_frames, len(frames)), dtype=int))
            return str(captioner([frames[i] for i in idx])).strip().strip('."')
        except Exception:  # noqa: BLE001  单条失败不拖垮整批,空串=未获 caption
            return ""
        finally:
            # finally:失败条、命中缓存的条都要计数,否则有失败时进度永远到不了 100%,
            # 看着像卡死。⚠️并发下多线程会同时调它 → 回调方必须自己线程安全
            # (_progress_tick 内部有锁,满足)。
            if on_progress is not None:
                on_progress()

    return list(_map_concurrent(_one, rows, max_concurrency))


def make_vlm_captioner(endpoint: str, model: str, timeout_s: float = 600.0,
                       api_key_env: str | None = None) -> Captioner:
    """openai 兼容端点 → captioner(生产用;模型/端点来自 YAML)。"""
    import requests

    import time as _time

    from ..adapters.vlm_client import (_frame_to_data_uri, auth_headers,
                                       latency_record, strip_reasoning)

    url = endpoint.rstrip("/") + "/chat/completions"
    headers = auth_headers(api_key_env)

    def captioner(frames: list) -> str:
        content = [{"type": "text", "text": CAPTION_PROMPT}]
        for f in frames:
            content.append({"type": "image_url",
                            "image_url": {"url": _frame_to_data_uri(np.asarray(f))}})
        _t = _time.time()
        _ok = False
        try:
            r = requests.post(url, json={"model": model, "temperature": 0.0, "max_tokens": 512,
                                         "messages": [{"role": "user", "content": content}]},
                              headers=headers, timeout=timeout_s)
            _ok = r.ok
        finally:
            latency_record("caption", _time.time() - _t, _ok, started_at=_t)
        r.raise_for_status()
        return strip_reasoning(r.json()["choices"][0]["message"]["content"])

    return captioner
