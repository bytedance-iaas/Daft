"""增广产物 vs 原视频的动作保真度量化(自带实现,不 import 质检系统)。

三项指标(都是"越高越像原视频"):
- flow_r / flow_lag:两段视频逐帧平均光流幅值序列的相关系数,以及互相关峰对应的 lag(帧;0 = 无错位)
- joint_r_orig / joint_r_gen:关节速度(parquet 里 observation.state 差分)与各自光流幅值的相关系数——
  原视频是基线,生成视频掉多少就是动作被改了多少
- motion_iou:逐帧运动区域(光流幅值前 5% 像素)的 IoU 均值——机械臂在不在原来的位置动
"""
from __future__ import annotations

import json
import os

import cv2
import numpy as np

from . import video_ops


def flow_series(path: str, *, size=(320, 240), top_frac: float = 0.05):
    """返回 (逐帧平均光流幅值 [n-1], 逐帧运动掩码 [n-1, h, w] bool)。"""
    prev = None
    mags, masks = [], []
    for _, rgb in video_ops.iter_frames(path):
        g = cv2.cvtColor(cv2.resize(rgb, size, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
        if prev is not None:
            f = cv2.calcOpticalFlowFarneback(prev, g, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            m = np.linalg.norm(f, axis=2)
            mags.append(float(m.mean()))
            thr = np.quantile(m, 1 - top_frac)
            masks.append(m >= max(thr, 0.3))
        prev = g
    return np.array(mags), np.array(masks)


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if a.std() < 1e-9 or b.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def best_lag(a: np.ndarray, b: np.ndarray, max_lag: int = 15) -> tuple[int, float]:
    """b 相对 a 平移多少帧相关最高。"""
    best = (0, -2.0)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            r = pearson(a[lag:], b[: n - lag])
        else:
            r = pearson(a[: n + lag], b[-lag:])
        if r > best[1]:
            best = (lag, r)
    return best


def joint_speed(parquet_path: str, fps: int, col: str = "observation.state") -> np.ndarray:
    import pyarrow.parquet as pq

    t = pq.read_table(parquet_path, columns=[col])
    x = np.stack(t.column(col).to_pylist()).astype(np.float64)
    v = np.linalg.norm(np.diff(x, axis=0), axis=1) * fps
    return v


def evaluate(orig: str, gen: str, parquet_path: str | None, fps: int) -> dict:
    mo, mask_o = flow_series(orig)
    mg, mask_g = flow_series(gen)
    n = min(len(mo), len(mg))
    lag, r_at_lag = best_lag(mo, mg)
    inter = (mask_o[:n] & mask_g[:n]).sum(axis=(1, 2))
    union = (mask_o[:n] | mask_g[:n]).sum(axis=(1, 2))
    iou_all = np.where(union > 0, inter / np.maximum(union, 1), 1.0)
    # 只在原视频真有动作的帧上算(静止帧的"运动区域"是噪声,IoU 没意义)
    active = mo[:n] > np.median(mo[:n])
    iou = iou_all[active] if active.any() else iou_all
    out = {
        "frames_orig": len(mo) + 1, "frames_gen": len(mg) + 1,
        "flow_r": pearson(mo, mg), "flow_lag_frames": lag, "flow_r_at_lag": r_at_lag,
        "motion_iou_mean": float(iou.mean()), "motion_iou_p10": float(np.quantile(iou, 0.1)),
        "motion_iou_frames": int(len(iou)),
        "flow_mean_orig": float(mo.mean()), "flow_mean_gen": float(mg.mean()),
    }
    if parquet_path:
        js = joint_speed(parquet_path, fps)
        lo, ro = best_lag(js, mo)
        lg, rg = best_lag(js, mg)
        out.update({"joint_r_orig": pearson(js, mo), "joint_r_gen": pearson(js, mg),
                    "joint_lag_orig": lo, "joint_lag_gen": lg, "joint_r_orig_at_lag": ro, "joint_r_gen_at_lag": rg})
    return out


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--orig", required=True)
    p.add_argument("--gen", required=True, nargs="+")
    p.add_argument("--parquet")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--out")
    a = p.parse_args(argv)
    res = {}
    for g in a.gen:
        res[os.path.basename(g)] = evaluate(a.orig, g, a.parquet, a.fps)
        print(os.path.basename(g), json.dumps(res[os.path.basename(g)], ensure_ascii=False), flush=True)
    if a.out:
        json.dump(res, open(a.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
