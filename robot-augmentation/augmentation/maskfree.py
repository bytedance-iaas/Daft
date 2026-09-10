"""不用掩码的几何层:切段避开夹持期、锚帧、意图门、首段择优,全部只靠 VLM 看整帧。
目的:静态编辑与换被操作物体走同一条纯链式路径,省掉几何档案(SAM2/GroundingDINO/26 分钟)。
判定比掩码版粗(定位靠 VLM 自己),但金银互换这类粗错误抓得住;每步都是 3 票多数。"""
from __future__ import annotations

import json
import re

import cv2
import numpy as np

from . import video_ops
from .vlm import ask_vlm as _ask_vlm, img_part as _img_part, inventory


def _json(txt: str) -> dict:
    try:
        return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    except Exception:
        return {}


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def visible_probe(frame: np.ndarray, targets: list[str], *, votes: int = 3) -> dict:
    """每个目标在这一帧是否"完整可见且没被机械臂夹着/挡着"(在盒子里但看得见也算可见:接力时认得出就行)。
    前两票全体一致就不问第三票。"""
    q = ("Objects of interest: " + json.dumps(targets, ensure_ascii=False) + ". For each object, is it clearly and fully visible in this "
         "frame, NOT being held by the robot gripper and NOT partly hidden behind the robot arm? An object resting inside an open box "
         "counts as visible if you can see it. Output JSON only: {\"<object>\": true/false}")
    ballots = []
    for k in range(votes):
        j = _json(_ask_vlm([_img_part(frame), {"type": "text", "text": q}], max_tokens=120))
        ballots.append({t: _truthy(j.get(t, False)) for t in targets})
        if k == 1 and ballots[0] == ballots[1]:
            break
    return {t: sum(b[t] for b in ballots) * 2 > len(ballots) for t in targets}


def choose_cuts(src: str, n_frames: int, fps: int, targets: list[str], *, max_seg: float, min_seg: float = 4.0,
                coarse: float = 2.0, fine: float = 1.0, probes: dict | None = None) -> tuple[list, dict]:
    """切点策略:从 t+min_seg 起每 coarse 秒探一次(懒扫,遇到第一次"有目标不可见/被夹"就停),
    切在它之前最后一个全可见时刻(再按 fine 精调);窗口里全程可见就切在上限;窗口开头就已被夹住则切在重新可见的第一刻。
    这样首段尽量短且以"所有目标可见"收尾(v6 掩码版切在 9s 的效果),首段多抽择优也便宜。
    探针只看源视频(可见/遮挡只取决于动作,源与生成一致;看生成帧会按颜色认物体)。返回 (spans, 探针记录)。"""
    dur = n_frames / fps
    spans, t = [], 0.0
    probes = probes if probes is not None else {}
    def vis(tc):
        tc = round(min(tc, dur - 1 / fps), 3)
        if tc not in probes:
            probes[tc] = visible_probe(video_ops.frames_at(src, [tc])[0], targets)
        return all(probes[tc].values())
    while dur - t > max_seg + 1e-6:
        lo, hi = t + min_seg, t + max_seg
        grid = [round(lo + k * coarse, 3) for k in range(int((hi - lo) / coarse) + 1)]
        if grid[-1] < hi - 1e-6:
            grid.append(round(hi, 3))
        cut, first_bad = None, None
        for k, g in enumerate(grid):
            if not vis(g):
                first_bad = k
                break
        if first_bad is None:
            cut = round(hi, 3)
        elif first_bad > 0:                              # 被夹之前最后一个全可见的粗格,再往后按 fine 精调
            cut = grid[first_bad - 1]
            tc = cut + fine
            while tc < grid[first_bad] - 1e-6 and vis(tc):
                cut, tc = round(tc, 3), tc + fine
        else:                                            # 窗口一开始就被夹着:往后找重新可见的第一刻
            cut = round(hi, 3)
            for g in grid[1:]:
                if vis(g):
                    cut = g
                    break
        spans.append((t, cut))
        t = cut
    spans.append((t, dur))
    return spans, probes


def anchor_frame(src: str, targets: list[str], f0: int, f1: int, fps: int, *, coarse: float = 4.0, probes: dict | None = None) -> int | None:
    """在前段 [f0,f1) 里找一帧"所有目标可见且未被夹持"当身份锚帧(绝对帧号),探针看**源视频**同一时刻:
    可见与否只取决于动作,源与生成一致;看生成帧问"红方块在不在"会因为它已变金而答否(实测两段都返回 None)。
    从段尾往前每 coarse 秒探一次,命中即返;都不中再探段首。"""
    probes = probes if probes is not None else {}
    def vis(tc):
        tc = round(tc, 3)
        if tc not in probes:
            probes[tc] = visible_probe(video_ops.frames_at(src, [tc])[0], targets)
        return all(probes[tc].values())
    t_end = (f1 - 1) / fps
    times = []
    tc = t_end - 0.5
    while tc > f0 / fps:
        times.append(tc)
        tc -= coarse
    times.append(f0 / fps)
    for t in times:
        if vis(t):
            return int(round(t * fps))
    return None


