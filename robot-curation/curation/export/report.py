"""质检报告(文档中的 M8):数据集级 + episode 级 → json + markdown。

对账原则:报告里的每个计数都直接从判决/去重/画像数据算出,测试验证口径一致。
"""
from __future__ import annotations

import json


def build_report(
    verdicts: dict[str, dict],          # episode_id -> episode_verdict() 的输出
    funnel_stats: dict,                 # run_funnel 的 stats
    dedup_dropped: list[dict],
    profile: dict,                      # skill_profile() 的输出
    config_path: str = "",
) -> dict:
    keeps = [e for e, v in verdicts.items() if v["verdict"] == "keep"]
    drops = {e: v for e, v in verdicts.items() if v["verdict"] == "drop"}
    hard_reasons: dict[str, int] = {}
    for v in drops.values():
        for name in v.get("hard_fails", []):
            hard_reasons[name] = hard_reasons.get(name, 0) + 1

    return {
        "dataset": {
            "input_episodes": funnel_stats.get("input"),
            "hard_gate_filtered": funnel_stats.get("input", 0) - funnel_stats.get("output", 0),
            "verdict_keep": len(keeps),
            "verdict_drop": len(drops),
            "dedup_removed": len(dedup_dropped),
            "delivered": len(keeps) - len(dedup_dropped),
            "hard_fail_breakdown": hard_reasons,
            "funnel_stats": funnel_stats,
        },
        "skills": profile,
        "episodes": {
            "kept": keeps,
            "dropped": {e: {"reason": v["reason"], "soft_score": v.get("soft_score")}
                        for e, v in drops.items()},
            "duplicates": dedup_dropped,
        },
        "config": config_path,
    }


