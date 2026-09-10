"""身份相关的三件事(都吃几何档案的掩码,颜色无关):
  1. choose_cuts:几何感知切段——切点时刻所有目标物体都可见且静止,别切在被夹爪压住的时候
  2. anchor_frame:在已生成的前段里挑一帧"所有目标物体都可见"当身份锚帧(@图片2)
  3. identity_check:相邻两段衔接处,逐目标物体让 VLM 比较材质/颜色是否一致(2/3 票),不一致=该段失败重抽
"""
from __future__ import annotations

import json
import os
import re

import cv2
import numpy as np

from . import video_ops
from .vlm import ask_vlm as _ask_vlm, img_part as _img_part, target_materials  # noqa: F401


def load_small_masks(geom_dir: str):
    z = np.load(os.path.join(geom_dir, "masks.npz"))
    names = [str(n) for n in z["names"]]
    meta = json.load(open(os.path.join(geom_dir, "objects.json")))
    return meta.get("objects", names), z["masks"]  # objects.json 的顺序与 masks 一致


def track_stats(masks: np.ndarray):
    """[n, T] 面积与质心。"""
    n, T = masks.shape[:2]
    area = masks.reshape(n, T, -1).sum(-1)
    cx = np.zeros((n, T)); cy = np.zeros((n, T))
    for i in range(n):
        for t in range(T):
            if area[i, t] > 0:
                ys, xs = np.where(masks[i, t])
                cx[i, t], cy[i, t] = xs.mean(), ys.mean()
    return area, cx, cy


def ok_frames(masks: np.ndarray, names: list[str], targets: list[str], *, fps: int, min_area: int = 40,
              speed_px_s: float = 12.0, window_s: float = 0.5) -> np.ndarray:
    """每帧:所有目标物体都可见(掩码面积 ≥ min_area,在 320x240 小图上)且静止(±window 内质心位移速度小)。"""
    T = masks.shape[1]
    ok = np.ones(T, bool)
    area, cx, cy = track_stats(masks)
    w = int(window_s * fps)
    for tname in targets:
        if tname not in names:
            continue
        i = names.index(tname)
        vis = area[i] >= min_area
        for t in range(T):
            if not vis[t]:
                ok[t] = False
                continue
            lo, hi = max(0, t - w), min(T - 1, t + w)
            if not (vis[lo] and vis[hi]):
                ok[t] = False
                continue
            d = np.hypot(cx[i, hi] - cx[i, lo], cy[i, hi] - cy[i, lo])
            if d / max((hi - lo) / fps, 1e-6) > speed_px_s:
                ok[t] = False
    return ok


def visible_runs(masks: np.ndarray, names: list[str], targets: list[str], *, min_area: int = 40) -> dict:
    """每个目标物体的可见区间列表 [(f0, f1)),按掩码面积。"""
    area = masks.reshape(masks.shape[0], masks.shape[1], -1).sum(-1)
    out = {}
    for tname in targets:
        if tname not in names:
            continue
        vis = area[names.index(tname)] >= min_area
        runs, s = [], None
        for t, v in enumerate(vis):
            if v and s is None:
                s = t
            if (not v) and s is not None:
                runs.append((s, t)); s = None
        if s is not None:
            runs.append((s, len(vis)))
        out[tname] = [r for r in runs if r[1] - r[0] >= 5]
    return out