def _pair(orig: np.ndarray, gen: np.ndarray) -> np.ndarray:
    """原帧 | 生成帧 并排(同一时刻),身份靠位置对应:物体名指的是左图里的东西,右图看同位置。"""
    gen = video_ops.resize(gen, (orig.shape[1], orig.shape[0]))
    a, b = orig.copy(), gen.copy()
    cv2.putText(a, "ORIGINAL", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
    cv2.putText(b, "EDITED", (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
    return np.concatenate([a, np.full((a.shape[0], 8, 3), 255, np.uint8), b], axis=1)


def intent_check(orig_frame: np.ndarray, gen_frame: np.ndarray, targets: list[str], materials: dict, *, votes: int = 3) -> dict:
    """同一时刻 原帧|生成帧 并排问:左图里叫某名字的物体,右图同一位置上的那个现在是不是要求的材质(只判材质/颜色,不判质感:
    整帧尺度下 VLM 对"抛光"判不稳,v6 真抛光也只 1/3;质感交给择优并排比)。
    单看生成帧会按颜色认物体——金银互换后 VLM 把桌上的金块当成"红方块",漏判(v1 链式 22s 实测);位置对应才是身份。
    返回 {目标: {"visible","ok","votes"}};左图看不见的记 n/a。"""
    img = _pair(orig_frame, gen_frame)
    q = ("Left: a frame from the ORIGINAL robot video. Right: the SAME moment after a video edit. The object names below refer to objects in the "
         "LEFT image; the edit was supposed to give each of them the material/finish listed. For each object: locate it in the LEFT image, then look "
         "at the SAME POSITION in the RIGHT image and answer yes if the object there now has that base material/color, no if it has a different "
         "material or color, or n/a if the object is not visible in the LEFT image. Judge base material and color only; IGNORE surface finish words "
         "such as polished/matte/brushed (finish is checked separately).\n" +
         json.dumps({t: materials.get(t, "") for t in targets}, ensure_ascii=False) +
         "\nOutput JSON only: {\"<object>\": \"yes\"|\"no\"|\"n/a\"}")
    yes = {t: 0 for t in targets}; seen = {t: 0 for t in targets}
    for _ in range(votes):
        j = _json(_ask_vlm([_img_part(img), {"type": "text", "text": q}], max_tokens=120))
        for t in targets:
            a = str(j.get(t, "n/a")).strip().lower()
            if a.startswith("n/a") or a == "":
                continue
            seen[t] += 1
            if a.startswith("y"):
                yes[t] += 1
    out = {}
    for t in targets:
        if seen[t] * 2 <= votes:
            out[t] = {"visible": False, "ok": True, "votes": f"{yes[t]}/{seen[t]}"}
        else:
            out[t] = {"visible": True, "ok": yes[t] * 2 > seen[t], "votes": f"{yes[t]}/{seen[t]}"}
    return out


def intent_gate(src: str, part_video: str, f0: int, targets: list[str], materials: dict, *, fps: int, n_samples: int = 4) -> list[dict]:
    """段内均匀抽 n_samples 个时刻,原帧|生成帧并排核材质;某目标在任一可见时刻被判不对即失败。"""
    info = video_ops.probe(part_video)
    dur = info["frames"] / fps
    times = [dur * (k + 1) / (n_samples + 1) for k in range(n_samples)]
    og = video_ops.frames_at(src, [f0 / fps + t for t in times])
    gg = video_ops.frames_at(part_video, times)
    checks = []
    for t, o, g in zip(times, og, gg):
        r = intent_check(o, g, targets, materials)
        for tname, v in r.items():
            if v["visible"]:
                checks.append({"checked": True, "ok": v["ok"], "votes": v["votes"], "target": tname, "t": round(f0 / fps + t, 2), "material": materials.get(tname)})
    if not checks:
        checks.append({"checked": False, "ok": True, "why": "targets never visible in sampled frames"})
    return checks


def pick_by_material(candidates: list[tuple[str, str]], targets: list[str], materials: dict, t_rel: float, *, votes: int = 3, orig_frame: np.ndarray | None = None) -> dict:
    """几张都过门的候选整帧并排(标 A/B/C),问哪张各目标的材质与质感最贴指令。"""
    letters = "ABCDEFGH"
    tiles = []
    if orig_frame is not None:
        o = video_ops.resize(orig_frame, (480, int(480 * orig_frame.shape[0] / orig_frame.shape[1])))
        cv2.putText(o, "ORIGINAL", (8, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 3)
        tiles.append(o)
    for k, (lab, path) in enumerate(candidates):
        fr = video_ops.frames_at(path, [t_rel])[0]
        fr = video_ops.resize(fr, (480, int(480 * fr.shape[0] / fr.shape[1])))
        cv2.putText(fr, letters[k], (8, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
        tiles.append(fr)
    h = max(t.shape[0] for t in tiles)
    img = np.concatenate([np.pad(t, ((0, h - t.shape[0]), (0, 8), (0, 0))) for t in tiles], axis=1)
    q = ((("The first image is the ORIGINAL scene for reference (object names below refer to it; match objects by position). ") if orig_frame is not None else "") +
         f"Then {len(candidates)} candidate renderings (labeled {', '.join(letters[:len(candidates)])}) of the same scene after an edit. The edit required: " +
         json.dumps({t: materials.get(t, "") for t in targets}, ensure_ascii=False) +
         ". Which candidate matches those materials best in BOTH color AND surface finish (e.g. 'polished' = smooth with clean specular "
         "highlights, not crumpled, foil-like, hammered, brushed or matte)? Answer with exactly one letter.")
    score = {lab: 0 for lab, _ in candidates}
    for _ in range(votes):
        ans = _ask_vlm([_img_part(img), {"type": "text", "text": q}], max_tokens=4).strip().upper()
        for k, (lab, _) in enumerate(candidates):
            if ans.startswith(letters[k]):
                score[lab] += 1
                break
    best = max(candidates, key=lambda c: score[c[0]])[0] if candidates else None
    return {"winner": best, "score": score, "t_rel": t_rel}


def sliver_check(orig_frame: np.ndarray, gen_frame: np.ndarray, targets: list[str], materials: dict, *, votes: int = 3) -> dict:
    """夹缝露红门(不用掩码):同一时刻 原帧|生成帧 并排,问"左图里该物体被夹住/遮挡时有没有露出一角;右图里那一角是不是还是原来的颜色"。
    返回 {目标: {"peeking": bool, "leak": bool, "votes": "x/y"}};物体完全可见或完全看不见都记 peeking=False。前两票一致不问第三票。"""
    img = _pair(orig_frame, gen_frame)
    q = ("Left: a frame from the ORIGINAL robot video. Right: the SAME moment after a video edit that was supposed to change each object below "
         "to the material listed. For each object: in the LEFT image, is the object being held by the gripper or partly hidden, with only a small "
         "part of it peeking out (through the gripper gap, around the fingers, etc.)? If NOT (it is fully visible, or completely hidden), answer "
         "n/a. If YES, look at the same spot in the RIGHT image: answer leak if that peeking part is still the object's ORIGINAL color/material, "
         "or ok if it has been changed to the new material. Wires, cables and connectors are not the object.\n" +
         json.dumps({t: materials.get(t, "") for t in targets}, ensure_ascii=False) +
         "\nOutput JSON only: {\"<object>\": \"leak\"|\"ok\"|\"n/a\"}")
    ballots = []
    for k in range(votes):
        j = _json(_ask_vlm([_img_part(img), {"type": "text", "text": q}], max_tokens=120))
        ballots.append({t: str(j.get(t, "n/a")).strip().lower() for t in targets})
        if k == 1 and ballots[0] == ballots[1]:
            break
    out = {}
    for t in targets:
        seen = [b[t] for b in ballots if not b[t].startswith("n/a") and b[t]]
        leak = sum(v.startswith("l") for v in seen)
        peeking = len(seen) * 2 > len(ballots)
        out[t] = {"peeking": peeking, "leak": peeking and leak * 2 > len(seen), "votes": f"{leak}/{len(seen)}/{len(ballots)}"}
    return out


def sliver_gate(src: str, gen: str, targets: list[str], materials: dict, *, fps: int, step: float = 1.0, min_run: int = 2) -> dict:
    """整条视频每 step 秒一对帧过 sliver_check;某目标连续 ≥min_run 个采样判 leak 即算漏改(只记录不拦)。"""
    info = video_ops.probe(src)
    times = [round(k * step, 3) for k in range(int(info["frames"] / fps / step))]
    fo = video_ops.frames_at(src, times)
    fg = video_ops.frames_at(gen, times)
    per = {t: [] for t in targets}
    detail = []
    for t, o, g in zip(times, fo, fg):
        r = sliver_check(o, g, targets, materials)
        for name, v in r.items():
            per[name].append((t, v["peeking"], v["leak"]))
        if any(v["leak"] for v in r.values()):
            detail.append({"t": t, **{k: v["votes"] for k, v in r.items() if v["leak"]}})
    out = {}
    for name, seq in per.items():
        best = run = 0
        prev = None
        leak_t = [t for t, _, l in seq if l]
        for t in leak_t:
            run = run + 1 if (prev is not None and t - prev <= step + 1e-6) else 1
            best = max(best, run); prev = t
        out[name] = {"peeking_t": [t for t, p, _ in seq if p], "leak_t": leak_t, "longest_run": best, "leak": best >= min_run}
    out["_detail"] = detail
    return out


def locate(frame: np.ndarray, what: str, *, votes: int = 1) -> list[int] | None:
    """让 VLM 给一个粗框(归一化 0-1000 坐标 → 像素)。只用来裁剪放大,不用来判定,粗一点无妨。"""
    H, W = frame.shape[:2]
    q = (f"Locate: {what}. Output JSON only: {{\"bbox\": [x1, y1, x2, y2]}} with coordinates normalized to 0-1000 "
         f"(x from left, y from top). If not present, output {{\"bbox\": null}}.")
    j = _json(_ask_vlm([_img_part(frame), {"type": "text", "text": q}], max_tokens=60))
    b = j.get("bbox")
    if not b or len(b) != 4:
        return None
    x1, y1, x2, y2 = [float(v) for v in b]
    if max(x1, y1, x2, y2) <= 1.0:
        x1, y1, x2, y2 = x1 * 1000, y1 * 1000, x2 * 1000, y2 * 1000
    x1, x2 = sorted((int(x1 / 1000 * W), int(x2 / 1000 * W))); y1, y2 = sorted((int(y1 / 1000 * H), int(y2 / 1000 * H)))
    return [max(0, x1), max(0, y1), min(W, x2), min(H, y2)]


def _zoom_pair(orig: np.ndarray, gen: np.ndarray, box: list[int], *, pad: int = 60, up: int = 3):
    x1, y1, x2, y2 = box
    H, W = orig.shape[:2]
    X1, Y1, X2, Y2 = max(0, x1 - pad), max(0, y1 - pad), min(W, x2 + pad), min(H, y2 + pad)
    gen = video_ops.resize(gen, (W, H))
    o = cv2.resize(orig[Y1:Y2, X1:X2], None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    g = cv2.resize(gen[Y1:Y2, X1:X2], None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
    return o, g


def sliver_check_zoom(orig_frame: np.ndarray, gen_frame: np.ndarray, targets: list[str], materials: dict, *, votes: int = 3) -> dict:
    """夹缝露红门的放大版:整帧尺度下 VLM 看不见 10 像素的缝(全片 n/a),先让它框出夹爪/目标所在区域,
    裁剪放大 3 倍再并排问。每个目标一次定位 + 最多 3 票。"""
    out = {}
    for t in targets:
        # 先框夹爪(它永远在画面里;问"被夹住的物体"VLM 会答"不存在"),夹缝就在夹爪区域内
        box = locate(orig_frame, "the robot gripper / end effector: the jaws at the tip of the robot arm, including anything they are holding")
        if box is None:
            out[t] = {"peeking": False, "leak": False, "votes": "no-box", "box": None}
            continue
        o, g = _zoom_pair(orig_frame, gen_frame, box)
        r = sliver_check(o, g, [t], materials, votes=votes)[t]
        r["box"] = box
        out[t] = r
    return out


def sliver_gate_zoom(src: str, gen: str, targets: list[str], materials: dict, *, fps: int, step: float = 1.0, min_run: int = 2,
                     window: tuple[float, float] | None = None) -> dict:
    """整条视频(或 window 时段)每 step 秒一对帧过 sliver_check_zoom;某目标连续 ≥min_run 个采样判 leak 即算漏改(只记录不拦)。"""
    info = video_ops.probe(src)
    dur = info["frames"] / fps
    t0, t1 = window or (0.0, dur)
    times = [round(t, 3) for t in np.arange(t0, min(t1, dur - 1 / fps), step)]
    fo = video_ops.frames_at(src, times)
    fg = video_ops.frames_at(gen, times)
    per = {t: [] for t in targets}
    detail = []
    for t, o, g in zip(times, fo, fg):
        r = sliver_check_zoom(o, g, targets, materials)
        for name, v in r.items():
            per[name].append((t, v["peeking"], v["leak"]))
        if any(v["leak"] for v in r.values()):
            detail.append({"t": t, **{k: [v["votes"], v.get("box")] for k, v in r.items() if v["leak"]}})
    out = {}
    for name, seq in per.items():
        best = run = 0
        prev = None
        leak_t = [t for t, _, l in seq if l]
        for t in leak_t:
            run = run + 1 if (prev is not None and t - prev <= step + 1e-6) else 1
            best = max(best, run); prev = t
        out[name] = {"peeking_t": [t for t, p, _ in seq if p], "leak_t": leak_t, "longest_run": best, "leak": best >= min_run}
    out["_detail"] = detail
    return out
