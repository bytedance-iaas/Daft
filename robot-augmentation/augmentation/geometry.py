"""几何漂移:原视频 vs 生成视频同一时刻的帧,ORB 特征匹配 + RANSAC 估相似变换(缩放/旋转/平移)。
模型是重渲染而不是贴图,画面常被拉近、广角化或倾斜;这里量化的是"画面整体被怎么变了"。

    python3 -m augmentation.geometry --orig A.mp4 --gen B.mp4 [C.mp4 ...] [--every 60]
输出每个版本:匹配到的帧数、中位缩放(1.0 = 没变)、中位平移(像素)、中位旋转(度)、残差(像素)。
"""
from __future__ import annotations

import argparse
import json
import math
import os

import cv2
import numpy as np

from . import video_ops


def frame_pairs(orig: str, gen: str, every: int):
    a = video_ops.iter_frames(orig)
    b = video_ops.iter_frames(gen)
    for i, ((_, fa), (_, fb)) in enumerate(zip(a, b)):
        if i % every == 0:
            yield i, fa, fb


def similarity(fa: np.ndarray, fb: np.ndarray, orb):
    ga = cv2.cvtColor(fa, cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor(fb, cv2.COLOR_RGB2GRAY)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return None
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    m = sorted(bf.match(da, db), key=lambda x: x.distance)[:300]
    if len(m) < 8:
        return None
    pa = np.float32([ka[x.queryIdx].pt for x in m])
    pb = np.float32([kb[x.trainIdx].pt for x in m])
    M, inl = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or inl is None or inl.sum() < 8:
        return None
    s = math.hypot(M[0, 0], M[0, 1])
    rot = math.degrees(math.atan2(M[0, 1], M[0, 0]))
    tx, ty = float(M[0, 2]), float(M[1, 2])
    proj = (M[:, :2] @ pa.T).T + M[:, 2]
    resid = float(np.median(np.linalg.norm(proj - pb, axis=1)[inl.ravel() == 1]))
    return {"scale": s, "rot_deg": rot, "tx": tx, "ty": ty, "inliers": int(inl.sum()), "resid": resid}


def evaluate(orig: str, gen: str, every: int = 60) -> dict:
    orb = cv2.ORB_create(2000)
    rows = [r for _, fa, fb in frame_pairs(orig, gen, every) if (r := similarity(fa, fb, orb))]
    if not rows:
        return {"frames_matched": 0}
    med = lambda k: float(np.median([r[k] for r in rows]))
    return {"frames_matched": len(rows), "scale_med": med("scale"), "rot_deg_med": med("rot_deg"),
            "shift_px_med": float(np.median([math.hypot(r["tx"], r["ty"]) for r in rows])),
            "resid_px_med": med("resid"), "inliers_med": med("inliers")}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--orig", required=True)
    p.add_argument("--gen", required=True, nargs="+")
    p.add_argument("--every", type=int, default=60)
    p.add_argument("--out")
    a = p.parse_args(argv)
    res = {}
    for g in a.gen:
        res[os.path.basename(g)] = evaluate(a.orig, g, a.every)
        print(os.path.basename(g), json.dumps(res[os.path.basename(g)]), flush=True)
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