def choose_cuts_v2(T: int, fps: int, ok: np.ndarray, runs: dict, *, max_seg: float = 28.0, min_seg: float = 4.0) -> list[tuple[float, float]]:
    """规则:①段长在 [min_seg, max_seg];②切点优先落在 ok 时刻(全员可见且静止)或可见区间边界;
    ③任何一段都不能跨越某个目标"消失又出现"(遮挡 20 秒再露面模型会失忆)。
    做法:把所有"消失"与"出现"的时刻当作必切候选,贪心从左往右选满足段长的切点。"""
    dur = T / fps
    events = sorted({r[0] for rs in runs.values() for r in rs} | {r[1] for rs in runs.values() for r in rs})
    events = [e for e in events if min_seg * fps <= e <= (dur - min_seg) * fps]
    spans, t0 = [], 0.0
    while dur - t0 > max_seg or any(_gap_inside(t0 * fps, dur * fps, rs) for rs in runs.values()):
        lo, hi = (t0 + min_seg) * fps, min(t0 + max_seg, dur - min_seg) * fps
        # 段内不许出现"消失后再出现":找本段起点之后第一个"再出现"事件,切点必须 ≤ 它
        must_before = min((r[0] for rs in runs.values() for r in rs if r[0] > t0 * fps + 1 and any(q[1] <= r[0] and q[1] > t0 * fps for q in rs)), default=None)
        if must_before is not None:
            hi = min(hi, must_before)
        cands = [e for e in events if lo <= e <= hi]
        if cands:
            cut = max(cands) / fps  # 事件时刻里最靠后的
        else:
            oks = [t for t in range(int(hi), int(lo) - 1, -1) if ok[t]]
            cut = (oks[0] if oks else int(hi)) / fps
        if cut <= t0 + 1e-6:
            break
        spans.append((round(t0, 3), round(cut, 3)))
        t0 = cut
    spans.append((round(t0, 3), dur))
    return spans


def _gap_inside(f0: float, f1: float, rs: list) -> bool:
    """区间 [f0,f1) 内是否包含某物体"消失又出现"(两个可见区间之间的空档完整落在段内)。"""
    inside = [r for r in rs if r[0] >= f0 and r[1] <= f1]
    return len(inside) >= 2


def choose_cuts(T: int, fps: int, ok: np.ndarray, *, max_seg: float = 28.0, min_seg: float = 4.0) -> list[tuple[float, float]]:
    """贪心:从当前段起点出发,在 [起点+min_seg, 起点+max_seg] 里选最靠后的 ok 时刻切;剩余不足 max_seg 就到尾。
    找不到 ok 时刻就退化为在窗口末端切(等分策略的味道),并把 fallback 记下来。"""
    dur = T / fps
    spans = []
    t0 = 0.0
    while dur - t0 > max_seg:
        lo, hi = int((t0 + min_seg) * fps), int(min(t0 + max_seg, dur - min_seg) * fps)
        cands = [t for t in range(hi, lo - 1, -1) if ok[t]]
        cut = (cands[0] / fps) if cands else (hi / fps)
        spans.append((round(t0, 3), round(cut, 3)))
        t0 = cut
    spans.append((round(t0, 3), dur))
    return spans


