"""视觉质量检查:糊/曝光/坏帧(文档中的 M4a)。零语义,软分,CPU。

算法移植自 scorer(拉普拉斯方差),判定逻辑自研重写。
⚠️ 实测教训(2026-07-02,pusht):朴素"亮端占比"区分不了白背景与过曝——干净 pusht 84% 像素
本来就是 255(白桌面渲染)。正确判别量是**信息死亡**:过曝/全黑/断流的共同特征是对比度崩塌
(std→0)或近全削顶(>99% 像素饱和),而白背景场景 std 仍有 20+。
- 子分数全部进 detail(报告用),总分 = 最差子分(短板效应);
- 参考值/阈值是参数不是常数(scorer 的魔法数字不抄),正式默认值 P5.1 校准;
- 冻结帧只在几乎全程冻结时惩罚(机器人数据有真实静止段)。
纯函数,不 import daft;抽帧由 adapters 层完成,本模块只吃帧数组。
"""
from __future__ import annotations

import numpy as np

from ..contract import CheckResult


def _gray(frame: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) if frame.ndim == 3 else frame


def visual_quality(
    frames: list[np.ndarray],
    *,
    blur_ref_var: float = 100.0,      # 拉普拉斯方差参考值(≥此值算全清晰;<100 经验上开始糊)
    dead_std: float = 3.0,            # 帧内灰度 std 低于此=信息死亡(全黑/全白/断流共同特征)
    clip_frac_ok: float = 0.99,       # 削顶(≥254 或 ≤1)占比超过此值开始扣曝光分
    frozen_diff: float = 0.5,         # 相邻帧平均差低于此=冻结
    frozen_ratio_bad: float = 0.95,   # 冻结帧占比超过此才惩罚(真实数据有静止段)
) -> CheckResult:
    """frames: 抽样帧列表(HxWx3 uint8 或灰度)。返回软分 0-1(最差子分)与逐项 detail。"""
    import cv2

    if not frames:
        return CheckResult(name="visual_quality", score=0.0,
                           detail={"error": "无帧可检"})

    grays = [_gray(f) for f in frames]

    # ① 清晰度:逐帧拉普拉斯方差,取中位数(抗单帧噪声)
    lap_vars = [float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in grays]
    blur_var_median = float(np.median(lap_vars))
    sharpness = min(blur_var_median / blur_ref_var, 1.0)

    # ② 曝光:近全削顶(两端)才扣分——白背景≠过曝,削到没纹理才是
    clip_fracs = [max(float((g >= 254).mean()), float((g <= 1).mean())) for g in grays]
    clip_median = float(np.median(clip_fracs))
    exposure = 1.0 if clip_median <= clip_frac_ok else max(
        0.0, 1.0 - (clip_median - clip_frac_ok) / (1.0 - clip_frac_ok))

    # ③ 完好性:信息死亡帧(std 崩塌:全黑/全白/断流)比例 + 全程冻结
    stds = [float(g.std()) for g in grays]
    dead_ratio = float(np.mean([s < dead_std for s in stds]))
    diffs = [float(np.abs(grays[i + 1].astype(np.int16) - grays[i].astype(np.int16)).mean())
             for i in range(len(grays) - 1)]
    frozen_ratio = float(np.mean([d < frozen_diff for d in diffs])) if diffs else 0.0
    integrity = max(0.0, 1.0 - dead_ratio)
    if frozen_ratio >= frozen_ratio_bad:
        integrity = 0.0

    sub = {"sharpness": round(sharpness, 4), "exposure": round(exposure, 4),
           "integrity": round(integrity, 4)}
    return CheckResult(
        name="visual_quality",
        score=float(min(sub.values())),
        detail={
            **sub,
            "blur_var_median": round(blur_var_median, 2),
            "clip_frac_median": round(clip_median, 4),
            "gray_std_median": round(float(np.median(stds)), 2),
            "dead_ratio": round(dead_ratio, 4),
            "frozen_ratio": round(frozen_ratio, 4),
            "n_frames": len(frames),
        },
    )
