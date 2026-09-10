"""正式流程:C 写法提示词 + 链式分段 + VLM 门 + 自动重抽。

    augmentation --name X --edit "把桌面上那本牛皮纸本子替换成拉丝不锈钢金属板,本子的位置、大小和形态不变;把白色桌面替换成木纹桌面" \
        [--seg-len 28] [--pick-draws 3] [--max-draws 4] [--build-dataset]

默认(maskfree):VLM 看首帧列物体清单 → 指令映射到目标 → 切点探针(避开目标被夹持的时刻)→ 逐段生成:
接力段参考视频前接前段生成帧、@图片1 接力、@图片2 锚帧 → 每抽过脑补门/并排门/意图门,不过就重抽 → 首段多抽择优
→ 带重叠的拼接(色调对齐 + 交叉淡入淡出)→ 全片复核 → 指标 → 可选按源规格封回 LeRobot 数据集。
产物:<AUG_WORK>/final/<name>/ 与 <AUG_OUT>/final/<name>/;记录 run.json。
--geom <档案> 切到掩码档案模式(需要 [mask] 可选依赖);--chain-only 为纯链式对照。
"""
from __future__ import annotations

import argparse
import json
import os
import time

from . import ark_video, config, evaluate as E, geometry as G, maskfree as MF, video_ops, vlm_check as V
from .config import log
from .tos_io import Tos, TosUrl
from .vlm import edit_targets, inventory, target_materials

PROMPT_C = (
    "视频编辑:{edit}。"
    "只画原视频里能看见的东西:原视频里被遮挡、看不见的物体不要画出来,不要新增原视频里没有的任何物体。"
    "机械臂的位置、形状和动作不变,不生成额外物体,镜头固定不动,画面构图和视角不变。"
)
CHAIN_SENTENCE = "新替换上去的材质、颜色和背景必须与 @图片1 中的完全一致,@图片1 是这段视频前一秒的画面。"
OCCLUSION_SENTENCE = "被替换的物体在被机械臂夹住或被别的东西部分遮挡时,露出来的每一小块也必须按新材质画,不能保留原来的颜色。"
ANCHOR_SENTENCE = "@图片2 里每个被替换物体的新材质和颜色是准的,按物体所在位置一一对应,同一个物体在整段视频里必须保持 @图片2 里的样子,不要互换。"


def _last_frame_png(video: str, out_png: str) -> str:
    import cv2
    last = None
    for _, rgb in video_ops.iter_frames(video):
        last = rgb
    os.makedirs(os.path.dirname(out_png) or ".", exist_ok=True)
    cv2.imwrite(out_png, cv2.cvtColor(last, cv2.COLOR_RGB2BGR))
    return out_png


def plan_segments(n_frames: int, fps: int, seg_len: float) -> list[tuple[float, float]]:
    """等分成 ceil(dur/seg_len) 段,每段长度相近(避免最后一小截)。"""
    import math
    dur = n_frames / fps
    k = max(1, math.ceil(dur / seg_len))
    step = dur / k
    return [(round(i * step, 3), round((i + 1) * step, 3) if i < k - 1 else dur) for i in range(k)]


