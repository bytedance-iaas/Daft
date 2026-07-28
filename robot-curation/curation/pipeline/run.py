"""端到端管线装配(cli 的执行体):M1 → 漏斗 → 去重 → 画像 → 三件套交付。

三件套:① 清洗后数据集(LeRobot v3 + episode 级 parquet)② 质检报告(json+md)③ 技能画像(含于报告)。
"""
from __future__ import annotations

import json
import os


def check_entry(res: dict) -> dict:
    """检查结果 struct → 报告条目。detail 有就带(2026-07-27 U0 修正:旧逻辑只在
    弃权时保留 detail,被拒条目"为什么杀"的 VLM 证据全被丢弃——reject.json 里
    只剩"拒绝"二字,UI/人工裁决无据可查)。"""
    entry = {"passed": res.get("passed"), "score": res.get("score")}
    det = res.get("detail")
    if det and det != "{}":                   # 占位空 JSON 不进报告
        entry["detail"] = det
    return entry


def _try_build_vlm(cfg: dict):
    """按配置起 VLM 客户端;端点不可达 → 明确降级(task_success 跳过,报告里注明)。"""
    from ..pipeline.config import enabled

    if not enabled(cfg, "task_success"):
        return None, "task_success 在配置中关闭"
    try:
        from ..adapters.vlm_client import probe_endpoint, vlm_completion_from_config

        v = cfg["checks"]["task_success"]["vlm"]
        # 探活须带鉴权:托管端点(方舟)无 Bearer 头会 401,会被误判成"不可达"
        ok, why = probe_endpoint(v["endpoint"], v["model"],
                                 api_key_env=v.get("api_key_env"))
        if not ok:
            return None, f"{why},task_success 跳过"
        return vlm_completion_from_config(cfg), None
    except Exception as e:  # noqa: BLE001
        return None, f"VLM 端点不可达({type(e).__name__}),task_success 跳过"


