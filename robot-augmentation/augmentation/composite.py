"""掩码合成(借 Cosmos mask_path 思想放到 Seedance 之后):保留掩码内用原像素,掩码外用生成像素。
几何已证明像素级锁定,所以逐帧直接叠。边缘羽化避免硬边。

    python3 -m augmentation.composite --orig src/front.mp4 --gen out/x_front.mp4 --geom <dataset>/geom/front \
        --keep "white robot arm,red cube,blue cube,black box" --out out/x_front_composited.mp4 [--feather 4] [--dilate 3]
--keep 里列出要保留原像素的物体;编辑指令要改的物体(比如"方块换金")就别放进 keep。
"""
from __future__ import annotations

import argparse
import json
import os

import cv2
import numpy as np

from . import video_ops


def load_masks_full(geom_dir: str) -> tuple[list[str], np.ndarray]:
    z = np.load(os.path.join(geom_dir, "masks_full.npz"))
    shape = tuple(int(x) for x in z["shape"])
    masks = np.unpackbits(z["masks"], axis=-1)[..., : shape[-1]].astype(bool).reshape(shape)
    names = json.load(open(os.path.join(geom_dir, "objects.json")))["objects"]
    return names, masks


def keep_mask(names: list[str], masks: np.ndarray, keep: list[str], t: int, *, dilate: int, feather: float,
              exclude: list[str] | None = None, exclude_dilate: int = 3) -> np.ndarray:
    """返回 [H, W] float32 权重:1 = 原像素,0 = 生成像素。exclude(编辑目标)的掩码外扩后从保留区里扣掉:
    目标进了容器(盒子)时,容器掩码连内部一起圈,不扣会把目标盖回原样。"""
    idx = [i for i, n in enumerate(names) if n in keep]
    m = np.zeros(masks.shape[2:], np.uint8)
    for i in idx:
        m |= masks[i, t].astype(np.uint8)
    for n in exclude or []:
        if n in names:
            ex = masks[names.index(n), t].astype(np.uint8)
            if exclude_dilate > 0:
                ex = cv2.dilate(ex, np.ones((2 * exclude_dilate + 1, 2 * exclude_dilate + 1), np.uint8))
            m &= (1 - ex)
    if dilate > 0:
        m = cv2.dilate(m, np.ones((2 * dilate + 1, 2 * dilate + 1), np.uint8))
    elif dilate < 0:
        m = cv2.erode(m, np.ones((-2 * dilate + 1, -2 * dilate + 1), np.uint8))
    w = m.astype(np.float32)
    if feather > 0:
        w = cv2.GaussianBlur(w, (0, 0), feather)
    return w


def run(orig: str, gen: str, geom_dir: str, keep: list[str], out: str, *, dilate: int = -1, feather: float = 0.7, fps: int = 30,
        exclude: list[str] | None = None) -> dict:
    names, masks = load_masks_full(geom_dir)
    missing = [k for k in keep if k not in names]
    if missing:
        raise SystemExit(f"几何档案里没有这些物体:{missing};有的是 {names}")
    w = None
    n = 0
    frac = []
    for (_, a), (_, b) in zip(video_ops.iter_frames(orig), video_ops.iter_frames(gen)):
        if n >= masks.shape[1]:
            break
        b = video_ops.resize(b, (a.shape[1], a.shape[0]))
        km = keep_mask(names, masks, keep, n, dilate=dilate, feather=feather, exclude=exclude)
        km = cv2.resize(km, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_LINEAR)[..., None]
        mix = (a.astype(np.float32) * km + b.astype(np.float32) * (1 - km)).round().astype(np.uint8)
        if w is None:
            w = video_ops.Writer(out, fps, a.shape[1], a.shape[0])
        w.write(mix)
        frac.append(float(km.mean()))
        n += 1
    w.close()
    return {"frames": n, "keep": keep, "exclude": exclude or [], "keep_frac_mean": float(np.mean(frac)), "out": out}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--orig", required=True)
    p.add_argument("--gen", required=True)
    p.add_argument("--geom", required=True)
    p.add_argument("--keep", required=True, help="逗号分隔的物体名,须与几何档案 objects 一致")
    p.add_argument("--out", required=True)
    p.add_argument("--dilate", type=int, default=-1, help="保留掩码外扩像素;深色新材质上外扩会带出原底色光晕,默认 0")
    p.add_argument("--feather", type=float, default=0.7)
    a = p.parse_args(argv)
    print(json.dumps(run(a.orig, a.gen, a.geom, [s.strip() for s in a.keep.split(",")], a.out, dilate=a.dilate, feather=a.feather), ensure_ascii=False))


if __name__ == "__main__":
    main()