def run(a: argparse.Namespace) -> dict:
    tos = Tos()
    WORK, OUT, LEROBOT_SRC, CAM = config.work_dir(), config.out_prefix(), config.lerobot_src(), config.camera()
    cam_short = CAM.split(".")[-1]
    root = os.path.join(WORK, "final", a.name)
    os.makedirs(root, exist_ok=True)
    out_tos = OUT.join("final", a.name)
    rec: dict = {"name": a.name, "edit": a.edit, "model": a.model, "seg_len": a.seg_len, "max_draws": a.max_draws,
                 "lerobot_src": str(LEROBOT_SRC), "camera": CAM, "work": WORK, "geom": a.geom, "composite": bool(a.composite),
                 "prompt_template": PROMPT_C, "chain_sentence": CHAIN_SENTENCE, "segments": [], "started": time.time()}

    # 1. 源视频(帧率、尺寸都从它探,不写死)与切段
    src = os.path.join(WORK, "src", f"{cam_short}.mp4")
    if not os.path.exists(src):
        os.makedirs(os.path.dirname(src), exist_ok=True)
        tos.download(LEROBOT_SRC.join(config.video_rel_path(CAM)), src)
    info = video_ops.probe(src)
    FPS = int(round(info["fps"]))
    SRC_SIZE = (info["width"], info["height"])
    SEND_SIZE = config.send_size(*SRC_SIZE)
    rec["source"] = {"fps": FPS, "size": SRC_SIZE, "send_size": SEND_SIZE, "frames": info["frames"]}
    geom = None
    if a.geom:
        from . import identity as I
        meta = json.load(open(os.path.join(a.geom, "objects.json")))
        gnames, gmasks = I.load_small_masks(a.geom)
        targets = edit_targets(a.edit, meta["objects"], meta.get("robot", []), a.geom)
        okf = I.ok_frames(gmasks, gnames, targets, fps=FPS)
        vruns = I.visible_runs(gmasks, gnames, targets)
        spans = I.choose_cuts_v2(info["frames"], FPS, okf, vruns, max_seg=a.seg_len, min_seg=4.0)
        materials = target_materials(a.edit, targets) if targets else {}
        geom = {"names": gnames, "masks": gmasks, "targets": targets, "ok": okf, "runs": vruns, "materials": materials, "meta": meta}
        rec["targets"] = targets
        rec["target_materials"] = materials
        rec["visible_runs"] = {k: [(round(r[0] / FPS, 2), round(r[1] / FPS, 2)) for r in v] for k, v in vruns.items()}
        rec["ok_frames"] = int(okf.sum())
        log(f"targets={targets}; materials={materials}; visible runs={rec['visible_runs']}; cuts={spans}")
    elif not a.chain_only:
        # 不用掩码的几何层:物体清单、目标、切点、锚帧、意图门全靠 VLM 看整帧(见 maskfree.py)
        first = video_ops.frames_at(src, [0.0])[0]
        inv = inventory(first)
        targets = edit_targets(a.edit, inv["objects"], inv.get("robot", []), None)
        materials = target_materials(a.edit, targets) if targets else {}
        spans, probes = MF.choose_cuts(src, info["frames"], FPS, targets, max_seg=a.seg_len, min_seg=4.0) if targets else (plan_segments(info["frames"], FPS, a.seg_len), {})
        geom = {"maskfree": True, "names": inv["objects"], "targets": targets, "materials": materials, "meta": inv, "probes": probes}
        rec["targets"] = targets
        rec["target_materials"] = materials
        rec["cut_probes"] = {str(k): v for k, v in probes.items()}
        log(f"maskfree: objects={inv['objects']}; targets={targets}; materials={materials}; cuts={spans}; probes={len(probes)}")
    else:
        spans = plan_segments(info["frames"], FPS, a.seg_len)
    log(f"source {info['frames']} frames; {len(spans)} segments: {spans}")
    segs = []
    for i, (t0, t1) in enumerate(spans):
        p = os.path.join(root, f"src_seg{i}.mp4")
        # 接力段的参考视频从 t0 往前多取 overlap 秒:两段在这一秒里画的是同一段动作,拼接时可交叉淡入淡出 + 色调对齐,
        # 消掉段边界的台阶
        ov = a.overlap if i > 0 else 0.0
        t0_ref = max(0.0, t0 - ov)
        n = video_ops.cut_segment(src, t0_ref, t1, p, size=SEND_SIZE, fps=FPS)
        key = out_tos.join("src", os.path.basename(p))
        if not (a.chain_prefix and i > 0):
            tos.upload(p, key)        # chain_prefix 模式下接力段的参考视频要等前段生成完再拼(前段生成帧 + 原片),到时再传
        segs.append({"i": i, "t0": t0, "t1": t1, "t0_ref": t0_ref, "overlap_frames": int(round((t0 - t0_ref) * FPS)), "frames": n, "path": p, "tos": str(key)})

    # 2. 逐段:生成 → 检测 → 重抽 → 链
    prev_local = None
    accepted = []
    for seg in segs:
        i = seg["i"]
        if a.chain_prefix and i > 0 and accepted:
            # 参考视频 = 前段生成结果的最后 overlap 秒(已是新材质) + 本段原片(t0 起):把"木纹长什么样"直接放进参考视频里让模型接着画,
            # 而不只是一张 @图片1(一张参考图约束不住纹理)。生成后开头那 overlap 秒与前段重叠,拼接时交叉淡入淡出
            pseg0, pdr0 = accepted[-1]
            part0 = os.path.join(root, f"part_seg{pseg0['i']}.mp4")
            if not os.path.exists(part0):
                video_ops.resample_to(pdr0["local"], part0, fps=FPS, n_frames=pseg0["frames"], size=SRC_SIZE)
            k = seg["overlap_frames"]
            pref = os.path.join(root, f"src_seg{i}_prefixed.mp4")
            n_pref = video_ops.splice_prefix(part0, src, seg["t0"], seg["t1"], pref, prefix_frames=k, fps=FPS, size=SEND_SIZE)
            seg["path"], seg["frames"] = pref, n_pref
            tos.upload(pref, TosUrl.parse(seg["tos"]))
            log(f"seg{i}: reference = last {k} generated frames of seg{pseg0['i']} + source clip ({n_pref} frames)")
        url = tos.presign(TosUrl.parse(seg["tos"]))
        prompt = PROMPT_C.format(edit=a.edit)
        if geom and geom["targets"] and a.occlusion_hint:
            prompt += OCCLUSION_SENTENCE   # 实验:对夹缝露色无效且多引脑补;默认不加
        image_urls = []
        anchor_info = None
        if prev_local is not None:
            pseg0, pdr0 = accepted[-1]
            part0 = os.path.join(root, f"part_seg{pseg0['i']}.mp4")
            if not os.path.exists(part0):
                video_ops.resample_to(pdr0["local"], part0, fps=FPS, n_frames=pseg0["frames"], size=SRC_SIZE)
            # @图片1 = 前段生成结果在本段参考视频起点(t0_ref)那一刻的帧;不重叠时就是前段末帧
            t_ref = seg["t0_ref"] - pseg0["t0_ref"]
            fr1 = video_ops.frames_at(part0, [max(0.0, t_ref - 1 / FPS)])[0]
            png = os.path.join(root, f"ref_seg{i}.png")
            import cv2
            cv2.imwrite(png, cv2.cvtColor(fr1, cv2.COLOR_RGB2BGR))
            k = out_tos.join("ref", os.path.basename(png))
            tos.upload(png, k)
            image_urls = [tos.presign(k)]
            prompt += CHAIN_SENTENCE
            if geom and geom["targets"]:
                # 身份锚帧:在已接受的前段输出里找"所有目标物体可见且静止"的一帧
                for pseg, pdr in reversed(accepted):
                    f0, f1 = int(pseg["t0_ref"] * FPS), int(pseg["t1"] * FPS)
                    part = os.path.join(root, f"part_seg{pseg['i']}.mp4")
                    if not os.path.exists(part):
                        video_ops.resample_to(pdr["local"], part, fps=FPS, n_frames=pseg["frames"], size=SRC_SIZE)
                    af = MF.anchor_frame(src, geom["targets"], f0, f1, FPS, probes=geom.get("probes")) if geom.get("maskfree") else I.anchor_frame(geom["ok"], f0, f1)
                    if af is not None:
                        fr = video_ops.frames_at(part, [(af - f0) / FPS])[0]
                        apng = os.path.join(root, f"anchor_seg{i}.png")
                        import cv2
                        cv2.imwrite(apng, cv2.cvtColor(fr, cv2.COLOR_RGB2BGR))
                        ak = out_tos.join("ref", os.path.basename(apng))
                        tos.upload(apng, ak)
                        image_urls.append(tos.presign(ak))
                        prompt += ANCHOR_SENTENCE
                        anchor_info = {"from_seg": pseg["i"], "frame": af}
                        break
        seg_rec = {"i": i, "prompt": prompt, "chained": bool(image_urls), "anchor": anchor_info, "draws": []}
        chosen = None
        passed = []          # 过了全部门的抽卡;首段可攒 --pick-draws 张再按材质质感择优,后段接力首段的质感只要一张
        want = max(1, a.pick_draws) if i == 0 else 1
        for d in range(a.max_draws):
            r = ark_video.create_task(a.model, prompt, [url], image_urls=image_urls, task_type="edit",
                                      ratio="adaptive", duration=-1, resolution="480p")
            if not r["ok"]:
                seg_rec["draws"].append({"draw": d, "error": r["body"]})
                log(f"seg{i} d{d} submit failed: {json.dumps(r['body'], ensure_ascii=False)[:200]}")
                continue
            tid = r["body"]["id"]
            log(f"seg{i} d{d} submitted {tid}{' (chained)' if image_urls else ''}")
            j = ark_video.wait_task(tid, poll_s=15, timeout_s=1800, log=log)
            dr = {"draw": d, "id": tid, "status": j.get("status"), "usage": j.get("usage"), "seed": j.get("seed"), "wall_s": j.get("_wall_s")}
            if j.get("status") != "succeeded":
                dr["error"] = j.get("error")
                seg_rec["draws"].append(dr)
                log(f"seg{i} d{d} {j.get('status')}: {j.get('error')}")
                continue
            local = os.path.join(root, f"gen_seg{i}_d{d}.mp4")
            ark_video.download(j["content"]["video_url"], local)
            tos.upload(local, out_tos.join("gen", os.path.basename(local)))
            dr["local"] = local
            dr["probe"] = video_ops.probe(local)
            seg_rec["draws"].append(dr)
            log(f"seg{i} d{d} generated tokens={dr['usage']}")
            # 脑补门(逐抽):原帧|生成帧并排问 VLM,多画了东西或原有物体消失(如放进盒子后方块凭空不见)都重抽
            if a.vlm_gate:
                v = V.evaluate(seg["path"], local, step=1.0, edit=a.edit)
                dr["vlm"] = {k: v.get(k) for k in ("hallucinated", "missing", "new_span", "missing_span", "frames")}
                if v["hallucinated"] or v["missing"]:
                    log(f"seg{i} d{d} vlm gate: new={v['new_span']} missing={v['missing_span']} → redraw")
                    continue
                log(f"seg{i} d{d} vlm gate clean ({v['frames']} frames)")
            # 身份门:本段里每个目标物体每次露面(取该露面区间中点帧)都要是编辑指令要求的材质;和意图比,不和邻居比
            if geom and geom["targets"] and geom["materials"]:
                f0, f1 = int(seg["t0_ref"] * FPS), int(seg["t1"] * FPS)
                part_tmp = os.path.join(root, f"tmp_seg{i}_d{d}.mp4")
                video_ops.resample_to(local, part_tmp, fps=FPS, n_frames=seg["frames"], size=SRC_SIZE)
                checks = []
                if geom.get("maskfree"):
                    checks = MF.intent_gate(src, part_tmp, int(seg["t0_ref"] * FPS), geom["targets"], geom["materials"], fps=FPS)
                    for c in checks:
                        c.setdefault("frame", c.get("t"))
                for tname in ([] if geom.get("maskfree") else geom["targets"]):
                    mat = geom["materials"].get(tname)
                    if not mat:
                        continue
                    for (r0, r1) in geom["runs"].get(tname, []):
                        lo, hi = max(r0, f0), min(r1, f1)
                        if hi - lo < 5:
                            continue
                        mid = (lo + hi) // 2
                        fr = video_ops.frames_at(part_tmp, [(mid - f0) / FPS])[0]
                        c = I.intent_check(fr, geom["masks"], geom["names"], tname, mid, mat)
                        c.update({"target": tname, "frame": mid, "material": mat})
                        checks.append(c)
                dr["identity"] = checks
                bad = [c for c in checks if c["checked"] and not c["ok"]]
                log(f"seg{i} d{d} identity: " + "; ".join(f"{c['target']}@{c['frame']}={'ok' if c['ok'] else 'BAD'}({c.get('votes')})" for c in checks))
                if bad:
                    log(f"seg{i} d{d} identity mismatch: {[(c['target'], c['frame']) for c in bad]} → redraw")
                    continue
                # 碎片(被夹住/遮挡期间露出的一角没被改)不在这里挡:模型几乎必错,重抽等不来对的;拼回整条后统一修补 + 门核验
            passed.append(dr)
            if len(passed) >= want:
                break
        if len(passed) == 1 or (passed and not (geom and geom["targets"])):
            chosen = passed[0]
        elif passed:
            f0, f1 = int(seg["t0_ref"] * FPS), int(seg["t1"] * FPS)
            cands = []
            for pdr in passed:
                part_tmp = os.path.join(root, f"tmp_seg{i}_d{pdr['draw']}.mp4")
                if not os.path.exists(part_tmp):
                    video_ops.resample_to(pdr["local"], part_tmp, fps=FPS, n_frames=seg["frames"], size=SRC_SIZE)
                cands.append((f"d{pdr['draw']}", part_tmp))
            if geom.get("maskfree"):
                pk = MF.pick_by_material(cands, geom["targets"], geom["materials"], t_rel=0.4 * (seg["t1"] - seg["t0_ref"]), orig_frame=video_ops.frames_at(src, [seg["t0_ref"] + 0.4 * (seg["t1"] - seg["t0_ref"])])[0])
            else:
                pk = I.pick_by_material(cands, geom["masks"], geom["names"], geom["targets"], geom["materials"], geom["runs"], f0, f1, fps=FPS)
            seg_rec["pick"] = pk
            chosen = next(pdr for pdr in passed if f"d{pdr['draw']}" == pk["winner"]) if pk["winner"] else passed[0]
            log(f"seg{i} pick by material: {pk['score']} → d{chosen['draw']}")
        seg_rec["chosen_draw"] = chosen["draw"] if chosen else None
        rec["segments"].append(seg_rec)
        json.dump(rec, open(os.path.join(root, "run.json"), "w"), ensure_ascii=False, indent=1)
        if chosen is None:
            log(f"seg{i}: {a.max_draws} draws all hallucinated/failed → abort")
            rec["status"] = "failed"
            return rec
        prev_local = chosen["local"]
        accepted.append((seg, chosen))

    # 3. 拼回整条 + 指标 + 三格
    parts = []
    for seg, dr in accepted:
        part = os.path.join(root, f"part_seg{seg['i']}.mp4")
        video_ops.resample_to(dr["local"], part, fps=FPS, n_frames=seg["frames"], size=SRC_SIZE)
        parts.append(part)
    final = os.path.join(root, f"{a.name}_{cam_short}.mp4")
    asm = video_ops.assemble(parts, [seg["overlap_frames"] for seg, _ in accepted], final, fps=FPS, size=SRC_SIZE)
    n = asm["frames"]
    rec["assemble"] = asm
    if asm["boundaries"]:
        log("assemble: " + "; ".join(f"seg{b['boundary']} overlap={b['overlap_frames']}f tone gain={b['gain']} offset={b['offset']}" for b in asm["boundaries"]))
    tri = os.path.join(root, f"{a.name}_triptych.mp4")
    video_ops.triptych(src, final, tri, fps=FPS)
    parquet = os.path.join(WORK, "src", "data.parquet")
    if not os.path.exists(parquet):
        tos.download(LEROBOT_SRC.join(config.data_rel_path()), parquet)
    rec["metrics"] = E.evaluate(src, final, parquet, FPS)
    rec["geometry"] = G.evaluate(src, final, every=60)
    rec["frames"] = n
    for p in (final, tri):
        tos.upload(p, out_tos.join(os.path.basename(p)))
    log(f"assembled {n} frames; flow_r={rec['metrics']['flow_r']:.3f} lag={rec['metrics']['flow_lag_frames']} "
        f"iou={rec['metrics']['motion_iou_mean']:.3f} shift={rec['geometry'].get('shift_px_med')}")

    # 3a. 碎片门(目标被夹住/遮挡期间露出的一角有没有被改):默认整体关;
    #     --sliver-gate 才检测记录,--sliver-repair 才改像素(实验,已知会误修线缆)
    if geom and not geom.get("maskfree") and geom["targets"] and geom["materials"] and a.sliver_gate:
        keep_names = [o for o in geom["names"] if o not in geom["targets"]]
        slim = lambda sg: {k: {kk: vv for kk, vv in v.items() if kk not in ("candidates", "verdicts", "confirmed_blobs")} for k, v in sg.items()}
        # 碎片门:校准色相锥找候选块 → 编号问 VLM 哪些是目标 → 连续确认才算漏。默认只检测记录,不改像素。
        sg0 = I.sliver_gate(src, final, geom["masks"], geom["names"], geom["targets"], keep_names, geom["materials"], geom["runs"], 0, info["frames"], fps=FPS)
        rec["sliver_gate"] = slim(sg0)
        rec["sliver_gate"]["_confirmed"] = {k: {str(f): v for f, v in sg.get("confirmed_blobs", {}).items()} for k, sg in sg0.items()}
        log("sliver gate: " + "; ".join(f"{k}: {'LEAK(疑似)' if v.get('leak') else 'ok'} run={v.get('longest_run', 0)} confirmed={len(v.get('confirmed_t', []))}/{v.get('n_candidate_samples', 0)} vlm={v.get('vlm_calls', 0)}" for k, v in sg0.items()))
        if a.sliver_repair:
            # 像素修补(opt-in):只修 VLM 确认的块。已知局限(实测):VLM 对单独出现的线缆块会误认成目标,
            # 逐采样帧的判定有噪声 → 修补会在线缆上留黄斑、在时间上闪烁。默认关,留作实验入口。
            rep = os.path.join(root, f"{a.name}_{cam_short}_repaired.mp4")
            rec["sliver_repair"] = I.sliver_repair(src, final, rep, geom["masks"], geom["names"], geom["targets"], keep_names, geom["runs"], sg0, fps=FPS)
            log(f"sliver repair: {rec['sliver_repair']['pixels_changed']} px changed in {rec['sliver_repair']['frames_touched']} frames")
            prior = {k: v.get("verdicts", {}) for k, v in sg0.items()}
            sg = I.sliver_gate(src, rep, geom["masks"], geom["names"], geom["targets"], keep_names, geom["materials"], geom["runs"], 0, info["frames"], fps=FPS, prior=prior)
            rec["sliver_gate_after_repair"] = slim(sg)
            log("sliver gate after repair: " + "; ".join(f"{k}: {'LEAK' if v.get('leak') else 'ok'} run={v.get('longest_run', 0)} t={v.get('confirmed_t', [])[:6]} vlm={v.get('vlm_calls', 0)}" for k, v in sg.items()))
            tos.upload(rep, out_tos.join(os.path.basename(rep)))
            final = rep

    # 3b. VLM 并排检测当出口门(默认在纯生成视频上做)
    if a.vlm_gate:
        rec["vlm_check"] = V.evaluate(src, final, step=1.0, edit=a.edit)
        log(f"vlm gate: hallucinated={rec['vlm_check']['hallucinated']} span={rec['vlm_check']['run_span']} missing={rec['vlm_check']['missing']}")

    # 3b'. 夹缝露红门(maskfree,--sliver-gate):原帧|生成帧同一时刻并排问,只记录不拦
    if geom and geom.get("maskfree") and geom["targets"] and a.sliver_gate:
        try:
            sg = MF.sliver_gate(src, final, geom["targets"], geom["materials"], fps=FPS)
            rec["sliver_gate"] = sg
            log("sliver gate (maskfree): " + "; ".join(f"{k}: {'LEAK' if v['leak'] else 'ok'} run={v['longest_run']} leak_t={v['leak_t'][:8]}" for k, v in sg.items() if not k.startswith("_")))
        except Exception as e:   # 只记录的实验门,VLM 超时不能把整条跑批(已生成、已过门)拖死
            rec["sliver_gate"] = {"error": repr(e)[:300]}
            log(f"sliver gate (maskfree) failed, ignored: {e!r}"[:300])

    # 3c. 掩码合成(保留区贴回原像素)——默认关:
    #     两条真实数据上都是净亏损:贴回的物体被 SAM2 掩码磨圆、盒子内部盖住方块、夹爪缝里的原像素把生成结果抹回去;
    #     运动指标上涨是"贴得像原图"的假象。只留作"门发现机械臂走样又不想重抽"时的兜底开关。
    if a.composite:
        if not a.geom:
            raise SystemExit("--composite 需要 --geom")
        from . import composite as C
        meta = json.load(open(os.path.join(a.geom, "objects.json")))
        targets = edit_targets(a.edit, meta["objects"], meta.get("robot", []), a.geom)
        keep = [o for o in meta["objects"] if o not in targets or o in meta.get("robot", [])]
        log(f"composite: edit targets={targets}; keep={keep}")
        comp = os.path.join(root, f"{a.name}_{cam_short}_composited.mp4")
        rec["composite"] = C.run(src, final, a.geom, keep, comp, exclude=targets)
        rec["composite"]["targets"] = targets
        tri_c = os.path.join(root, f"{a.name}_composited_triptych.mp4")
        video_ops.triptych(src, comp, tri_c, fps=FPS)
        for p_ in (comp, tri_c):
            tos.upload(p_, out_tos.join(os.path.basename(p_)))
        rec["metrics_composited"] = E.evaluate(src, comp, parquet, FPS)
        if a.vlm_gate:
            rec["vlm_check_composited"] = V.evaluate(src, comp, step=1.0, edit=a.edit)
            log(f"vlm gate on composited: hallucinated={rec['vlm_check_composited']['hallucinated']}")
        final = comp  # 显式要了合成才用合成版封数据集
        log(f"composited → {comp}")

    # 4. 可选:封成 LeRobot 数据集
    if a.build_dataset:
        from .build_dataset import build
        dst = OUT.join("datasets", a.name)
        rec["dataset"] = build(tos, LEROBOT_SRC, dst, CAM, final, expect_frames=info["frames"])
        rec["dataset_tos"] = str(dst)
        log(f"dataset → {dst}")

    # 5. 费用
    tok = sum(d["usage"]["completion_tokens"] for s in rec["segments"] for d in s["draws"] if d.get("usage"))
    price = 42 if a.model.startswith("2.5") else 28
    rec["cost"] = {"tokens": tok, "yuan": round(tok / 1e6 * price, 1), "draws_total": sum(len(s["draws"]) for s in rec["segments"])}
    rec["status"] = "ok"
    rec["finished"] = time.time()
    json.dump(rec, open(os.path.join(root, "run.json"), "w"), ensure_ascii=False, indent=1)
    tos.put_bytes(json.dumps(rec, ensure_ascii=False, indent=1).encode(), out_tos.join("run.json"))
    log(f"DONE {a.name}: cost={rec['cost']}")
    return rec


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    p.add_argument("--edit", required=True, help="要改什么,一句话;会套进 C 写法模板")
    p.add_argument("--model", default="2.5")
    p.add_argument("--seg-len", type=float, default=17.0)
    p.add_argument("--no-chain-prefix", dest="chain_prefix", action="store_false", help="接力段参考视频不再前接前段生成帧(默认前接 overlap 秒,纹理与轮廓在段边界延续,交叉淡入淡出无重影)")
    p.add_argument("--overlap", type=float, default=1.0, help="接力段与前段的重叠秒数:拼接时交叉淡入淡出 + 色调对齐,消段边界台阶;0 = 直接拼")
    p.add_argument("--max-draws", type=int, default=3)
    p.add_argument("--pick-draws", type=int, default=1, help="首段攒几张过门的抽卡再按材质质感择优(并排问 VLM);后段接力首段。默认 1 = 不择优")
    p.add_argument("--build-dataset", action="store_true")
    p.add_argument("--maskfree", action="store_true", help="(默认即是)不用几何档案的几何层:切点/锚帧/意图门/择优全靠 VLM 看整帧")
    p.add_argument("--occlusion-hint", action="store_true", help="实验:加\"被夹住/遮挡时露出的部分也按新材质画\"这句通用提示(实测对夹缝无效且多引脑补,默认不加)")
    p.add_argument("--chain-only", action="store_true", help="纯链式对照:不做任何几何层(不解析目标、等分切段、无锚帧/意图门)")
    p.add_argument("--geom", help="掩码档案模式:几何档案目录(geom_archive 产物,需 [mask] 依赖);切段、锚帧、意图门按掩码做")
    p.add_argument("--composite", action="store_true", help="兜底:掩码合成,保留区贴回原像素(默认关,已知磨圆棱角/盒子盖住/抹掉夹缝里的生成结果);需 --geom")
    p.add_argument("--no-vlm-gate", dest="vlm_gate", action="store_false", help="关掉 VLM 并排门(逐抽 + 全片复核);默认开")
    p.add_argument("--sliver-gate", action="store_true", help="夹缝露红门(目标被夹住/遮挡时露出的一角有没有被改),只检测记进 run.json;maskfree 下每秒一对帧问 VLM,掩码版走色相锥+编号确认;默认关")
    p.add_argument("--sliver-repair", action="store_true", help="实验:按 VLM 确认的碎片块做像素修补(已知会在同色线缆上误修、时间上闪烁,默认关)")
    a = p.parse_args(argv)
    run(a)


if __name__ == "__main__":
    main()