def to_markdown(report: dict) -> str:
    d = report["dataset"]
    rb = report.get("机器人") or {}
    _rb_line = ""
    if rb:
        _rb_line = (f"- **机器人型号**: {rb.get('robot_type')}"
                    f"(规格表: {rb.get('registry_profile')}"
                    f"{',质量 ' + str(rb.get('quality')) if rb.get('quality') else ''})")
    lines = [
        "# 数据集质检报告",
        f"- **数据集**: {report.get('数据集', '(未知)')}",
        *([_rb_line] if _rb_line else []),
        f"- 生成时间: {report.get('生成时间', '?')} | 代码版本: {report.get('代码版本', '?')}",
        # 数据集注记(2026-07-29):数据集 profile 的 extras.note,读数字前必须知道的
        # 前提(如 bridge 的 state 由 action 累加合成 → stuck 只能弃权)。有才出这行
        *([f"- **数据集注记**: {d['dataset_note']}"] if d.get("dataset_note") else []),
        "",
        "## 总览",
        f"- 输入 episode:{d['input_episodes']}",
        f"- 硬门拦截(漏斗中途淘汰):{d['hard_gate_filtered']}",
        f"- 判决 keep / drop:{d['verdict_keep']} / {d['verdict_drop']}",
        f"- 精确去重删除:{d['dedup_removed']}"
        + (f"({d['dedup_note']})" if d.get("dedup_note") else ""),
        f"- **交付:{d['delivered']} 条**",
        "",
    ]
    ss = d.get("summary_stats")
    if ss:
        lines.append("## 汇总统计")
        lines.append(f"- 通过率: {ss['pass_rate_pct']}%  |  平均软分: {ss['avg_soft_score']}")
        for n, s in ss["per_check"].items():
            if s["avg_score"] is not None:                      # 软分检查:只报均分
                lines.append(f"- {n}(软分): 均分 {s['avg_score']}")
            else:                                               # 硬门:报过/杀/弃权
                lines.append(f"- {n}(硬门): 过{s['pass']} 杀{s['fail']} 弃权{s['abstain']}")
                for rsn, cnt in (s.get("abstain_reasons") or {}).items():
                    lines.append(f"  - 弃权原因: {rsn}({cnt} 条)")
        lines.append("")
    lat = report.get("dataset", {}).get("vlm_latency")
    if lat:
        # 延时档案(2026-07-28):客户端视角 = 网络+服务端排队+推理。四类调用体质
        # 不同分开报;逐请求明细在 details/vlm_latency.csv。
        _cn = {"probe": "渐变问询(VOC)", "endstate": "二值复核",
               "caption": "画像 caption", "llm": "归纳/审计 LLM"}
        lines.append("## 模型调用延时(客户端视角,秒)")
        lines.append("| 调用类型 | 次数 | 错误 | 均值 | P50 | P90 | P99 | 最大 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for tag, s in lat.items():
            if s.get("n"):
                lines.append(f"| {_cn.get(tag, tag)} | {s['n']} | {s['errors']} | "
                             f"{s['mean_s']} | {s['p50_s']} | {s['p90_s']} | "
                             f"{s['p99_s']} | {s['max_s']} |")
            else:
                lines.append(f"| {_cn.get(tag, tag)} | 0 | {s['errors']} | - | - | - | - | - |")
        lines.append("")
    results = report["episodes"].get("results")
    if results:
        names = sorted({n for pe in results.values() for n in pe["checks"]})
        lines.append(f"## 每 episode 结果({len(results)} 条)")
        lines.append("| episode | 判决 | 软分 | " + " | ".join(names) + " |")
        lines.append("|" + "---|" * (3 + len(names)))
        for e in sorted(results):
            pe = results[e]
            cells = []
            for n in names:
                c = pe["checks"].get(n)
                if c is None:
                    cells.append("-")
                elif c["score"] is not None:
                    cells.append(f"{c['score']:.2f}")
                else:
                    cells.append({True: "过", False: "杀", None: "弃权"}[c["passed"]])
            soft = f"{pe['soft_score']:.2f}" if pe.get("soft_score") is not None else "-"
            lines.append(f"| {e} | {pe['verdict']} | {soft} | " + " | ".join(cells) + " |")
        lines.append("")
    vp = d.get("visual_qc_params")
    if vp:
        lines.append("## 视觉质检参数(可在配置覆盖)")
        lines.append(f"- 清晰度参考线 blur_ref_var: {vp['blur_ref_var']}(绑定分辨率标尺 "
                     f"frame_max_side={vp['frame_max_side']})")
        lines.append(f"- 相机权重: {vp['camera_weights']};总分聚合: {vp['aggregation']}")
        lines.append("")
    if d["hard_fail_breakdown"]:
        lines.append("## 硬门违规分布")
        for name, n in sorted(d["hard_fail_breakdown"].items(), key=lambda x: -x[1]):
            lines.append(f"- {name}: {n}")
        lines.append("")
    fl = d.get("operator_fluency")
    if fl:
        lines.append("## 操作流畅度(只报不罚)")
        if fl.get("avg_fluency") is not None:
            lines.append(f"- 平均流畅度: {fl['avg_fluency']*100:.1f}%"
                         "(执行窗内非停顿占比=操作技能;头尾空闲不计入)")
        lines.append(f"- 平均有效动作占比: {fl['avg_active_ratio']*100:.1f}%"
                     "(全程口径=录制卫生;头尾空闲该修剪的部分在此扣分)")
        worst = [w for w in fl.get("worst_episodes", []) if w.get("fluency", 1) < 0.9]
        if worst:
            lines.append("- 执行中犹豫最多(建议操作反馈): " + ", ".join(
                f"{w['episode']}(流畅{w['fluency']*100:.0f}%/有效{w['active_ratio']*100:.0f}%)"
                for w in worst[:8]))
        lines.append("")
    st = d.get("stuck")
    if st:
        lines.append("## 执行器卡死(stuck,单列/不进总分)")
        lines.append(f"- 被判 stuck 的 episode: {st['flagged_episodes']} 条"
                     "(指令在动、实际冻结;二值判定;明细见 details/stuck_details.csv)")
        if st.get("episodes"):
            shown = ", ".join(st["episodes"][:20])
            more = " …" if st["flagged_episodes"] > 20 else ""
            lines.append(f"- 列表: {shown}{more}")
        lines.append("")
    sk = report["skills"]
    if sk and sk.get("families"):
        lines.append(f"## 技能分布画像(两级,{sk['n_families']} 族,{sk['n_episodes']} 条)")
        if sk.get("guideline"):
            lines.append("> 分类判据(可配置 skill_profile.taxonomy_guideline;"
                         "类数由数据在判据下涌现,不设数字范围):")
            for gl in sk["guideline"].splitlines():
                lines.append(f"> {gl}")
        def _crit(d):                        # 判据说明:LLM 偶尔罗嗦,报告里截断保可读
            c = str(d.get("criterion") or "").strip()
            return f"  ——{c[:120]}{'…' if len(c) > 120 else ''}" if c else ""

        for name, f in sorted(sk["families"].items(), key=lambda x: -x[1]["count"]):
            lines.append(f"- **{name}**: {f['count']} 条({f['pct']:.2f}%){_crit(f)}")
            for sub, s in sorted(f["subskills"].items(), key=lambda x: -x[1]["count"]):
                lines.append(f"  - {sub}: {s['count']} 条({s['pct']:.2f}%){_crit(s)}")
                for lab in s.get("raw_labels_top", [])[:2]:
                    lines.append(f"    - [原始标注] {lab}")
        if sk.get("undersampled"):
            lines.append(f"- ⚠️ 欠采样技能族: {', '.join(sk['undersampled'])}")
        lines.append("")
    elif sk and sk.get("skills"):
        lines.append(f"## 技能分布画像({sk['n_skills']} 类,{sk['n_episodes']} 条;按原始标注分组,未经审计)")
        for name, s in sorted(sk["skills"].items(), key=lambda x: -x[1]["count"]):
            lines.append(f"- {name}: {s['count']} 条({s['pct']}%),平均时长 {s['avg_len_s']}s")
        if sk.get("undersampled"):
            lines.append(f"- ⚠️ 欠采样技能: {', '.join(sk['undersampled'])}")
        lines.append("")
    la = report.get("label_audit")
    if la and (la["high"] or la["mid_for_review"]):
        lines.append(f"## 标注审计(高置信 {len(la['high'])} / 人工复核 {len(la['mid_for_review'])})")
        for f in la["high"][:20]:
            lines.append(f"- [高] {f['id']} {f['reason']}: 标注「{f['label'][:40]}」"
                         f" vs 画面「{f['caption'][:40]}」")
        lines.append("")
    dropped = report["episodes"]["dropped"]
    if dropped:
        lines.append("## 淘汰明细(episode 级)")
        results = report["episodes"].get("results") or {}
        for e, v in list(dropped.items())[:50]:
            lines.append(f"- {e}: {v['reason']}")
            # 证据直出(2026-07-14 用户定):被拒必须当场看到哪里错,不必翻 reject.json
            for cname, c in (results.get(e, {}).get("checks") or {}).items():
                if c.get("passed") is not False or not c.get("detail"):
                    continue
                try:
                    det = (json.loads(c["detail"]) if isinstance(c["detail"], str)
                           else c["detail"]) or {}
                except Exception:  # noqa: BLE001
                    det = {}
                for x in (det.get("violations") or [])[:3]:
                    lines.append(f"  - 证据[{cname}]: {x.get('type')} 关节{x.get('joint')}"
                                 f" 帧{x.get('frame')} 值={x.get('value')} 限={x.get('limit')}")
                nv = det.get("n_violations", 0)
                if nv > 3:
                    lines.append(f"  - …共 {nv} 处违规(全量见 details/kinematic_details.csv"
                                 " / reject.json)")
                if det.get("reason") and not det.get("violations"):
                    lines.append(f"  - 证据[{cname}]: {str(det['reason'])[:110]}")
        if len(dropped) > 50:
            lines.append(f"- …(共 {len(dropped)} 条,全量见 json)")
    return "\n".join(lines)


CHECK_CN = {"timestamp_check": "时间戳检查", "kinematic_limits": "运动学极限",
            "motion_quality": "运动质量", "visual_quality": "视觉质量",
            "video_action_sync": "视频-动作同步", "task_success": "任务成败判定"}


def _render_checks(checks: dict) -> dict:
    """passed 三态 → 人话:true='pass',false='拒绝',null='弃权'。"""
    out = {}
    for name, c in checks.items():
        if c.get("passed") is None and c.get("score") is not None:
            state = "软分"                      # 软分检查不投票,只打分
        else:
            state = {True: "pass", False: "拒绝", None: "弃权"}[c.get("passed")]
        entry = {"结果": state}
        if c.get("score") is not None:
            entry["score"] = round(c["score"], 4)
        if c.get("detail"):
            entry["detail"] = c["detail"]
        out[CHECK_CN.get(name, name)] = entry
    return out


def save_report(report: dict, out_dir: str) -> tuple[str, str]:
    """交付三文件:passed.json(通过条目) / reject.json(被拒条目+中文原因+证据) / report.md。"""
    import os

    os.makedirs(out_dir, exist_ok=True)
    results = report.get("episodes", {}).get("results", {})

    ds_name = report.get("数据集", "")
    passed_view = {k: v for k, v in report.items() if k != "episodes"}
    passed_view["episodes"] = {
        e: {"判决": "通过", "综合软分": pe.get("soft_score"),
            "checks": _render_checks(pe.get("checks", {}))}
        for e, pe in sorted(results.items()) if pe.get("verdict") == "keep"}

    reject_view = {}
    for e, pe in sorted(results.items()):
        if pe.get("verdict") != "drop":
            continue
        reason = pe.get("reason", "")
        for en, cn in CHECK_CN.items():
            reason = reason.replace(en, f"「{cn}」")
        reject_view[e] = {"判决": "拒绝", "原因": reason or "未注明",
                          "综合软分": pe.get("soft_score"),
                          "checks": _render_checks(pe.get("checks", {}))}
    for dup in report.get("episodes", {}).get("duplicates", []):
        e = dup.get("episode_id", str(dup))
        reject_view[e] = {"判决": "拒绝(去重)",
                          "原因": f"与 {dup.get('duplicate_of', '另一条')} 字节级完全重复",
                          "checks": {}}

    legacy = os.path.join(out_dir, "report.json")
    if os.path.exists(legacy):
        os.remove(legacy)                      # 防止旧版文件残留误导
    # review.json:系统诚实弃权的条目(判决通常仍为 keep,但带未决问题)→ 人工抽查队列
    review_view = {}
    for e, pe in sorted(results.items()):
        und = pe.get("undecidable") or []
        if not und:
            continue
        reasons = {}
        for name in und:
            c = pe.get("checks", {}).get(name, {})
            try:
                reasons[CHECK_CN.get(name, name)] = json.loads(
                    c.get("detail") or "{}").get("reason", "未注明")
            except Exception:  # noqa: BLE001
                reasons[CHECK_CN.get(name, name)] = "未注明"
        review_view[e] = {"当前判决": {"keep": "通过", "drop": "拒绝"}[pe["verdict"]],
                          "待裁决项": [CHECK_CN.get(n, n) for n in und],
                          "弃权原因": reasons}
    review_out = {"数据集": report.get("数据集", ""), "机器人": report.get("机器人"),
                  "待人工裁决总数": len(review_view), "episodes": review_view}
    audit = report.get("label_audit")
    if audit and audit.get("mid_for_review"):
        review_out["标注审计复核队列"] = audit["mid_for_review"]

    pj = os.path.join(out_dir, "passed.json")
    rj = os.path.join(out_dir, "reject.json")
    mp = os.path.join(out_dir, "report.md")
    with open(pj, "w") as f:
        json.dump(passed_view, f, ensure_ascii=False, indent=1, default=str)
    with open(rj, "w") as f:
        json.dump({"数据集": ds_name, "机器人": report.get("机器人"),
                   "被拒总数": len(reject_view), "episodes": reject_view},
                  f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(out_dir, "review.json"), "w") as f:
        json.dump(review_out, f, ensure_ascii=False, indent=1, default=str)
    with open(mp, "w") as f:
        f.write(to_markdown(report))
    return pj, mp
