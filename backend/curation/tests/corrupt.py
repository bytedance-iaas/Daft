"""造脏数据注入器(P3.0,先造武器再打靶;思路参考 scorer corrupt.py,重写为 Episode 行级)。

设计:输入干净 episode 行(M1 read_lerobot_rows 的 dict),输出 (病灶行, 真值标签)。
- 注入在内存行/帧数组层面做,不改磁盘 → 测检查函数零 IO;
- 注入后行仍须通过 M1 validate(格式合法,内容有病)——这是 P3.0 的验收之一;
- 真值标签记录"注错在哪"(关节/帧号/幅度),验收时对照检查函数的 detail 定位。

帧级注入器(blur/black)单独作用于帧数组(M4a 的输入是帧,不是行)。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class Injection:
    """真值标签:注了什么病灶,应被哪个检查抓住。"""

    kind: str
    should_be_caught_by: str            # 语义化检查名(visual/motion/kinematics/sync/task)
    params: dict[str, Any] = field(default_factory=dict)


def _copy_row(row: dict) -> dict:
    out = dict(row)
    for k in ("action", "proprio_state", "timestamps"):
        if row.get(k) is not None:
            out[k] = np.array(row[k], copy=True)
    out["video"] = copy.deepcopy(row.get("video") or {})
    return out


# ---------- 动作/时序注入(行级) ----------

def drop_frames(row: dict, start: int, n: int) -> tuple[dict, Injection]:
    """丢帧:action/proprio/timestamps 同步删掉 [start, start+n) → 时间戳出现空洞(测同步 L0)。"""
    out = _copy_row(row)
    keep = np.ones(len(out["action"]), dtype=bool)
    keep[start:start + n] = False
    for k in ("action", "proprio_state", "timestamps"):
        if out.get(k) is not None:
            out[k] = out[k][keep]
    return out, Injection("drop_frames", "video_action_sync",
                          {"start": start, "n": n})


def shift_video(row: dict, shift_frames: int) -> tuple[dict, Injection]:
    """视频整体错位 N 帧:平移 video 指针的时间窗(v3 合并 mp4 天然支持;测同步 L1)。"""
    out = _copy_row(row)
    dt = shift_frames / float(row["fps"])
    for cam in out["video"]:
        out["video"][cam]["from_ts"] += dt
        out["video"][cam]["to_ts"] += dt
    return out, Injection("shift_video", "video_action_sync",
                          {"shift_frames": shift_frames, "shift_s": dt})


def exceed_limits(row: dict, joint: int, frame: int,
                  limit_value: float, factor: float = 1.5) -> tuple[dict, Injection]:
    """关节超限:action[frame, joint] 设为极限值 × factor(测运动学硬门,应定位到关节+帧)。"""
    out = _copy_row(row)
    out["action"][frame, joint] = limit_value * factor
    return out, Injection("exceed_limits", "kinematics",
                          {"joint": joint, "frame": frame, "value": float(limit_value * factor)})


def add_spike(row: dict, frame: int, magnitude: float = 10.0) -> tuple[dict, Injection]:
    """加速度尖刺:单帧突跳(测运动质量 spike 维)。作用于 action 与 proprio(实际也跳)。"""
    out = _copy_row(row)
    scale = float(np.abs(np.diff(out["action"], axis=0)).mean() + 1e-6)
    for k in ("action", "proprio_state"):
        if out.get(k) is not None:
            out[k][frame] = out[k][frame] + magnitude * scale
    return out, Injection("add_spike", "motion_quality",
                          {"frame": frame, "magnitude": magnitude})


def stuck_joint(row: dict, joint: int, from_frame: int = 0) -> tuple[dict, Injection]:
    """卡死执行器:指令继续动、**实际(proprio)冻结**(物理意义上的 dead actuator)。

    ⚠️ 不能把 action 也冻掉——真数据里"指令恒值的有意保持"(如 aloha 单臂持物 7 秒)
    是正常行为,与卡死不可区分;卡死的可检测特征 = cmd 在动而 achieved 不响应。
    """
    out = _copy_row(row)
    if out.get("proprio_state") is None:
        raise ValueError("stuck_joint 注入需要 proprio_state(卡死=实际不响应指令)")
    out["proprio_state"][from_frame:, joint] = out["proprio_state"][from_frame, joint]
    return out, Injection("stuck_joint", "motion_quality",
                          {"joint": joint, "from_frame": from_frame})


def saturate_actuator(row: dict, joint: int, offset_scale: float = 30.0) -> tuple[dict, Injection]:
    """执行器饱和:command 与 achieved 持续大偏差(action 加恒定偏置,proprio 不变)。"""
    out = _copy_row(row)
    scale = float(np.abs(np.diff(out["action"], axis=0)).mean() + 1e-6)
    out["action"][:, joint] = out["action"][:, joint] + offset_scale * scale
    return out, Injection("saturate_actuator", "motion_quality",
                          {"joint": joint, "offset": offset_scale * scale})


def truncate(row: dict, keep_fraction: float = 0.5) -> tuple[dict, Injection]:
    """截断:砍掉后半段 → 任务必然未完成(测任务成败判定)。"""
    out = _copy_row(row)
    n = max(2, int(len(out["action"]) * keep_fraction))
    for k in ("action", "proprio_state", "timestamps"):
        if out.get(k) is not None:
            out[k] = out[k][:n]
    if out.get("video"):
        for cam in out["video"]:
            v = out["video"][cam]
            v["to_ts"] = v["from_ts"] + n / float(row["fps"])
    return out, Injection("truncate", "task_success", {"kept": n})


def duplicate(rows: list[dict], idx: int, new_id: str | None = None) -> tuple[list[dict], Injection]:
    """完全复制一条(测精确去重)。"""
    dup = _copy_row(rows[idx])
    dup["episode_id"] = new_id or f"{rows[idx]['episode_id']}_dup"
    return [*rows, dup], Injection("duplicate", "dedup",
                                   {"source": rows[idx]["episode_id"]})


# ---------- 帧级注入(M4a 用) ----------

def blur_frames(frames: list[np.ndarray], kernel: int = 21) -> tuple[list[np.ndarray], Injection]:
    """高斯糊(kernel 越大越糊;scorer 同款思路)。"""
    import cv2

    k = kernel if kernel % 2 == 1 else kernel + 1
    out = [cv2.GaussianBlur(f, (k, k), 0) for f in frames]
    return out, Injection("blur_frames", "visual_quality", {"kernel": k})


def black_frames(frames: list[np.ndarray], start: int = 0, n: int | None = None
                 ) -> tuple[list[np.ndarray], Injection]:
    """全黑帧段(相机断流/遮挡)。"""
    out = [f.copy() for f in frames]
    n = len(out) - start if n is None else n
    for i in range(start, min(start + n, len(out))):
        out[i][:] = 0
    return out, Injection("black_frames", "visual_quality", {"start": start, "n": n})


def overexpose_frames(frames: list[np.ndarray], delta: int = 150
                      ) -> tuple[list[np.ndarray], Injection]:
    """过曝(整体提亮裁顶)。"""
    out = [np.clip(f.astype(np.int16) + delta, 0, 255).astype(np.uint8) for f in frames]
    return out, Injection("overexpose_frames", "visual_quality", {"delta": delta})