def anchor_frame(ok: np.ndarray, f0: int, f1: int) -> int | None:
    """[f0, f1) 里最长 ok 连续段的中点帧;没有就 None。"""
    best = (0, None)
    run = 0
    for t in range(f0, f1):
        run = run + 1 if ok[t] else 0
        if run > best[0]:
            best = (run, t - run // 2)
    return best[1]


def _crop(rgb: np.ndarray, bbox_small, small_size=(320, 240), pad: float = 0.6):
    x1, y1, x2, y2 = bbox_small
    sx, sy = rgb.shape[1] / small_size[0], rgb.shape[0] / small_size[1]
    w, h = (x2 - x1) * sx, (y2 - y1) * sy
    cx, cy = (x1 + x2) / 2 * sx, (y1 + y2) / 2 * sy
    half = max(w, h) * (0.5 + pad)
    X1, Y1 = int(max(0, cx - half)), int(max(0, cy - half))
    X2, Y2 = int(min(rgb.shape[1], cx + half)), int(min(rgb.shape[0], cy + half))
    return rgb[Y1:Y2, X1:X2]


def bbox_small(mask: np.ndarray):
    ys, xs = np.where(mask)
    if len(xs) < 10:
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def identity_check(prev_frame: np.ndarray, next_frame: np.ndarray, masks: np.ndarray, names: list[str], targets: list[str],
                   frame_prev: int, frame_next: int, *, votes: int = 3) -> dict:
    """衔接处逐目标物体比较:两张裁剪图并排,问"是不是同一种材质和颜色"。返回 {name: {"same": bool, "checked": bool}}。"""
    out = {}
    for tname in targets:
        if tname not in names:
            continue
        i = names.index(tname)
        b0, b1 = bbox_small(masks[i, min(frame_prev, masks.shape[1] - 1)]), bbox_small(masks[i, min(frame_next, masks.shape[1] - 1)])
        if b0 is None or b1 is None:
            out[tname] = {"same": True, "checked": False, "why": "not visible at boundary"}
            continue
        a, b = _crop(prev_frame, b0), _crop(next_frame, b1)
        s = max(a.shape[0], b.shape[0], 160)
        a = cv2.resize(a, (int(a.shape[1] * s / a.shape[0]), s)); b = cv2.resize(b, (int(b.shape[1] * s / b.shape[0]), s))
        pair = np.concatenate([a, np.full((s, 6, 3), 255, np.uint8), b], axis=1)
        q = ("Left and right show the same object one second apart in an edited video. Do they have the SAME material and color "
             "(e.g. both gold, or both silver)? Ignore lighting, blur and viewpoint. Answer exactly one word: yes or no.")
        ys = [_ask_vlm([_img_part(pair), {"type": "text", "text": q}], max_tokens=5).strip().lower().startswith("y") for _ in range(votes)]
        out[tname] = {"same": sum(ys) * 2 > votes, "checked": True, "votes": int(sum(ys))}
    return out


def intent_check(frame: np.ndarray, masks: np.ndarray, names: list[str], target: str, frame_idx: int, material: str, *, votes: int = 3) -> dict:
    """目标物体在这一帧是不是编辑指令要求的材质(和意图比,不和邻居比)。
    目标掩码轮廓画成绿线再问"绿线圈的区域",而不是问"画面中央的物体":大目标(本子、桌垫)被机械臂压在中央时,
    "中央的物体"就是机械臂,VLM 会如实答 no。压在上面的东西明说忽略。"""
    fi = min(frame_idx, masks.shape[1] - 1)
    m = masks[names.index(target), fi]
    b = bbox_small(m)
    if b is None:
        return {"checked": False, "ok": True, "why": "not visible"}
    mf = cv2.resize(m.astype(np.uint8), (frame.shape[1], frame.shape[0]), interpolation=cv2.INTER_NEAREST)
    fr = frame.copy()
    cnt, _ = cv2.findContours(mf, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(fr, cnt, -1, (0, 255, 0), 2)
    crop = _crop(fr, b)
    s = max(crop.shape[0], 160)
    crop = cv2.resize(crop, (int(crop.shape[1] * s / crop.shape[0]), s))
    q = (f"The region outlined in green is the object that used to be the '{target}', after a video edit. Is the surface inside the green "
         f"outline now made of / colored as: {material}? Judge only that surface itself; ignore anything lying on top of it or covering part "
         f"of it (robot arm, wires, other objects). Answer exactly one word: yes or no.")
    ys = [_ask_vlm([_img_part(crop), {"type": "text", "text": q}], max_tokens=5).strip().lower().startswith("y") for _ in range(votes)]
    return {"checked": True, "ok": sum(ys) * 2 > votes, "votes": int(sum(ys))}


def describe_targets(frame: np.ndarray, masks: np.ndarray, names: list[str], targets: list[str], frame_idx: int) -> dict:
    """留痕用:每个目标物体当前是什么材质/颜色(一个短语)。"""
    out = {}
    for tname in targets:
        if tname not in names:
            continue
        b = bbox_small(masks[names.index(tname), min(frame_idx, masks.shape[1] - 1)])
        if b is None:
            continue
        crop = _crop(frame, b)
        out[tname] = _ask_vlm([_img_part(crop), {"type": "text", "text": "Describe this object's material and color in at most 4 words."}], max_tokens=12).strip()
    return out


def pick_by_material(candidates: list[tuple[str, str]], masks: np.ndarray, names: list[str], targets: list[str], materials: dict,
                     runs: dict, f0: int, f1: int, *, fps: int = 30, votes: int = 3) -> dict:
    """几个都过了门的候选抽卡里,挑材质质感最贴编辑意图的那个(同 seed 随机会给抛光/磨砂/箔纸等不同质感,
    单张问 VLM"是不是抛光"它都说是;并排比较才分得出)。candidates = [(标签, 已重采样到源帧率的段视频路径)]。
    每个目标取段内第一次露面区间的中点帧,把各候选的目标裁剪(绿线勾掩码)拼成一行标 A/B/C 问 VLM,票数累加。"""
    letters = "ABCDEFGH"
    score = {lab: 0 for lab, _ in candidates}
    detail = []
    for tname in targets:
        mat = materials.get(tname)
        if not mat or tname not in names:
            continue
        mid = None
        for (r0, r1) in runs.get(tname, []):
            lo, hi = max(r0, f0), min(r1, f1)
            if hi - lo >= 5:
                mid = (lo + hi) // 2
                break
        if mid is None:
            continue
        m = masks[names.index(tname), min(mid, masks.shape[1] - 1)]
        b = bbox_small(m)
        if b is None:
            continue
        tiles = []
        for k, (lab, path) in enumerate(candidates):
            fr = video_ops.frames_at(path, [(mid - f0) / fps])[0]
            mf = cv2.resize(m.astype(np.uint8), (fr.shape[1], fr.shape[0]), interpolation=cv2.INTER_NEAREST)
            fr = fr.copy()
            cnt, _ = cv2.findContours(mf, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(fr, cnt, -1, (0, 255, 0), 2)
            crop = _crop(fr, b, pad=0.3)
            crop = cv2.resize(crop, (256, int(256 * crop.shape[0] / max(crop.shape[1], 1))), interpolation=cv2.INTER_CUBIC)
            cv2.putText(crop, letters[k], (6, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
            tiles.append(crop)
        h = max(t.shape[0] for t in tiles)
        img = np.concatenate([np.pad(t, ((0, h - t.shape[0]), (0, 8), (0, 0))) for t in tiles], axis=1)
        q = (f"These are {len(tiles)} candidate renderings (labeled {', '.join(letters[:len(tiles)])}) of the same object, which was supposed to become: "
             f"{mat}. Judge the surface inside the green outline. Which candidate matches that description best in BOTH color AND surface finish "
             f"(finish matters: e.g. 'polished' means smooth with clean specular highlights, not crumpled, wrinkled, foil-like, hammered, brushed or matte)? "
             f"Answer with exactly one letter.")
        vs = []
        for _ in range(votes):
            ans = _ask_vlm([_img_part(img), {"type": "text", "text": q}], max_tokens=4).strip().upper()
            for k, (lab, _) in enumerate(candidates):
                if ans.startswith(letters[k]):
                    score[lab] += 1
                    vs.append(lab)
                    break
        detail.append({"target": tname, "frame": mid, "material": mat, "votes": vs})
    best = max(candidates, key=lambda c: score[c[0]])[0] if candidates else None
    return {"winner": best, "score": score, "detail": detail}


# ------------------------------------------------------------- 碎片门:被夹住/半遮挡的目标物体露出的一角有没有被改
def target_appearance(orig_video: str, masks: np.ndarray, names: list[str], target: str, runs: list, *, n_frames: int = 8):
    """从目标可见的帧上学它的原始外观(Lab 中位数 + 容差 = 自身 ΔE 的 95 分位)。全部来自数据,不写死颜色。"""
    i = names.index(target)
    frames = []
    for (a, b) in runs:
        step = max(1, (b - a) // max(1, n_frames // max(1, len(runs))))
        frames += list(range(a, b, step))[: n_frames]
    frames = sorted(set(frames))[:n_frames]
    labs = []
    got = video_ops.frames_at(orig_video, [f / 30 for f in frames])
    for f, rgb in zip(frames, got):
        m = cv2.resize(masks[i, f].astype(np.uint8), (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)
        m = cv2.erode(m.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        labs.append(lab[m])
    labs = np.concatenate(labs) if labs else np.zeros((0, 3), np.float32)
    ab = labs[:, 1:]  # 只用色度(a,b),亮度随阴影变太大
    med = np.median(ab, axis=0)
    tol = float(np.percentile(np.linalg.norm(ab - med, axis=1), 95)) if len(ab) else 15.0
    chroma = float(np.linalg.norm(med - 128))  # 离中性灰多远;太灰的目标(银/白)这条门判不了,如实标出
    return {"ab": med.tolist(), "tol": float(min(max(tol, 8.0), 40.0)), "chroma": chroma, "n": int(len(ab))}


def _match(rgb: np.ndarray, app: dict) -> np.ndarray:
    """像素是否"像目标原样"。校准过的 app 用色相锥 + 彩度下限(阴影只压彩度不换色相,夹缝里的同一物体也能配上);
    没校准的 app 退回欧氏色度距离。"""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    if "ang_tol" in app:
        ang, c = _polar(lab, app)
        return (ang <= app["ang_tol"]) & (c >= app["floor"])
    d = np.linalg.norm(lab[..., 1:] - np.array(app["ab"], np.float32), axis=2)
    return d <= app["tol"]


def _polar(lab: np.ndarray, app: dict):
    """每个像素的色度向量相对签名色度的夹角(度)与彩度。"""
    v = lab[..., 1:] - 128.0
    s = np.array(app["ab"], np.float32) - 128.0
    cs = float(np.linalg.norm(s)) + 1e-6
    c = np.linalg.norm(v, axis=2) + 1e-6
    ang = np.degrees(np.arccos(np.clip((v @ s) / (c * cs), -1.0, 1.0)))
    return ang, c


def _scan_region(masks: np.ndarray, names: list[str], keep_names: list[str], target: str, f: int, H: int, W: int) -> np.ndarray:
    """碎片可能出现的区域:保留物体(机器人/容器)掩码外扩 7px,扣掉目标自己(可见时)外扩 15px。"""
    keep_idx = [names.index(k) for k in keep_names if k in names]
    reg = np.zeros(masks.shape[2:], np.uint8)
    for k in keep_idx:
        reg |= masks[k, f].astype(np.uint8)
    reg = cv2.dilate(cv2.resize(reg, (W, H), interpolation=cv2.INTER_NEAREST), np.ones((7, 7), np.uint8))
    own = cv2.dilate(cv2.resize(masks[names.index(target), f].astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST), np.ones((15, 15), np.uint8))
    return (reg & (1 - own)).astype(bool)


def _blobs(m: np.ndarray, min_blob: int) -> list:
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    return [(int(stats[k, 4]), [int(stats[k, 0]), int(stats[k, 1]), int(stats[k, 2]), int(stats[k, 3])]) for k in range(1, n) if stats[k, 4] >= min_blob]


def calibrate_match(orig_video: str, masks: np.ndarray, names: list[str], target: str, keep_names: list[str], app: dict,
                    visible_frames: list[int], *, fps: int = 30, min_blob: int = 40, step_deg: float = 4.0) -> dict:
    """把色相锥收窄到本底消失为止:目标完整可见时,同一区域里的干扰物(线缆、接插件)不该被配上。
    彩度下限 = 签名彩度的 20%(不低于 6);锥的最大角由原容差换算,逐步收窄,直到可见帧里最大匹配块 < min_blob。
    收到最窄仍有本底就保留最窄(后面的 1.5 倍本底门槛照常兜底)。参数全部从数据来。"""
    s = np.array(app["ab"], np.float32) - 128.0
    cs = float(np.linalg.norm(s)) + 1e-6
    floor = max(6.0, 0.2 * cs)
    ang_max = float(np.degrees(2 * np.arcsin(min(1.0, app["tol"] / (2 * cs)))))
    sample = visible_frames[:: max(1, len(visible_frames) // 10)][:10]
    labs = []
    for f, rgb in zip(sample, video_ops.frames_at(orig_video, [x / fps for x in sample])):
        H, W = rgb.shape[:2]
        labs.append((cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32), _scan_region(masks, names, keep_names, target, f, H, W)))
    chosen, base = None, None
    ang = ang_max
    while ang >= step_deg:
        base = 0
        for lab, reg in labs:
            a, c = _polar(lab, app)
            base = max(base, max((b[0] for b in _blobs((a <= ang) & (c >= floor) & reg, min_blob)), default=0))
        chosen = ang
        if base < min_blob:
            break
        ang -= step_deg
    app.update({"ang_tol": float(chosen), "floor": float(floor), "ang_max": ang_max, "baseline_cal": int(base)})
    return app


def sliver_scan(orig_video: str, gen_video: str, masks: np.ndarray, names: list[str], target: str, keep_names: list[str],
                app: dict, gap_frames: list[int], visible_frames: list[int], *, fps: int = 30, min_blob: int = 40,
                gen_offset: int = 0, max_blobs: int = 6) -> dict:
    """在目标不可见的帧里(每 0.2s 一帧),保留物体(机器人/容器)区域内找"像目标原样、且生成后仍是目标原色系"的像素块。
    候选块全部返回(最多 max_blobs 个,按面积),交给 sliver_confirm_multi 编号分辨;这里不判真假。
    本底 = 目标完整可见时同一区域里的最大匹配块(线缆等干扰物),门槛 = max(min_blob, 1.5×本底)。
    gen_offset:gen_video 的第 0 帧对应原视频的第几帧(逐段过门时段视频从 f0 起)。"""
    def measure(f, rgb_o, rgb_g):
        H, W = rgb_o.shape[:2]
        reg = _scan_region(masks, names, keep_names, target, f, H, W)
        mo = _match(rgb_o, app) & reg
        if rgb_g is not None:
            # 生成后仍是目标原色系。以前用"原/生成色度差 ≤ 1.5×tol"当"没变":对低彩度碎片那是不设条件(红→金的色度差也在容差内)
            mo = mo & _match(video_ops.resize(rgb_g, (W, H)), app)
        return sorted(_blobs(mo, min_blob), reverse=True)[:max_blobs]
    vis_sample = visible_frames[:: max(1, len(visible_frames) // 10)][:10]
    base = 0
    for f, rgb in zip(vis_sample, video_ops.frames_at(orig_video, [x / fps for x in vis_sample])):
        base = max(base, max((b[0] for b in measure(f, rgb, None)), default=0))
    thr = max(min_blob, int(base * 1.5))
    gap_sample = gap_frames[:: max(1, int(fps * 0.2))]
    fo = video_ops.frames_at(orig_video, [x / fps for x in gap_sample])
    fg = video_ops.frames_at(gen_video, [(x - gen_offset) / fps for x in gap_sample])
    cands = []
    for f, ro, rg in zip(gap_sample, fo, fg):
        blobs = [b for b in measure(f, ro, rg) if b[0] > thr]
        if blobs:
            cands.append({"frame": f, "t": round(f / fps, 2), "blobs": blobs})
    return {"baseline_blob": base, "thr": thr, "candidates": cands}


def sliver_confirm_multi(orig_frame: np.ndarray, blobs: list, orig_name: str, *, votes: int = 3, margin: int = 100, up: int = 3) -> tuple[list[int], list[int]]:
    """一张带上下文的图(候选块并集外扩 margin),所有候选块编号画绿框,问 VLM 哪些编号是目标本体,多数票。
    单块小裁剪问不准(线缆会被当成方块,9/14);给足夹爪上下文、让它在几个块之间比较,实测 16/16 且三票全一致。
    返回 (确认的块下标, 每块票数)。"""
    H, W = orig_frame.shape[:2]
    x1 = min(b[1][0] for b in blobs); y1 = min(b[1][1] for b in blobs)
    x2 = max(b[1][0] + b[1][2] for b in blobs); y2 = max(b[1][1] + b[1][3] for b in blobs)
    X1, Y1, X2, Y2 = max(0, x1 - margin), max(0, y1 - margin), min(W, x2 + margin), min(H, y2 + margin)
    crop = cv2.resize(orig_frame[Y1:Y2, X1:X2].copy(), None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    for j, (_, (x, y, w, h)) in enumerate(blobs, 1):
        p1 = ((x - X1) * up - 4, (y - Y1) * up - 4); p2 = ((x + w - X1) * up + 4, (y + h - Y1) * up + 4)
        cv2.rectangle(crop, p1, p2, (0, 255, 0), 2)
        cv2.putText(crop, str(j), (max(0, p1[0]), max(14, p1[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    q = (f"Robot video frame, zoomed in. The robot is holding or partly covering the '{orig_name}'; most of it may be hidden, but small parts "
         f"may peek out through gaps. Several candidate patches are marked with numbered green boxes. Which boxes contain part of the "
         f"'{orig_name}' itself? Wires, ribbon cables, connectors, shadows and the table surface are NOT it. Answer only with the box numbers "
         f"separated by commas, or the word none.")
    counts = [0] * len(blobs)
    for _ in range(votes):
        ans = _ask_vlm([_img_part(crop), {"type": "text", "text": q}], max_tokens=20).lower()
        for t in re.findall(r"\d+", ans):
            j = int(t) - 1
            if 0 <= j < len(blobs):
                counts[j] += 1
    return [j for j, c in enumerate(counts) if c * 2 > votes], counts


def _iou(a: list[int], b: list[int]) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax2, bx2) - max(a[0], b[0])), max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    return inter / float(a[2] * a[3] + b[2] * b[3] - inter + 1e-6)


def sliver_gate(orig_video: str, gen_video: str, masks: np.ndarray, names: list[str], targets: list[str], keep_names: list[str],
                materials: dict, runs: dict, f0: int, f1: int, *, fps: int = 30, min_run: int = 2, gen_offset: int = 0,
                prior: dict | None = None) -> dict:
    """段 [f0,f1) 内逐目标:找不可见帧 → 校准色相锥筛候选块 → 编号问 VLM 哪些是目标 → 连续 ≥min_run 个采样被确认才判失败。
    prior:上一轮(修补前)同一目标的裁决 {frame: [(bbox, 是否目标)]},与之重叠(IoU>0.5)的块直接沿用结论不再问 VLM——
    修补后线缆等干扰块还在,靠它把修补后核验的 VLM 调用压到接近零。
    返回项 confirmed_blobs = {frame: [bbox, ...]} 供 sliver_repair 只修确认的块。"""
    out = {}
    for tname in targets:
        if tname not in names or tname not in materials:
            continue
        vis = sorted({t for (a, b) in runs.get(tname, []) for t in range(a, b)})
        vis_set = set(vis)
        gap = [t for t in range(f0, f1) if t not in vis_set]
        if not gap:
            out[tname] = {"checked": False, "leak": False}
            continue
        app = target_appearance(orig_video, masks, names, tname, runs.get(tname, []))
        if app["chroma"] < 12:
            out[tname] = {"checked": False, "leak": False, "why": f"target too neutral (chroma {app['chroma']:.1f}), color gate not applicable"}
            continue
        calibrate_match(orig_video, masks, names, tname, keep_names, app, vis, fps=fps)
        scan = sliver_scan(orig_video, gen_video, masks, names, tname, keep_names, app, gap, vis, fps=fps, gen_offset=gen_offset)
        pri = (prior or {}).get(tname, {})
        confirmed, confirmed_blobs, verdicts = [], {}, {}
        vlm_calls = 0
        orig_frames = video_ops.frames_at(orig_video, [c["t"] for c in scan["candidates"]]) if scan["candidates"] else []
        for c, ofr in zip(scan["candidates"], orig_frames):
            known = pri.get(c["frame"], [])
            picked, counts = [], [None] * len(c["blobs"])
            ask = []
            for j, b in enumerate(c["blobs"]):
                hit = [k for k in known if _iou(k[0], b[1]) > 0.5]
                if hit:
                    counts[j] = "prior"
                    if hit[0][1]:
                        picked.append(j)
                else:
                    ask.append(j)
            if ask:
                idx, cnt = sliver_confirm_multi(ofr, [c["blobs"][j] for j in ask], tname)
                vlm_calls += 3
                for jj, j in enumerate(ask):
                    counts[j] = cnt[jj]
                    if jj in idx:
                        picked.append(j)
            c["confirm"] = {"picked": sorted(picked), "votes": counts}
            verdicts[c["frame"]] = [(b[1], j in picked) for j, b in enumerate(c["blobs"])]
            if picked:
                confirmed.append(c["t"])
                confirmed_blobs[c["frame"]] = [c["blobs"][j][1] for j in sorted(picked)]
        best = run = 0
        prev = None
        for t in confirmed:
            run = run + 1 if (prev is not None and t - prev <= 0.45) else 1
            best = max(best, run); prev = t
        out[tname] = {"checked": True, "appearance": {k: app[k] for k in ("ab", "tol", "chroma", "ang_tol", "floor", "baseline_cal") if k in app},
                      "baseline_blob": scan["baseline_blob"], "thr": scan["thr"], "n_candidate_samples": len(scan["candidates"]), "vlm_calls": vlm_calls,
                      "candidates": [(c["t"], [b[0] for b in c["blobs"]], c["confirm"]["votes"]) for c in scan["candidates"]],
                      "confirmed_t": confirmed, "confirmed_blobs": confirmed_blobs, "verdicts": verdicts,
                      "longest_run": best, "leak": best >= min_run}
    return out


# ------------------------------------------------------------- 碎片修补:把 VLM 确认的碎片像素的色度换成目标生成后的色度(保亮度)
def generated_appearance(gen_video: str, masks: np.ndarray, names: list[str], target: str, runs: list, *, n_frames: int = 8, gen_offset: int = 0) -> dict:
    """目标在生成视频里(可见帧、掩码内)的色度中位数,即"它现在该长什么样"。"""
    i = names.index(target)
    frames = []
    for (a, b) in runs:
        step = max(1, (b - a) // max(1, n_frames // max(1, len(runs))))
        frames += list(range(a, b, step))[: n_frames]
    frames = sorted(set(frames))[:n_frames]
    abs_ = []
    for f, rgb in zip(frames, video_ops.frames_at(gen_video, [(f - gen_offset) / 30 for f in frames])):
        m = cv2.resize(masks[i, f].astype(np.uint8), (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST)
        m = cv2.erode(m, np.ones((5, 5), np.uint8)).astype(bool)
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
        abs_.append(lab[m][:, 1:])
    ab = np.concatenate(abs_) if abs_ else np.zeros((0, 2), np.float32)
    return {"ab": np.median(ab, axis=0).tolist() if len(ab) else [128.0, 128.0], "n": int(len(ab))}


def sliver_repair(orig_video: str, gen_video: str, out_video: str, masks: np.ndarray, names: list[str], targets: list[str],
                  keep_names: list[str], runs: dict, gate: dict, *, fps: int = 30, gen_offset: int = 0, n_frames_total: int | None = None,
                  bridge: int = 4, pad: int = 6) -> dict:
    """只修 sliver_gate 确认过的块:每帧取最近的已裁决采样帧(|Δf| ≤ bridge),其确认块外扩 pad 像素为修补区,
    区内"像目标原样且生成后仍是原色系"的像素 → a,b 换成目标生成后的色度,L 不动。没被 VLM 确认的块一个像素都不碰。"""
    apps, gens, region_by_frame = {}, {}, {}
    for t in targets:
        g = gate.get(t, {})
        if t not in names or not runs.get(t) or not g.get("checked") or not g.get("confirmed_blobs"):
            continue
        apps[t] = target_appearance(orig_video, masks, names, t, runs[t])
        vis = sorted({x for (a, b) in runs[t] for x in range(a, b)})
        calibrate_match(orig_video, masks, names, t, keep_names, apps[t], vis, fps=fps)
        gens[t] = generated_appearance(gen_video, masks, names, t, runs[t], gen_offset=gen_offset)
        region_by_frame[t] = {int(f): bbs for f, bbs in g["confirmed_blobs"].items()}
    w = None
    changed = 0
    n = 0
    frames_touched = 0
    for (_, ro), (_, rg) in zip(video_ops.iter_frames(orig_video), video_ops.iter_frames(gen_video)):
        f = n + gen_offset
        if f >= masks.shape[1] or (n_frames_total and n >= n_frames_total):
            break
        H, W = ro.shape[:2]
        rg = video_ops.resize(rg, (W, H))
        out = rg
        lab_g = None
        for t, ap in apps.items():
            near = [fs for fs in region_by_frame[t] if abs(fs - f) <= bridge]
            if not near:
                continue
            fs = min(near, key=lambda x: abs(x - f))
            reg = np.zeros((H, W), np.uint8)
            for (x, y, bw, bh) in region_by_frame[t][fs]:
                reg[max(0, y - pad):min(H, y + bh + pad), max(0, x - pad):min(W, x + bw + pad)] = 1
            m = _match(ro, ap) & _match(rg, ap) & reg.astype(bool)
            if m.sum() < 5:
                continue
            if lab_g is None:
                lab_g = cv2.cvtColor(rg, cv2.COLOR_RGB2LAB).astype(np.float32)
            m = cv2.dilate(m.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
            lab_g[m, 1] = gens[t]["ab"][0]
            lab_g[m, 2] = gens[t]["ab"][1]
            changed += int(m.sum())
        if lab_g is not None:
            frames_touched += 1
            out = cv2.cvtColor(np.clip(lab_g, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
        if w is None:
            w = video_ops.Writer(out_video, fps, W, H)
        w.write(out)
        n += 1
    if w:
        w.close()
    return {"frames": n, "frames_touched": frames_touched, "pixels_changed": changed, "generated_ab": {t: g["ab"] for t, g in gens.items()},
            "match": {t: {k: a[k] for k in ("ang_tol", "floor", "baseline_cal") if k in a} for t, a in apps.items()}, "out": out_video}
