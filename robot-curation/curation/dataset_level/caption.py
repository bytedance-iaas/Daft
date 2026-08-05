"""M7 第一步:VLM 逐条 caption(整条管线唯一"看视频"的环节)。

captioner 注入式(与 M4c 同哲学:模型只在 YAML 一处;测试注入假函数)。
下游(taxonomy/audit/profile)只处理这些句子,不再碰视频。
"""
from __future__ import annotations

from typing import Callable

import numpy as np

# 多路带标签 caption(2026-08-05,v7 家族哲学平移:分机位分组+标签、信看得清的
# 视角、意图措辞、unclear 弃权出口)。此前只看 sorted()[0] 单机位——目标太远/被挡时
# captioner 被迫编故事且三次重打编得一样(droid ep32"空气炸锅"被写成"咖啡机",
# 审计线 30% 误旗的主要来源;打分层同款病已在 v7 治过)。
# {cam_sections} = 每路一行 "Images i-j: camera X (相机名), in temporal order."
CAPTION_PROMPT = (
    "{cam_sections}"
    "All cameras show the SAME robot episode. Some views may be occluded or too far "
    "away; rely on the views where the manipulated objects are clearly visible.\n"
    "Describe the task the robot is ATTEMPTING in ONE short imperative phrase "
    "(e.g. 'put the cup in the sink'). Describe the attempted goal even if the "
    "attempt fails or the object slips.\n"
    "If NO view shows the manipulation clearly enough to tell what the task is, "
    "answer exactly: unclear\n"
    "Answer ONLY the phrase.")

# captioner(groups: list[tuple[相机名, list[np.ndarray]]]) -> str
# 每组 = 一路相机的时序帧;单相机数据集 = 单元素列表(自动退化,零分支)
Captioner = Callable[[list], str]


def cam_sections_text(groups: list) -> str:
    """分机位段落头:告诉模型哪些图属于哪路(匿名混排会让好视角被稀释,v6.5 复核实锤)。"""
    lines, start = [], 1
    for i, (name, frames) in enumerate(groups):
        end = start + len(frames) - 1
        lines.append(f"Images {start}-{end}: camera {chr(ord('A') + i)} ({name}), "
                     "in temporal order.")
        start = end + 1
    return "\n".join(lines) + "\n"


def caption_episodes(rows: list[dict], captioner: Captioner,
                     n_frames: int = 8, max_side: int = 448,
                     precomputed: dict | None = None,
                     on_progress=None,
                     max_concurrency: int = 1,
                     max_cams: int = 4) -> list[str]:
    """每条 episode:解码→均匀 n_frames 帧→captioner→一句话。失败条给空串(不崩批)。

    precomputed: {episode_id: caption} 缓存(漏斗前为无标注条目生成过的,不重复调用)。
    on_progress: 每条(含命中缓存的)完成后调用一次,无参。给调用方报进度用——
      本模块不自己打印,免得把 CLI 表现层耦合进数据集级逻辑。
    max_concurrency: 同时处理几条。单条几乎全是等 VLM 响应(网络阻塞),并发收益大。
      **1 = 串行**(排障/对照基线)。

    ⚠️ **保序是正确性要求,不是性能细节**:返回的第 i 条必须对应 rows[i]。下游
    taxonomy.assign / audit_labels / skill_profile_two_level 全都按下标把 caption 与
    episode 对上——错位一格,整份技能画像和标注-画面分歧就全错,而且**不会报错**,
    只会安静地给出一份看起来合理的错报告。用 _map_concurrent(ThreadPoolExecutor.map
    保序),并由 test_caption_concurrency 用"内容可区分的假 captioner"钉死。
    """
    from ..adapters.decode import decode_window
    from ..adapters.vlm_client import _map_concurrent

    def _one(r: dict) -> str:
        try:
            if precomputed and r.get("episode_id") in precomputed:
                return precomputed[r["episode_id"]]
            groups = []
            for cam in sorted(r["video"])[:max_cams]:
                v = r["video"][cam]
                try:
                    frames, _ = decode_window(v["path"], v["from_ts"], v["to_ts"],
                                              max_side=max_side)
                except Exception:  # noqa: BLE001  单路解码失败=少一路视角,不中断
                    continue
                if not frames:
                    continue
                idx = np.unique(np.linspace(0, len(frames) - 1,
                                            min(n_frames, len(frames)), dtype=int))
                groups.append((cam.split(".")[-1], [frames[i] for i in idx]))
            if not groups:
                return ""
            cap = str(captioner(groups)).strip().strip('."')
            # unclear = captioner 的诚实弃权(所有视角都看不清,拒绝编故事)。
            # 归一成空串走既有降级:该条按原始标注分组,审计线跳过不比对——
            # 把"被迫幻觉的错 caption"变成"诚实的无 caption"。
            if cap.strip().lower().startswith("unclear"):
                return ""
            return cap
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

    def captioner(groups: list) -> str:
        text = CAPTION_PROMPT.format(cam_sections=cam_sections_text(groups))
        content = [{"type": "text", "text": text}]
        for _name, frames in groups:
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
