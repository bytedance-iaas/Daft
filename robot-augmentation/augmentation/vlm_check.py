"""语义层脑补检测:把原帧和生成帧左右并排给多模态模型,问右图有没有左图同一位置没有的物体、
有没有左图有而右图没有的物体;明确要求忽略颜色/材质/纹理/光照变化。颜色无关、场景无关。

    python3 -m augmentation.vlm_check --orig src/front.mp4 --gen A.mp4 [B.mp4 ...] [--step 1.0] [--votes 1]
返回 hallucinated / longest_run / run_span / missing / detail。
"""
from __future__ import annotations

import argparse
import json
import os

import cv2
import numpy as np

from . import video_ops
from .vlm import ask_vlm as _ask_vlm, img_part as _img_part

PROMPT = (
    "Left: a frame from an original robot video. Right: the same frame after an AI edit that may change colors, materials, "
    "textures, lighting or the background. The robot pose and every object's position must be identical.\n"
    "{edit_ctx}"
    "Ignore changes of color, material, texture, lighting, shadows and reflections, and ignore surfaces (table top, mat, floor, "
    "countertop, walls, background scenery) — those are not objects. Report only discrete physical OBJECTS "
    "(cubes, tools, containers, people, body parts, animals, extra robot parts):\n"
    "1) new_objects: physical objects visible in the right image that are NOT present at the same place in the left image "
    "(e.g. an extra cube, an object appearing at the gripper tip that is hidden/absent on the left).\n"
    "2) missing_objects: objects present in the left image that are absent in the right image.\n"
    "Output JSON only: {\"new_objects\": [\"...\"], \"missing_objects\": [\"...\"]} with short names; empty lists if none."
)


def pair_image(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    b = video_ops.resize(b, (a.shape[1], a.shape[0]))
    bar = np.full((a.shape[0], 8, 3), 255, np.uint8)
    img = np.concatenate([a, bar, b], axis=1)
    cv2.putText(img, "ORIGINAL", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2, cv2.LINE_AA)
    cv2.putText(img, "EDITED", (a.shape[1] + 16, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2, cv2.LINE_AA)
    return img


def ask(a: np.ndarray, b: np.ndarray, edit: str | None = None) -> dict:
    ctx = (f"The edit instruction was: \"{edit}\". Changes that this instruction asks for are expected and must NOT be reported.\n"
           if edit else "")
    txt = _ask_vlm([_img_part(pair_image(a, b)), {"type": "text", "text": PROMPT.replace("{edit_ctx}", ctx)}], max_tokens=200)
    try:
        j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    except Exception:
        return {"new_objects": [], "missing_objects": [], "raw": txt[:200]}
    return {"new_objects": [str(x) for x in j.get("new_objects", []) or []],
            "missing_objects": [str(x) for x in j.get("missing_objects", []) or []]}


def evaluate(orig: str, gen: str, *, step: float = 1.0, min_run: int = 2, votes: int = 1, edit: str | None = None) -> dict:
    info = video_ops.probe(orig)
    times = [round(k * step, 3) for k in range(int(info["frames"] / info["fps"] / step))]
    fo = video_ops.frames_at(orig, times)
    fg = video_ops.frames_at(gen, times)
    new_flags, miss_flags, detail = [], [], []
    for t, a, b in zip(times, fo, fg):
        rs = [ask(a, b, edit) for _ in range(votes)]
        print(f"    t={t} new={rs[0]['new_objects']} missing={rs[0]['missing_objects']}", flush=True)
        new = sum(bool(r["new_objects"]) for r in rs) * 2 > votes
        miss = sum(bool(r["missing_objects"]) for r in rs) * 2 > votes
        new_flags.append(new); miss_flags.append(miss)
        if new or miss:
            detail.append({"t": t, "new": rs[0]["new_objects"], "missing": rs[0]["missing_objects"]})

    def runs(flags):
        run = best = 0; start = span = None
        for i, f in enumerate(flags):
            run = run + 1 if f else 0
            if f and run == 1: start = times[i]
            if run > best: best, span = run, (start, times[i])
        return best, span
    nb, ns = runs(new_flags); mb, ms = runs(miss_flags)
    return {"frames": len(times), "new_longest_run": nb, "new_span": ns, "missing_longest_run": mb, "missing_span": ms,
            "longest_run": nb, "run_span": ns, "hallucinated": nb >= min_run, "missing": mb >= min_run,
            "flagged_frames": int(sum(new_flags)), "detail": detail[:40]}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--orig", required=True)
    p.add_argument("--gen", required=True, nargs="+")
    p.add_argument("--step", type=float, default=1.0)
    p.add_argument("--votes", type=int, default=1)
    p.add_argument("--edit", help="编辑指令原文;指令要求的变化不算脑补")
    a = p.parse_args(argv)
    for g in a.gen:
        r = evaluate(a.orig, g, step=a.step, votes=a.votes, edit=a.edit)
        print(os.path.basename(g), json.dumps({k: r[k] for k in ("hallucinated", "new_longest_run", "new_span", "missing", "missing_longest_run", "missing_span", "flagged_frames")}), flush=True)
        for d in r["detail"][:8]:
            print("   ", json.dumps(d, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