def run_pipeline(
    config_path: str | None,
    input_dir: str,
    output_dir: str,
    embodiment_id: str | None = None,
    max_episodes: int | None = None,
    only_checks: str | None = None,
    skip_checks: str | None = None,
    report_only: bool = False,
    lite: bool = False,
    overwrite: bool = False,
    set_overrides: list | None = None,
    episode_indices: set[int] | None = None,   # 只跑指定 episode(CLI --episodes)
    vlm_backend: str | None = None,            # VLM 后端预设名(CLI --vlm-backend)
    vlm_endpoint: str | None = None,           # 直连端点(CLI --vlm-endpoint / env)
    vlm_model: str | None = None,              # 直连模型(CLI --vlm-model / env)
    vlm_api_key_env: str | None = None,        # 直连密钥环境变量名
) -> dict:
    from ..dataset_level.dedup import episode_fingerprint
    from ..dataset_level.profile import instruction_grouping_available, skill_profile
    from ..export.lerobot_writer import export_lerobot_v3
    from ..export.report import build_report, save_report
    from ..export.writers import write_episodes_parquet
    from ..ingest.daft_source import read_lerobot_lazy
    from ..ingest.lerobot_reader import _load_info, read_lerobot_meta, read_lerobot_rows
    from ..pipeline.config import enabled, load_config
    from ..pipeline.funnel import run_funnel
    from ..registry.registry import EmbodimentRegistry

    # 提前检查:输出目录已有上次交付物 → 立即拦(别做完漏斗+VLM才在导出时崩)。
    # overwrite=清理旧交付子目录后重跑;否则友好报错让用户换目录或加 --overwrite。
    from ..ingest.lerobot_reader import OutputExistsError
    _deliv = ("episodes_parquet", "lerobot_curated", "passed.json")
    _existing = [d for d in _deliv if os.path.exists(os.path.join(output_dir, d))]
    if _existing:
        if overwrite:
            import shutil
            for d in ("episodes_parquet", "lerobot_curated", "details"):
                p = os.path.join(output_dir, d)
                if os.path.isdir(p):
                    shutil.rmtree(p)
        else:
            raise OutputExistsError(
                f"输出目录 {output_dir} 已有上次运行的结果({', '.join(_existing)})。\n"
                "  换一个新的 --output 目录,或加 --overwrite 覆盖重跑。")
    os.makedirs(output_dir, exist_ok=True)
    cfg = load_config(config_path)
    if only_checks or skip_checks:
        from .config import apply_check_selection
        cfg = apply_check_selection(cfg, only=only_checks, skip=skip_checks)
    if vlm_backend:
        # backend 先于 --set:预设整组切换后,--set 仍可微调单项(后到者赢)
        from .config import apply_vlm_backend
        cfg = apply_vlm_backend(cfg, vlm_backend)
    if vlm_endpoint or vlm_model or vlm_api_key_env:
        # 直连参数在 backend 之后:可单用(免别名的正门),也可在预设之上单项覆盖
        from .config import apply_vlm_direct
        cfg = apply_vlm_direct(cfg, endpoint=vlm_endpoint, model=vlm_model,
                               api_key_env=vlm_api_key_env)
    if set_overrides:
        # --set 在 --only/--skip 之后应用(后者管开关,前者管任意值;后到者赢)
        from .config import apply_overrides, validate_config
        cfg = apply_overrides(cfg, set_overrides)
        validate_config(cfg, "--set 覆盖后")
    if not lite:
        # n_probe/帧问询并发未对齐 → 分波问询,单条耗时静默上升;开跑前一行点破
        from .config import probe_concurrency_hint
        _hint = probe_concurrency_hint(cfg)
        if _hint:
            print(_hint, flush=True)

    # ① 摄入(M1,懒扫描,2026-07-10):构造 DataFrame 零数据读取,数值 parquet 由
    # daft 引擎执行时按 task 流式拉取(ingest/daft_source);caption/报告所需上下文走
    # 轻量元数据(只读 meta 文件,万条秒级)。skip_missing=True:客户数据/下载缺口是
    # 常态,缺文件跳过并在 stderr 汇报(不崩、不静默),而非碰到一个缺失就整批失败。
    rows = read_lerobot_meta(input_dir, max_episodes=max_episodes,
                             episode_indices=episode_indices,
                             embodiment_id=embodiment_id, skip_missing=True)
    row_of = {r["episode_id"]: r for r in rows}
    # 身份行(2026-07-15 用户定):终端开跑即亮明数据集+机器人,不用等报告文件
    _info0 = _load_info(input_dir)
    _rt = str(_info0.get("robot_type") or "unknown")
    _emb = embodiment_id or _rt
    try:
        _prof0 = EmbodimentRegistry().get(_emb)
        _robot = {"robot_type": _rt, "embodiment_id": _emb,
                  "registry_profile": _prof0.embodiment_id, "quality": _prof0.quality}
        _rob_str = f"{_rt}(规格表 {_prof0.embodiment_id},质量 {_prof0.quality})"
    except Exception:  # noqa: BLE001
        _robot = {"robot_type": _rt, "embodiment_id": _emb,
                  "registry_profile": "(未注册)", "quality": None}
        _rob_str = f"{_rt}(未注册规格表)"
    print(f"[curation] 数据集: {os.path.basename(input_dir.rstrip('/'))} | "
          f"机器人: {_rob_str} | {len(rows)} 条", flush=True)

    # ② 漏斗(M4/M5)。默认满血:自动确保 VLM 服务(复用在线的/空闲卡自动拉起);
    #    --lite 精简版:跳过一切 VLM 环节,不碰 GPU。
    # VLM 的启用与 task_success **解耦**(2026-07-11):技能画像的 caption 路线同样
    # 需要 VLM——只 skip task_success 不应连带把画像降级成原始标注分组。
    sp_cfg = cfg.setdefault("skill_profile", {})
    sp_caption_on = (sp_cfg.get("enable", True)
                     and sp_cfg.get("method", "caption") == "caption")
    vlm_ready = False              # VLM 服务可用(画像 caption/审计的先决条件)
    if lite:
        cfg["checks"]["task_success"]["enable"] = False
        vlm, vlm_note = None, "精简版(--lite):跳过 VLM 环节"
    elif not enabled(cfg, "task_success") and not sp_caption_on:
        # 本次运行没有任何环节需要 VLM(如 --only visual_quality)→ 不碰 GPU
        vlm, vlm_note = None, "task_success 与技能画像均未启用,跳过 VLM"
    else:
        from ..adapters.vlm_server import ensure_vlm
        vcfg_e = cfg["checks"]["task_success"].get("vlm", {})
        ok, note = ensure_vlm(vcfg_e.get("endpoint", ""), vcfg_e.get("model", ""),
                              api_key_env=vcfg_e.get("api_key_env"))
        print(f"[curation] {note}", flush=True)
        vlm_ready = ok
        if enabled(cfg, "task_success"):
            vlm, vlm_note = _try_build_vlm(cfg)
            if vlm_note is None and not ok:
                vlm_note = note
        else:
            vlm, vlm_note = None, "task_success 未启用,跳过成败判定(VLM 仍供技能画像)"
    # 任务描述分层(2026-07-08 定):成败判定需要"意图"——有标注用标注;无标注用自产
    # caption 兜底(否则 VLM 无从判断,droid 全员弃权);来源留痕进判定 detail。
    auto_caps: dict = {}
    if vlm is not None:
        unlabeled = [r for r in rows if not (r.get("instruction") or "").strip()]
        if unlabeled:
            from ..dataset_level.caption import caption_episodes, make_vlm_captioner
            vcfg0 = cfg["checks"]["task_success"]["vlm"]
            capper = make_vlm_captioner(vcfg0["endpoint"], vcfg0["model"],
                                        api_key_env=vcfg0.get("api_key_env"))
            for r, c in zip(unlabeled, caption_episodes(
                    unlabeled, capper, n_frames=cfg.get("skill_profile", {}).get("n_frames", 8))):
                if c:
                    auto_caps[r["episode_id"]] = c
    desc_of, desc_src_of = {}, {}
    for r in rows:
        ins = (r.get("instruction") or "").strip()
        cap = auto_caps.get(r["episode_id"], "")
        r["task_desc"] = desc_of[r["episode_id"]] = ins or cap
        r["task_desc_source"] = desc_src_of[r["episode_id"]] = (
            "原始标注" if ins else ("自产caption" if cap else "无"))

    import daft as _daft

    @_daft.func
    def _lookup_desc(episode_id: str) -> str:
        return desc_of.get(episode_id, "")

    @_daft.func
    def _lookup_desc_src(episode_id: str) -> str:
        return desc_src_of.get(episode_id, "无")

    df0 = read_lerobot_lazy(input_dir, max_episodes=max_episodes,
                            episode_indices=episode_indices,
                            embodiment_id=embodiment_id)
    df0 = df0.with_column("task_desc", _lookup_desc(_daft.col("episode_id")))
    df0 = df0.with_column("task_desc_source", _lookup_desc_src(_daft.col("episode_id")))
    df, stats = run_funnel(df0, cfg, EmbodimentRegistry(), vlm_completion=vlm)
    check_cols = [c for c in df.column_names if c.startswith("check_")]
    _sel_cols = list(check_cols)
    if "_sync_curves" in df.column_names:
        _sel_cols.append("_sync_curves")
    _has_video = "video" in df.column_names
    if _has_video:
        _sel_cols.append("video")             # 指针 struct(KB 级),证据帧导出要用
    out = df.select("episode_id", "verdict", *_sel_cols).to_pydict()
    verdicts = {e: json.loads(v) for e, v in zip(out["episode_id"], out["verdict"])}
    videos_of = (dict(zip(out["episode_id"], out["video"])) if _has_video else {})
    # 逐 episode 结果(幸存者:全部检查的 passed/score)
    per_episode = {}
    for i, e in enumerate(out["episode_id"]):
        per_episode[e] = {"verdict": verdicts[e]["verdict"],
                          "soft_score": verdicts[e].get("soft_score"),
                          "reason": verdicts[e].get("reason", ""),
                          "undecidable": verdicts[e].get("undecidable", []),
                          "checks": {c.replace("check_", ""): check_entry(out[c][i])
                                     for c in check_cols if out[c][i] is not None}}
    # stuck 单列(二值,不进总分):统计被判 stuck 的 episode 数,进报告
    stuck_eps = []
    for i, e in enumerate(out["episode_id"]):
        mc = out.get("check_motion_quality", [None] * len(out["episode_id"]))[i]
        if mc is None:
            continue
        try:
            sj = json.loads(mc.get("detail") or "{}").get("stuck_joints")
        except Exception:  # noqa: BLE001
            sj = None
        if sj:
            stuck_eps.append(e)
    # 中途被硬门杀的:并入判决与逐条结果(episode 级留档)
    for k in stats.get("hard_killed", []):
        e = k["episode_id"]
        verdicts[e] = {"verdict": "drop", "reason": f"硬门违规: {k['check']}",
                       "hard_fails": [k["check"]], "soft_score": None}
        per_episode[e] = {"verdict": "drop", "soft_score": None,
                          "reason": f"硬门违规: {k['check']}",
                          "checks": {k["check"]: {"passed": False, "score": None,
                                                  "detail": k.get("detail", "")}}}

    # ③ keep 行 → 去重(M6)→ 画像(M7)
    # 两段式精确去重(2026-07-15):第一道只哈希 action 字节(便宜);action 撞车的
    # (罕见)才进第二道验视频内容——droid 实测全员视频指纹要哈希 200GB 磨十几分钟,
    # 两段式把视频哈希压到只剩撞车组。判重条件不变:action+视频内容都同才算重复。
    # 幸存者分批重读(每批 200),算完哈希即丢数值 → 内存有界。
    from ..dataset_level.dedup import action_hash
    keep_ids = [e for e, v in verdicts.items() if v["verdict"] == "keep"]
    dedup_on = cfg.setdefault("dedup", {}).get("enable", True)
    dedup_note = None
    _ah_first: dict = {}                # action_hash → 首见 episode_id
    _collide: dict = {}                 # action_hash → [episode_id...](含首见,≥2 才验视频)
    _order: list = list(keep_ids) if not dedup_on else []
    if not dedup_on:
        dedup_note = "去重未启用(--only/--skip 未选 dedup):交付中可能含字节级重复"
        print(f"[curation] {dedup_note}", flush=True)
    for _i0 in range(0, len(keep_ids) if dedup_on else 0, 200):
        if _i0 and _i0 % 1000 == 0:
            print(f"[curation] 去重指纹(第一道 action 哈希): {_i0}/{len(keep_ids)}",
                  flush=True)
        _chunk = {int(e[2:]) for e in keep_ids[_i0:_i0 + 200]}
        for _row in read_lerobot_rows(input_dir, episode_indices=_chunk,
                                      embodiment_id=embodiment_id,
                                      validate=False, skip_missing=True):
            _ah = action_hash(_row)
            _order.append(_row["episode_id"])
            if _ah in _ah_first:
                _collide.setdefault(_ah, [_ah_first[_ah]]).append(_row["episode_id"])
            else:
                _ah_first[_ah] = _row["episode_id"]
    dedup_dropped: list = []
    _dup_ids: set = set()
    if _collide:
        _n_c = sum(len(v) for v in _collide.values())
        print(f"[curation] 去重第二道:{len(_collide)} 组 action 撞车(共 {_n_c} 条),"
              "验视频内容", flush=True)
        for _ah, _eps in _collide.items():
            _idxs = {int(e[2:]) for e in _eps}
            _seen: dict = {}
            for _row in read_lerobot_rows(input_dir, episode_indices=_idxs,
                                          embodiment_id=embodiment_id,
                                          validate=False, skip_missing=True):
                _fp = episode_fingerprint(_row)      # 含视频内容哈希(只对撞车组)
                if _fp in _seen:
                    dedup_dropped.append({"episode_id": _row["episode_id"],
                                          "duplicate_of": _seen[_fp],
                                          "fingerprint": _fp[:16]})
                    _dup_ids.add(_row["episode_id"])
                else:
                    _seen[_fp] = _row["episode_id"]
    keep_ids = [e for e in _order if e not in _dup_ids]
    keep_rows = [row_of[e] for e in keep_ids]   # 轻量元数据行(视频指针/instruction/长度)

    label_audit = None
    profile_note = None
    if keep_rows and sp_caption_on and vlm_ready:
        # 技能画像终版:caption→LLM 归纳两级体系→画像+标注审计(8 次迭代定稿)
        from ..adapters.vlm_client import make_llm_ask
        from ..dataset_level.audit import audit_labels
        from ..dataset_level.caption import caption_episodes, make_vlm_captioner
        from ..dataset_level.profile import skill_profile_two_level
        from ..dataset_level.taxonomy import assign, induce_taxonomy

        vcfg = cfg["checks"]["task_success"]["vlm"]
        captioner = make_vlm_captioner(vcfg["endpoint"], vcfg["model"],
                                       api_key_env=vcfg.get("api_key_env"))
        llm_ask = make_llm_ask(vcfg["endpoint"], vcfg["model"],
                               api_key_env=vcfg.get("api_key_env"))

        # 进度:本段是"1 个逐条长循环 + 4 个离散 LLM 步",故两种显示混用——
        # caption 有明确总数 → 条目式进度条;后四步各是一次 LLM 大调用,既无可数单位
        # 又不可预测耗时 → 阶段式只报"第几步/在做什么"。给不可预测的步骤编百分比是
        # 骗人:卡住时用户还以为在动。详见 pipeline/progress.py 的模块注释。
        import time as _t

        from .progress import _progress_init, _progress_tick, phase_step
        _sp_t0 = _t.time()
        _G = "技能画像"
        _pk_cap = _progress_init("caption", len(keep_rows), f"{_G}·逐条 caption",
                                 quiet_before_s=3.0)
        _cap_conc = int(sp_cfg.get("caption_concurrency", 8))
        phase_step(_G, 1, 5, f"逐条 caption({len(keep_rows)} 条,并发 {_cap_conc})…", _sp_t0)
        caps = caption_episodes(keep_rows, captioner,
                                n_frames=sp_cfg.get("n_frames", 8),
                                precomputed=auto_caps,
                                on_progress=lambda: _progress_tick(_pk_cap),
                                max_concurrency=_cap_conc)
        phase_step(_G, 2, 5, "归纳技能体系(LLM)…", _sp_t0)
        taxonomy = induce_taxonomy(caps, llm_ask,
                                   guideline=sp_cfg.get("taxonomy_guideline"))
        # 自查裁判回合:LLM 对照判据审自己的分类,合并"按目的地/物体分"的违规类
        from ..dataset_level.taxonomy import refine_taxonomy
        phase_step(_G, 3, 5, "自查裁判:按判据复核分类(LLM)…", _sp_t0)
        taxonomy = refine_taxonomy(taxonomy, llm_ask,
                                   guideline=sp_cfg.get("taxonomy_guideline"))
        fams, subs = assign(caps, taxonomy)
        # 补漏回合:LLM 抄 members 会漏(实测),漏网 caption 二次指认到既有子技能
        missed = sorted({c for c, f in zip(caps, fams) if f == "未归类" and c.strip()})
        if missed:
            from ..dataset_level.taxonomy import repair_unassigned
            phase_step(_G, 4, 5, f"补漏:{len(missed)} 条未归类重新指认(LLM)…", _sp_t0)
            fix = repair_unassigned(missed, taxonomy, llm_ask)
            for i, c in enumerate(caps):
                if fams[i] == "未归类" and c in fix:
                    fams[i], subs[i] = fix[c]
        else:
            # 没漏网也要报,否则用户看到 3/5 直接跳 5/5 会以为漏了一步或出错
            phase_step(_G, 4, 5, "补漏:无未归类,跳过", _sp_t0)
        phase_step(_G, 5, 5, "汇总画像 + 标注审计", _sp_t0)
        profile = skill_profile_two_level(keep_rows, fams, subs, caps)
        # 判据留痕(2026-07-11):guideline + LLM 自述的归类理由进报告,分类可审计
        from ..dataset_level.taxonomy import DEFAULT_GUIDELINE, criteria_of
        fam_c, sub_c = criteria_of(taxonomy)
        profile["guideline"] = (sp_cfg.get("taxonomy_guideline") or DEFAULT_GUIDELINE).strip()
        for fname, f in profile.get("families", {}).items():
            if fam_c.get(fname):
                f["criterion"] = fam_c[fname]
            for sname, s in f.get("subskills", {}).items():
                if sub_c.get((fname, sname)):
                    s["criterion"] = sub_c[(fname, sname)]
        label_audit = audit_labels([r["episode_id"] for r in keep_rows],
                                   [r.get("instruction", "") for r in keep_rows],
                                   caps, fams, taxonomy, llm_ask)
    elif keep_rows and not sp_cfg.get("enable", True):
        # 用户主动未选画像(--only 其它模块 / --skip skill_profile)→ 不画像,如实注明
        profile = {"n_episodes": len(keep_rows), "n_skills": 0, "skills": {},
                   "undersampled": []}
        profile_note = "技能画像未启用(--only/--skip 未选 skill_profile)"
    elif keep_rows and instruction_grouping_available(keep_rows):
        # 降级:按原始标注分组(未经审计,报告注明)——仅当画像被要求但 VLM 不可用
        skills = [r["instruction"].strip() or "(无指令)" for r in keep_rows]
        profile = skill_profile(keep_rows, skills)
        if sp_caption_on:
            profile_note = "技能画像降级:VLM 不可用,按原始标注分组(碎片化、未经审计)"
            print(f"[curation] ⚠️ {profile_note}", flush=True)
    else:
        profile = skill_profile(keep_rows, ["(未分组)"] * len(keep_rows)) if keep_rows else {
            "n_episodes": 0, "n_skills": 0, "skills": {}, "undersampled": []}

    # ④ 交付三件套
    report = build_report(verdicts, stats, dedup_dropped, profile, config_path or "default")
    # 数据集身份:开跑时已解析(_robot),报告开头列明
    # 出生证(2026-07-15):旧输出文件三次误导用户("怎么没变/怎么全弃权")——
    # 生成时间+代码版本盖章,新旧一眼可辨
    import datetime
    import subprocess
    try:
        _ver = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5,
                              cwd=os.path.dirname(os.path.abspath(__file__))
                              ).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        _ver = "unknown"
    report = {"数据集": os.path.basename(input_dir.rstrip("/")), "机器人": _robot,
              "生成时间": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "代码版本": _ver, **report}
    vq = cfg["checks"].get("visual_quality", {})
    report["dataset"]["visual_qc_params"] = {
        "blur_ref_var": vq.get("params", {}).get("blur_ref_var", 100.0),
        "frame_max_side": cfg.get("pipeline", {}).get("frame_max_side", 448),
        "camera_weights": vq.get("camera_weights", {}) or "全部默认 1.0",
        "aggregation": "加权平均(占位黑帧路除外)"}
    from ..ingest.validate import stats_prior_warnings
    warns = stats_prior_warnings(input_dir)
    if warns:
        report["dataset"]["ingest_warnings"] = warns
    if keep_rows:
        from ..adapters.decode import probe_live_cameras
        cam_audit = {}
        for r in keep_rows:
            pr = probe_live_cameras(r["video"])
            if pr["dead_or_padded"]:
                cam_audit[r["episode_id"]] = pr
        if cam_audit:
            report["dataset"]["camera_audit_note"] = (
                f"{len(cam_audit)}/{len(keep_rows)} 条存在占位/黑帧相机通道(详见 episodes)")
            report["episodes"]["camera_audit"] = cam_audit
    if vlm_note:
        report["dataset"]["task_success_note"] = vlm_note
    if profile_note:
        report["dataset"]["skill_profile_note"] = profile_note
    if dedup_note:
        report["dataset"]["dedup_note"] = dedup_note
    if label_audit is not None:
        report["label_audit"] = label_audit
    report["episodes"]["results"] = per_episode
    # 操作流畅度汇总(episode 级明细在 motion detail/CSV;此处给数据集级视图)
    _flu = []
    for i, e in enumerate(out["episode_id"]):
        mc = out.get("check_motion_quality", [None] * len(out["episode_id"]))[i]
        if mc is None:
            continue
        try:
            _d = json.loads(mc.get("detail") or "{}")
            fl_v, ar_v = _d.get("fluency"), _d.get("active_ratio")
        except Exception:  # noqa: BLE001
            fl_v = ar_v = None
        if ar_v is not None:
            _flu.append((e, fl_v, float(ar_v)))
    if _flu:
        import numpy as _np2
        _by_fl = sorted([x for x in _flu if x[1] is not None], key=lambda x: x[1])
        report["dataset"]["operator_fluency"] = {
            # 两个口径(2026-07-14 用户定):fluency=执行窗内犹豫(操作技能,头尾空闲
            # 不算);active_ratio=全程有效占比(录制卫生/修剪价值)
            "avg_fluency": (round(float(_np2.mean([f for _, f, _ in _by_fl])), 4)
                            if _by_fl else None),
            "avg_active_ratio": round(float(_np2.mean([a for _, _, a in _flu])), 4),
            "note": "fluency=执行窗(首尾空闲掐除)内的非停顿占比,反映操作技能;"
                    "active_ratio=全程有效动作占比,反映录制卫生(头尾该修剪多少)。"
                    "只报不罚,明细见 details/motion_details.csv",
            "worst_episodes": [{"episode": e, "fluency": round(f, 3),
                                "active_ratio": round(a, 3)}
                               for e, f, a in _by_fl[:10]]}
    report["dataset"]["stuck"] = {
        "flagged_episodes": len(stuck_eps),
        "note": "一次卡死事件一行(包络重叠的轴合并,如 y+z)。口径:stuck=指令在推而"
                "不动(定罪证据);freeze_total=连续静止全窗(视频观感);两者之差=idle"
                "(无指令静止),逐段剧本见 details/stuck_details.json 的 timeline。"
                "二值判定,不进运动总分",
        "episodes": stuck_eps[:200]}
    # 汇总统计:通过率/平均分/各检查的过-杀-弃权
    import numpy as _np
    softs = [v["soft_score"] for v in per_episode.values() if v.get("soft_score") is not None]
    per_check = {}
    for pe in per_episode.values():
        for name, c in pe["checks"].items():
            s = per_check.setdefault(name, {"pass": 0, "fail": 0, "abstain": 0,
                                            "scored": 0, "scores": []})
            if c["passed"] is False:
                s["fail"] += 1
            elif c["passed"] is True:
                s["pass"] += 1
            elif c["score"] is not None:
                s["scored"] += 1     # 软分检查:设计上不投票(passed=None),有分≠弃权
            else:
                s["abstain"] += 1    # 真弃权:无判无分(信号不足/语义不符,detail 有原因)
                try:                 # 弃权原因聚合(2026-07-14 用户要求:汇总层可见)
                    _rsn = json.loads(c.get("detail") or "{}").get("reason", "")[:80]
                except Exception:  # noqa: BLE001
                    _rsn = ""
                if _rsn:
                    s.setdefault("abstain_reasons", {})
                    s["abstain_reasons"][_rsn] = s["abstain_reasons"].get(_rsn, 0) + 1
            if c["score"] is not None:
                s["scores"].append(c["score"])
    report["dataset"]["summary_stats"] = {
        "pass_rate_pct": round(100.0 * len([v for v in per_episode.values()
                                            if v["verdict"] == "keep"]) / max(len(per_episode), 1), 2),
        "avg_soft_score": round(float(_np.mean(softs)), 4) if softs else None,
        "per_check": {n: {"pass": s["pass"], "fail": s["fail"], "scored": s["scored"],
                          "abstain": s["abstain"],
                          **({"abstain_reasons": dict(sorted(
                              s["abstain_reasons"].items(), key=lambda x: -x[1])[:3])}
                             if s.get("abstain_reasons") else {}),
                          "avg_score": round(float(_np.mean(s["scores"])), 4) if s["scores"] else None}
                      for n, s in per_check.items()}}
    jp, mp = save_report(report, output_dir)

    # 明细表(表格格式交付件):运动逐子项 / 视觉逐路相机
    import csv as _csv
    det_dir = os.path.join(output_dir, "details")
    os.makedirs(det_dir, exist_ok=True)
    # 生效配置快照(2026-07-27 U0):UI/复盘要能回答"这次到底用了什么阈值/后端"。
    # 配置里只有 env 变量名(api_key_env),无密钥,可安心入交付件。
    report["config_effective"] = cfg
    # task_success 证据帧(2026-07-27 U0):被拒/待裁决条目的 probe 帧落 JPEG——
    # detail 里只有帧号,没图人工没法裁决。导出期重解码,不碰漏斗并发路径。
    _ev_mode = str(cfg.get("pipeline", {}).get("evidence_frames", "flagged"))
    if _ev_mode != "off" and videos_of:
        from ..export.evidence import render_task_evidence
        _pcfg2 = cfg.get("pipeline", {})
        _ev = render_task_evidence(
            per_episode, videos_of, os.path.join(det_dir, "evidence"),
            interval=_pcfg2.get("frame_sample_interval_s", 0.5),
            max_side=_pcfg2.get("frame_max_side", 448), mode=_ev_mode)
        if _ev:
            report["dataset"]["task_evidence"] = {
                "episodes": len(_ev), "帧数": sum(len(v) for v in _ev.values()),
                "目录": "details/evidence/",
                "note": "task_success 拒绝/待裁决条目的 VLM probe 帧"
                        "(配置 pipeline.evidence_frames: flagged|all|off)"}
    M_COLS = ["smoothness", "spike", "stuck", "gripper_jitter", "actuator_saturation",
              "joint_stability", "path_efficiency", "fluency", "active_ratio",
              "idle_head_s", "idle_tail_s", "idle_mid_count", "idle_mid_total_s",
              "spike_isolation", "saturation_gap_ratio", "tail_std", "gripper_flips"]
    mrows, vrows, srows = [], [], []
    stuck_json: dict = {}          # episode → 事件+timeline(权威完整版,JSON)
    for i, e in enumerate(out["episode_id"]):
        for c in check_cols:
            if out[c][i] is None:
                continue
            try:
                d = json.loads(out[c][i].get("detail") or "{}")
            except Exception:  # noqa: BLE001
                continue
            if c == "check_motion_quality":
                mrows.append({"episode": e, "score": out[c][i].get("score"),
                              **{k: d.get(k) for k in M_COLS}})
                # stuck 明细(二值,不进总分)。三个概念分列(2026-07-15 用户定):
                # stuck=指令在推而不动(证据段,定罪依据);idle=环绕的无指令静止
                # (操作员停手);总冻结窗=两者之和(视频观感)。包络重叠的轴合并为
                # **同一次事件**一行(手腕卡死时 y/z 差一帧先后冻结,分行会被误读成
                # "两个开始时间差0.1秒"——同一事件,轴列写 y+z)。
                sjs = d.get("stuck_joints") or []
                _lc = d.get("stuck_low_confidence") or []
                if sjs or _lc:
                    r0 = row_of.get(e, {})
                    fps = float(r0.get("fps") or 0) or 1.0
                    T = int(r0.get("length") or 0)
                    vids = r0.get("video") or {}
                    vid = (os.path.basename(vids[sorted(vids)[0]].get("path", ""))
                           if vids else "")
                    from ..dataset_level.stuck_events import build_stuck_events
                    events = build_stuck_events(sjs, fps)
                    stuck_json[e] = {
                        "frozen_axes": sorted({str(s.get("axis")) for s in sjs}),
                        "video_file": vid, "total_frames": T,
                        "events": events}
                    if _lc:   # <1.5s 的低置信事件:只记录不定罪(负载暂态/贴线证据)
                        stuck_json[e]["low_confidence_events"] = build_stuck_events(_lc, fps)
                    if not sjs:
                        stuck_json[e]["frozen_axes"] = []
                    for ev in events:
                        srows.append({"episode": e, "axes": "+".join(ev["axes"]),
                                      **{k: ev[k] for k in (
                                          "stuck_start_sec", "stuck_seconds",
                                          "freeze_start_sec", "freeze_total_seconds")},
                                      "total_frames": T, "video_file": vid})
            if c == "check_visual_quality":
                for cam, cd in (d.get("per_camera_detail") or {}).items():
                    vrows.append({"episode": e, "camera": cam.split(".")[-1],
                                  "status": "OK", **cd})
                for cam in d.get("padded_channels") or []:
                    vrows.append({"episode": e, "camera": cam.split(".")[-1],
                                  "status": "PAD"})
    if mrows:
        with open(os.path.join(det_dir, "motion_details.csv"), "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["episode", "score"] + M_COLS)
            w.writeheader()
            w.writerows(mrows)
    if vrows:
        vcols = ["episode", "camera", "status", "score", "sharpness", "exposure",
                 "integrity", "blur_var_median", "clip_frac_median",
                 "gray_std_median", "frozen_ratio"]
        with open(os.path.join(det_dir, "visual_details.csv"), "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=vcols, extrasaction="ignore")
            w.writeheader()
            w.writerows(vrows)
    # 运动学违规明细(2026-07-14 用户定):被拒必须能定位;硬杀发生在漏斗中途,
    # 明细从 stats.hard_killed 提取(幸存者不会有运动学违规——硬门语义)
    krows = []
    for k in stats.get("hard_killed", []):
        if k.get("check") != "kinematic_limits":
            continue
        try:
            kdet = json.loads(k.get("detail") or "{}")
        except Exception:  # noqa: BLE001
            kdet = {}
        for x in (kdet.get("violations") or []):
            krows.append({"episode": k["episode_id"], "type": x.get("type"),
                          "joint": x.get("joint"), "frame": x.get("frame"),
                          "value": x.get("value"), "limit": str(x.get("limit"))})
        if not kdet.get("violations") and kdet.get("reason"):
            krows.append({"episode": k["episode_id"], "type": "format_or_other",
                          "joint": "", "frame": "", "value": "",
                          "limit": str(kdet.get("reason"))[:80]})
    with open(os.path.join(det_dir, "kinematic_details.csv"), "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["episode", "type", "joint", "frame",
                                           "value", "limit"], extrasaction="ignore")
        w.writeheader()
        w.writerows(krows)
    # stuck 明细(二值,不进总分):总是生成——无 stuck 时是"只有表头的空文件",
    # 明确表示"查了、0 条命中"(与 motion/visual details 行为一致,2026-07-10 用户定)
    # 同步曲线证据图(证据附件第一块):漏斗暂存的曲线 → details/plots/*.png
    if out.get("_sync_curves"):
        from ..export.sync_plots import render_sync_plots
        _crows = [(e, cj) for e, cj in zip(out["episode_id"], out["_sync_curves"]) if cj]
        _pngs = render_sync_plots(_crows, os.path.join(det_dir, "plots"))
        if _crows:
            report["dataset"]["sync_plots"] = {
                "生成": len(_pngs), "目录": "details/plots/",
                "note": ("每张=光流曲线vs速度曲线+滞后扫描;默认只画非'过'条目"
                         "(配置 pipeline.sync_plots: flagged|all|off)"
                         + ("" if _pngs or not _crows else ";matplotlib 缺失,未渲染"))}
            save_report(report, output_dir)     # 报告已写过,补写含图信息的版本
    srows.sort(key=lambda x: -(x["stuck_seconds"] or 0))
    scols = ["episode", "axes", "stuck_start_sec", "stuck_seconds",
             "freeze_start_sec", "freeze_total_seconds",
             "total_frames", "video_file"]
    with open(os.path.join(det_dir, "stuck_details.csv"), "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=scols, extrasaction="ignore")
        w.writeheader()
        w.writerows(srows)
    # stuck_details.json(2026-07-15 用户定):嵌套真相的权威版——每 episode 一条,
    # 事件内按时间顺序给 idle/stuck 剧本(秒+帧双标);CSV 是同源的摘要版
    with open(os.path.join(det_dir, "stuck_details.json"), "w") as f:
        json.dump({"数据集": report.get("数据集"), "机器人": report.get("机器人"),
                   "口径": "stuck=指令在推而不动(各轴证据窗并集);idle=静止且指令未推;"
                          "一次事件=包络重叠的轴段合并;timeline 按时间顺序",
                   "episodes": stuck_json}, f, ensure_ascii=False, indent=1)

    deliver = {"passed_json": jp,
               "reject_json": jp.replace("passed.json", "reject.json"),
               "review_json": jp.replace("passed.json", "review.json"),
               "report_md": mp,
               "details_dir": os.path.join(output_dir, "details")}
    if keep_rows and not report_only:
        # 导出需要数值列 → 只按需重读最终幸存者(QC 一遍全程未整批驻留数值)
        keep_full = read_lerobot_rows(
            input_dir, episode_indices={int(e[2:]) for e in keep_ids},
            embodiment_id=embodiment_id, skip_missing=True)
        deliver["episodes_parquet"] = write_episodes_parquet(
            keep_full, os.path.join(output_dir, "episodes_parquet"))
        from ..ingest.lerobot_reader import _load_info

        if _load_info(input_dir)["codebase_version"].startswith("v3"):
            keep_src_idx = [int(e.replace("ep", "")) for e in keep_ids]
            deliver["lerobot_dataset"] = export_lerobot_v3(
                input_dir, keep_src_idx,
                os.path.join(output_dir, "lerobot_curated"))["out_dir"]
        else:
            report["dataset"]["lerobot_export_note"] = "v2 源的 LeRobot 导出在 V1(先交付 parquet)"
            save_report(report, output_dir)

    return {"stats": stats, "verdicts": verdicts, "deliverables": deliver,
            "n_delivered": len(keep_rows),
            "dataset_name": os.path.basename(input_dir.rstrip("/")), "robot": _robot}
