"""几何档案(借 Cosmos-Transfer 的预处理思路):对一条视频逐帧提取
  - 物体/机械臂掩码:VLM 在首帧给框 → SAM2 视频传播(ultralytics SAM2VideoPredictor)
  - 深度:Depth-Anything-V2-Small(CPU)
落盘 <out>/masks.npz(bool [n_obj, T, H, W],缩到 WORK_SIZE)、depth.npz(float16 [T, h, w])、objects.json、overlay.mp4(人眼核)。

    python3 -m augmentation.geom_archive --video <源视频> --out <档案目录> \
        [--objects "robot arm,red cube,blue cube,black box"] [--step 1]
相机必须固定;CPU 上 1016 帧约十几分钟(掩码传播)+ 深度按 step 抽帧。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time

import cv2
import numpy as np

from . import video_ops

WORK_SIZE = (320, 240)
MODELS = os.environ.get("AUG_MODELS", os.path.join(os.path.abspath("augment_work"), "models"))  # 权重目录:grounding_dino_tiny/、sam2.1_s.pt、depth_anything_v2_small/


# ------------------------------------------------------------- VLM 给首帧框
def vlm_boxes(frame_rgb: np.ndarray, objects: list[str], *, model: str = "doubao-seed-2-0-pro-260215") -> dict:
    """问豆包首帧里各物体的像素框。返回 {name: [x1,y1,x2,y2]},没找到的不在里面。"""
    import requests
    h, w = frame_rgb.shape[:2]
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
    b64 = base64.b64encode(buf.tobytes()).decode()
    prompt = (f"这是一张 {w}x{h} 像素的俯视图。请给出下列每个物体在图中的外接框,像素坐标 [x1,y1,x2,y2],左上角为原点。"
              f"物体:{'、'.join(objects)}。只输出 JSON 对象,键是物体名(照抄),值是四个整数;图中没有的物体不要输出。")
    body = {"model": model, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt}]}]}
    r = requests.post(os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3") + "/chat/completions",
                      headers={"Authorization": f"Bearer {os.environ['ARK_API_KEY']}"}, json=body, timeout=120)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    txt = txt[txt.find("{"): txt.rfind("}") + 1]
    raw = json.loads(txt)
    out = {}
    for k, v in raw.items():
        if isinstance(v, list) and len(v) == 4:
            x1, y1, x2, y2 = [int(round(float(t))) for t in v]
            # 豆包有时按 0-1000 归一化给;按范围判断
            if max(x2, y2) <= 1000 and (w > 1000 or h > 1000 or max(x2, y2) > max(w, h)):
                x1, x2 = x1 * w // 1000, x2 * w // 1000
                y1, y2 = y1 * h // 1000, y2 * h // 1000
            out[k] = [max(0, x1), max(0, y1), min(w - 1, x2), min(h - 1, y2)]
    return out


# ------------------------------------------------------------- 通用清单:VLM 列物体 → GroundingDINO 出框 → VLM 逐框校验 → 几何常识去重
from .vlm import ask_vlm as _ask_vlm, img_part as _img_part, inventory  # noqa: E402


def verify_box(frame_rgb: np.ndarray, name: str, box: list[int], *, robot_part: bool = False) -> bool:
    """整图画红框反问 VLM。普通物体问"是不是 <名字>";机器人部件只问类别"是不是机器人的一部分",
    不问颜色——机器人是什么颜色由 VLM 看图决定,代码里不写死。张冠李戴的框在这里被丢掉。"""
    vis = frame_rgb.copy()
    x1, y1, x2, y2 = box
    # 框用绿色加黑边:红框套红方块会把 VLM 问糊涂("红色"指框还是指物体)
    cv2.rectangle(vis, (x1 - 2, y1 - 2), (x2 + 2, y2 + 2), (0, 0, 0), 5)
    cv2.rectangle(vis, (x1 - 2, y1 - 2), (x2 + 2, y2 + 2), (0, 255, 0), 3)
    q = ("Look at the object outlined by the green rectangle. Is it a part of the robot (its arm, gripper, wrist or base)? "
         "Answer with exactly one word: yes or no.") if robot_part else \
        f"Look at the object outlined by the green rectangle. Is it a '{name}'? Answer with exactly one word: yes or no."
    votes = [_ask_vlm([_img_part(vis), {"type": "text", "text": q}], max_tokens=5).strip().lower().startswith("y") for _ in range(3)]
    return sum(votes) >= 2


def _iou(a, b) -> float:
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / max(ua, 1)


def auto_boxes(frame_rgb: np.ndarray, *, max_frac: float = 0.6, iou_thr: float = 0.7, log=print) -> dict:
    """通用流程:清单 → 出框 → 校验 → 去重。返回 {"boxes": {name: box}, "robot": [...], "dropped": [...]}。"""
    inv = inventory(frame_rgb)
    log(f"[geom] inventory: {inv}")
    raw = gdino_boxes(frame_rgb, inv["objects"])
    h, w = frame_rgb.shape[:2]
    dropped, kept = [], {}
    for name, box in raw.items():
        frac = (box[2] - box[0]) * (box[3] - box[1]) / (w * h)
        if frac > max_frac:
            dropped.append((name, box, f"box covers {frac:.0%} of frame")); continue
        if not verify_box(frame_rgb, name, box, robot_part=name in inv["robot"]):
            dropped.append((name, box, "VLM says no")); continue
        kept[name] = box
    # 重叠去重:两个框 IoU 高只留一个(保留机器人部件优先,其次面积小的更具体)
    names = list(kept)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a in inv["robot"] and b in inv["robot"]:
                continue  # 机器人部件(臂/夹爪/底座)互相重叠是正常的,全部保留取并集
            if a in kept and b in kept and _iou(kept[a], kept[b]) > iou_thr:
                loser = b if (a in inv["robot"]) or (b not in inv["robot"] and _area(kept[a]) <= _area(kept[b])) else a
                dropped.append((loser, kept[loser], f"overlaps {a if loser == b else b}"))
                del kept[loser]
    for d in dropped:
        log(f"[geom] dropped {d[0]} {d[1]}: {d[2]}")
    missing = [o for o in inv["objects"] if o not in kept and o not in [d[0] for d in dropped]]
    if missing:
        log(f"[geom] not detected: {missing}")
    return {"boxes": kept, "robot": [r for r in inv["robot"] if r in kept], "dropped": [d[0] for d in dropped], "inventory": inv}


def _area(b) -> int:
    return (b[2] - b[0]) * (b[3] - b[1])


# ------------------------------------------------------------- GroundingDINO 给首帧框(Cosmos 原配方,比 VLM 坐标准)
def gdino_boxes(frame_rgb: np.ndarray, objects: list[str], *, path: str | None = None,
                box_thr: float = 0.3, text_thr: float = 0.25) -> dict:
    """每个物体名取最高分的一个框。返回 {name: [x1,y1,x2,y2]}(像素)。"""
    import torch
    from PIL import Image
    from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
    path = path or os.path.join(MODELS, "grounding_dino_tiny")
    proc = AutoProcessor.from_pretrained(path)
    model = AutoModelForZeroShotObjectDetection.from_pretrained(path).eval()
    img = Image.fromarray(frame_rgb)

    def query(phrase: str):
        inp = proc(images=img, text=phrase.lower().rstrip(".") + ".", return_tensors="pt")
        with torch.no_grad():
            out = model(**inp)
        res = proc.post_process_grounded_object_detection(out, inp.input_ids, threshold=box_thr, text_threshold=text_thr,
                                                          target_sizes=[img.size[::-1]])[0]
        if len(res["boxes"]) == 0:
            return None
        i = int(res["scores"].argmax())
        return float(res["scores"][i]), [int(round(float(v))) for v in res["boxes"][i].tolist()]

    best: dict = {}
    for o in objects:  # 一次一个短语:多短语合并查询时标签会串位
        r = query(o)
        if r is None and " " in o:  # 修饰语太长认不出 → 只用末尾名词(如 "black-rimmed open box" → "box")
            r = query(o.split()[-1])
        if r is not None:
            best[o] = r[1]
    return best


# ------------------------------------------------------------- SAM2 视频传播
def sam2_masks(video: str, boxes: dict, *, weights: str | None = None, imgsz: int = 640) -> tuple[list[str], np.ndarray]:
    """用首帧框做提示,SAM2 逐帧传播。返回 (物体名列表, bool 掩码 [n_obj, T, H, W] 原分辨率)。"""
    from ultralytics.models.sam import SAM2VideoPredictor
    weights = weights or os.path.join(MODELS, "sam2.1_s.pt")
    names = list(boxes)
    bboxes = [boxes[n] for n in names]
    pred = SAM2VideoPredictor(overrides=dict(conf=0.25, task="segment", mode="predict", imgsz=imgsz, model=weights, verbose=False))
    frames_masks = []
    for res in pred(source=video, bboxes=bboxes, stream=True):
        m = res.masks
        if m is None:
            frames_masks.append(None)
            continue
        arr = m.data.cpu().numpy().astype(bool)  # [k, H, W]
        frames_masks.append(arr)
    T = len(frames_masks)
    H, W = next(m.shape[1:] for m in frames_masks if m is not None)
    out = np.zeros((len(names), T, H, W), bool)
    for t, arr in enumerate(frames_masks):
        if arr is None:
            continue
        for i in range(min(len(names), arr.shape[0])):
            out[i, t] = arr[i]
    return names, out


def sam2_masks_chunked(video: str, names: list[str], *, chunk_frames: int = 90, weights: str | None = None,
                       imgsz: int = 640, robot: list[str] | None = None, first_boxes: dict | None = None,
                       max_jump: float = 60.0, log=print) -> tuple[list[str], np.ndarray]:
    """分块传播 + 连续性提示:每块的提示框 = 上一块末帧该物体掩码的外接框(自己的历史,不会换名字);
    掩码丢了(遮挡/出画)才用 GroundingDINO 重新检测,并做一次 VLM 校验(机器人部件问类别)。
    既截断整段传播的漂移,又不像逐块盲检那样把盒子检成本子。"""
    import tempfile
    info = video_ops.probe(video)
    T, fps, H, W = info["frames"], int(round(info["fps"])), info["height"], info["width"]
    robot = robot or []
    out = np.zeros((len(names), T, H, W), bool)
    tmpdir = tempfile.mkdtemp(prefix="sam2chunk_")
    prev_boxes: dict = dict(first_boxes or {})
    buf, start = [], 0

    def bbox_of(mask: np.ndarray, pad: int = 4):
        ys, xs = np.where(mask)
        if len(xs) < 30:
            return None
        return [max(0, int(xs.min()) - pad), max(0, int(ys.min()) - pad), min(W - 1, int(xs.max()) + pad), min(H - 1, int(ys.max()) + pad)]

    def flush(start, buf):
        nonlocal prev_boxes
        if not buf:
            return
        seg = os.path.join(tmpdir, f"c{start}.mp4")
        w = video_ops.Writer(seg, fps, W, H, crf=12)
        for f in buf:
            w.write(f)
        w.close()
        boxes = {}
        lost = [n for n in names if n not in prev_boxes]
        for n in names:
            if n in prev_boxes:
                boxes[n] = prev_boxes[n]
        if lost:
            det = gdino_boxes(buf[0], lost)
            for n, b in det.items():
                if verify_box(buf[0], n, b, robot_part=n in robot):
                    boxes[n] = b
                else:
                    log(f"[geom] chunk {start}: re-detected {n} rejected by VLM")
        log(f"[geom] chunk {start}-{start + len(buf)}: tracked {[n for n in names if n in prev_boxes]} re-detected {[n for n in lost if n in boxes]}")
        new_prev: dict = {}
        if boxes:
            pn, pm = sam2_masks(seg, boxes, weights=weights, imgsz=imgsz)
            L = min(pm.shape[1], len(buf))
            for i, n in enumerate(pn):
                k = names.index(n)
                track = pm[i, :L].copy()
                # 连续性守卫:物体质心不能瞬移(>max_jump 像素/帧)——那是轨道跳到别的东西上了,从跳变处截断,下一块重检
                last_c = None
                for t in range(L):
                    ys, xs = np.where(track[t])
                    if len(xs) < 30:
                        last_c = None
                        continue
                    c = (xs.mean(), ys.mean())
                    if last_c is not None and np.hypot(c[0] - last_c[0], c[1] - last_c[1]) > max_jump:
                        log(f"[geom] chunk {start}: {n} jumped {np.hypot(c[0] - last_c[0], c[1] - last_c[1]):.0f}px at frame {start + t} → track cut")
                        track[t:] = False
                        break
                    last_c = c
                out[k, start:start + L] = track
                b = bbox_of(track[L - 1])
                if b is not None:
                    new_prev[n] = b
        prev_boxes = new_prev
        os.remove(seg)

    for t, (_, rgb) in enumerate(video_ops.iter_frames(video)):
        buf.append(rgb)
        if len(buf) == chunk_frames:
            flush(start, buf); start = t + 1; buf = []
    flush(start, buf)
    return names, out


# ------------------------------------------------------------- 深度
class Depth:
    def __init__(self, path: str | None = None):
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        path = path or os.path.join(MODELS, "depth_anything_v2_small")
        self.proc = AutoImageProcessor.from_pretrained(path)
        self.model = AutoModelForDepthEstimation.from_pretrained(path).eval()
        self.torch = torch

    def __call__(self, rgb: np.ndarray, size=WORK_SIZE) -> np.ndarray:
        with self.torch.no_grad():
            inp = self.proc(images=rgb, return_tensors="pt")
            d = self.model(**inp).predicted_depth[0].numpy()
        return cv2.resize(d, size, interpolation=cv2.INTER_AREA).astype(np.float16)


# ------------------------------------------------------------- 主流程
def build(video: str, out: str, objects: list[str], *, step: int = 1, depth_step: int = 3, overlay: bool = True,
          detector: str = "auto", boxes: dict | None = None, skip_depth: bool = False, chunk_frames: int = 90) -> dict:
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    first = next(video_ops.iter_frames(video))[1]
    robot_parts: list[str] = []
    if boxes is None and detector == "auto":
        ab = auto_boxes(first, log=lambda m: print(m, flush=True))
        boxes, robot_parts = ab["boxes"], ab["robot"]
    elif boxes is None:
        boxes = gdino_boxes(first, objects) if detector == "gdino" else vlm_boxes(first, objects)
        missing = [o for o in objects if o not in boxes]
        if missing and detector == "gdino":
            print(f"[geom] gdino missed {missing}, asking VLM", flush=True)
            boxes.update({k: v for k, v in vlm_boxes(first, missing).items()})
    print(f"[geom] boxes ({detector}): {boxes}", flush=True)
    # 首帧框叠图,人眼核
    vis = first.copy()
    for k, (x1, y1, x2, y2) in boxes.items():
        cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 0, 0), 2)
        cv2.putText(vis, k, (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA)
    cv2.imwrite(os.path.join(out, "boxes_first_frame.png"), cv2.cvtColor(vis, cv2.COLOR_RGB2BGR))
    if chunk_frames > 0:
        names, masks = sam2_masks_chunked(video, list(boxes), chunk_frames=chunk_frames, robot=robot_parts, first_boxes=boxes,
                                          log=lambda m: print(m, flush=True))
    else:
        names, masks = sam2_masks(video, boxes)
    T = masks.shape[1]
    print(f"[geom] sam2 masks {masks.shape} {time.time() - t0:.0f}s", flush=True)
    small = np.zeros((len(names), T, WORK_SIZE[1], WORK_SIZE[0]), bool)
    for i in range(len(names)):
        for t in range(T):
            small[i, t] = cv2.resize(masks[i, t].astype(np.uint8), WORK_SIZE, interpolation=cv2.INTER_NEAREST).astype(bool)
    np.savez_compressed(os.path.join(out, "masks.npz"), masks=small, names=np.array(names))
    np.savez_compressed(os.path.join(out, "masks_full.npz"), masks=np.packbits(masks, axis=-1), shape=np.array(masks.shape))
    if skip_depth and os.path.exists(os.path.join(out, "depth.npz")):
        print("[geom] depth reused", flush=True)
    else:
        dep = Depth()
        depths, dts = [], []
        for t, (_, rgb) in enumerate(video_ops.iter_frames(video)):
            if t % depth_step == 0:
                depths.append(dep(rgb)); dts.append(t)
        np.savez_compressed(os.path.join(out, "depth.npz"), depth=np.stack(depths), frame_idx=np.array(dts))
        print(f"[geom] depth {len(depths)} frames {time.time() - t0:.0f}s", flush=True)
    meta = {"video": video, "objects": names, "robot": robot_parts, "boxes_first_frame": boxes, "frames": int(T), "work_size": WORK_SIZE,
            "depth_step": depth_step, "elapsed_s": round(time.time() - t0)}
    json.dump(meta, open(os.path.join(out, "objects.json"), "w"), ensure_ascii=False, indent=1)
    if overlay:
        colors = [(255, 0, 0), (0, 200, 0), (0, 120, 255), (255, 200, 0), (200, 0, 255), (0, 255, 255)]
        w = None
        for t, (_, rgb) in enumerate(video_ops.iter_frames(video)):
            vis = rgb.copy()
            for i in range(len(names)):
                m = masks[i, t]
                vis[m] = (0.55 * vis[m] + 0.45 * np.array(colors[i % len(colors)])).astype(np.uint8)
            if w is None:
                w = video_ops.Writer(os.path.join(out, "overlay.mp4"), 30, vis.shape[1], vis.shape[0], crf=22)
            w.write(vis)
        w.close()
    return meta


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--objects", default="white robot arm,red cube,blue cube,black box")
    p.add_argument("--depth-step", type=int, default=3)
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--detector", default="auto", choices=["auto", "gdino", "vlm"],
                   help="auto = VLM 列清单 → GroundingDINO 出框 → VLM 逐框校验 → 去重(不用 --objects)")
    p.add_argument("--boxes", help="手工指定首帧框 JSON:{\"name\":[x1,y1,x2,y2],...}")
    p.add_argument("--skip-depth", action="store_true", help="已有 depth.npz 时复用")
    p.add_argument("--chunk-frames", type=int, default=90, help="SAM2 分块传播的块长(帧);0 = 整段一次传播")
    a = p.parse_args(argv)
    meta = build(a.video, a.out, [s.strip() for s in a.objects.split(",")], depth_step=a.depth_step, overlay=not a.no_overlay,
                 detector=a.detector, boxes=json.loads(a.boxes) if a.boxes else None, skip_depth=a.skip_depth, chunk_frames=a.chunk_frames)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
