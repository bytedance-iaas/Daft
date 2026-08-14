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


def collect_camera_audit(per_episode: dict) -> tuple[dict, int]:
    """相机体检(占位/黑帧通道)→ (逐条结论, 有结论的条目数)。**纯装配**。

    数据来自视觉质量那一遍顺手产出的 `detail.camera_liveness`(2026-08-14 合并):
    本函数一帧都不解、一个数不重算。没跑视觉质量的条目自然没有结论,如实不计入
    分母 —— 编一个"全部相机正常"比留空更误导。
    只回报**有占位/黑帧路**的条目;全活的不占版面(与老口径一致)。
    """
    audit: dict = {}
    n_audited = 0
    for eid, pe in sorted(per_episode.items()):
        vq = (pe.get("checks") or {}).get("visual_quality")
        if not vq:
            continue
        try:
            liveness = json.loads(vq.get("detail") or "{}").get("camera_liveness")
        except Exception:  # noqa: BLE001
            liveness = None
        if not liveness:
            continue
        n_audited += 1
        if liveness.get("dead_or_padded"):
            audit[eid] = liveness
    return audit, n_audited


def container_findings(input_format: str, info: dict, robot: dict,
                       rrd_fps_arg: float | None = None) -> list[dict]:
    """数据包(容器)完整性体检:文件里带没带管线要用的元信息。

    检的不是数据内容,是容器本身 —— 源数据里本来有、转换/打包时丢了的字段
    (2026-08-10 同事转的 rrd 三样全丢:robot_type / fps / 任务文本),现在的
    存在形式是启动时刷一行提示,跑完就没了。报错管拦路,报告管留痕:缺了什么、
    我们按什么补救的、哪些检查因此受影响,客户和转换方都要能从报告里看到。

    返回 [{"项", "状态", "说明"}];状态 ∈ 正常 / 缺失(已补) / 缺失(已溯源补全)
    / 降级 / 缺失。「已溯源补全」= 属性里带了原始数据集路径,缺的元数据从源头
    meta 自动读回(2026-08-10 用户定的三级降级:内嵌/自带 → 溯源 → 人工 → 缺失,
    只补空缺不覆盖)。LeRobot 输入一切正常时返回 [](info.json 本来就是它的标配,
    不占版面);RRD 输入永远返回四项 —— "正常"也值得写,因为它是逐数据集变化的。
    """
    out: list[dict] = []
    registry_hit = robot.get("registry_profile") not in (None, "", "(未注册)")
    emb = str(robot.get("embodiment_id") or "")
    if input_format == "rrd":
        rt_src = str(info.get("robot_type_source") or "")
        file_rt = str(info.get("robot_type_file") or "")
        if registry_hit and emb != "unknown":
            if rt_src == "embedded":
                out.append({"项": "机器人型号", "状态": "正常",
                            "说明": f"文件属性内嵌 robot_type={emb},规格类检查全自动。"})
            elif rt_src == "provenance":
                out.append({"项": "机器人型号", "状态": "缺失(已溯源补全)",
                            "说明": f"RRD 文件不带 robot_type;已从溯源到的原始数据集"
                                    f"读回 robot_type={emb},规格类检查照常执行。"})
            else:                      # flag(或来源信号缺失的老数据):人工指定
                note = ""
                if file_rt and file_rt != emb:
                    note = (f" ⚠️ 但文件派生的型号是 {file_rt},与人工指定不一致 —— "
                            "已按人工指定为准,请核实哪个才对。")
                out.append({"项": "机器人型号", "状态": "缺失(已补)",
                            "说明": f"RRD 文件不带 robot_type 字段;已按 --embodiment "
                                    f"{emb} 人工指定,规格类检查(关节/速度极限)"
                                    f"照常执行。{note}"})
        elif emb and emb != "unknown":
            out.append({"项": "机器人型号", "状态": "缺失",
                        "说明": f"型号 {emb} 不在规格库,运动学极限对照按弃权处理。"})
        else:
            out.append({"项": "机器人型号", "状态": "缺失",
                        "说明": "RRD 文件不带 robot_type 字段,又无溯源信息、未指定 "
                                "--embodiment,运动学极限检查无规格可查。重跑时加 "
                                "--embodiment <型号>(如 so101),或 "
                                "--skip kinematic_limits 明确跳过。"})
        ts = str(info.get("time_source") or "")
        if ts == "video_timestamps":
            out.append({"项": "帧时间信息", "状态": "正常",
                        "说明": "视频帧自带时间戳(VideoFrameReference),时间轴全自动。"})
        elif ts == "properties":
            out.append({"项": "帧时间信息", "状态": "正常",
                        "说明": f"录制属性带 fps={info.get('fps')},按恒定帧率换算时间轴。"})
        elif ts == "provenance":
            out.append({"项": "帧时间信息", "状态": "缺失(已溯源补全)",
                        "说明": f"文件内没有时间信息;已从溯源到的原始数据集读回 "
                                f"fps={info.get('fps')},按恒定帧率换算时间轴。"})
        else:
            _fps = rrd_fps_arg if rrd_fps_arg else info.get("fps")
            out.append({"项": "帧时间信息", "状态": "缺失(已补)",
                        "说明": f"文件内没有任何时间信息,已按人工指定的 "
                                f"ingest.rrd_fps={_fps} 换算时间轴。该值若与真实采集"
                                "帧率不符,时序/同步类结论会整体失真 —— 请核对。"})
        task_src = str(info.get("task_source") or "")
        if not task_src and info.get("has_task_text"):
            task_src = "task_channel"        # 老信号兼容:只知道"有",按通道自带算
        if task_src == "task_channel":
            out.append({"项": "任务文本", "状态": "正常",
                        "说明": "/task 通道带任务描述,按有标注数据处理。"})
        elif task_src == "embedded":
            out.append({"项": "任务文本", "状态": "正常",
                        "说明": "任务描述内嵌在文件属性里,按有标注数据处理。"})
        elif task_src == "provenance":
            out.append({"项": "任务文本", "状态": "缺失(已溯源补全)",
                        "说明": "RRD 里没有任务文本;已按属性里的源 episode 序号从"
                                "原始数据集逐条读回,按有标注数据处理。"})
        else:
            name = str(info.get("recording_name") or "")
            hint = (f"录制名「{name}」里疑似混有任务描述,但这不是稳定约定,未采用;"
                    if name else "")
            out.append({"项": "任务文本", "状态": "缺失",
                        "说明": f"RRD 里没有 /task 通道;{hint}"
                                "按无标注数据处理(任务意图走自产描述补标线)。"})
        prov = info.get("provenance") or {}
        if prov.get("source") and prov.get("reachable"):
            out.append({"项": "溯源信息", "状态": "正常",
                        "说明": f"属性带原始数据集路径 {prov['source']},可访问 —— "
                                "缺失元数据可自动回源补全。"})
        elif prov.get("source"):
            out.append({"项": "溯源信息", "状态": "降级",
                        "说明": f"属性带原始数据集路径 {prov['source']},但当前环境"
                                "访问不到 —— 无法用于补全,按各项实际缺失处理。"})
        else:
            out.append({"项": "溯源信息", "状态": "缺失",
                        "说明": "属性里没有原始数据集路径。建议转换时写入 "
                                "source_dataset 与 source_episode_index(各一行),"
                                "此后缺失元数据即可自动回源补全。"})
    else:
        rt = str(robot.get("robot_type") or "unknown")
        if rt == "unknown":
            out.append({"项": "机器人型号", "状态": "缺失",
                        "说明": "info.json 未声明 robot_type,运动学极限检查无规格可查;"
                                "可用 --embodiment <型号> 指定。"})
        elif not registry_hit:
            out.append({"项": "机器人型号", "状态": "降级",
                        "说明": f"型号 {rt} 不在规格库,运动学极限对照按弃权处理"
                                "(不误杀也不放行);需要支持请提供该机器人的关节规格。"})
    return out


#: 报告里给客户看的说法。⚠️ 只换**人看的字**,JSON 的键名与数值口径一个字不动
#: (passed/reject/review.json 的键是数据契约,`综合软分` 那种键名尤其不许碰)。
#: 「软分」是内部机制名(可补偿的打分维度),界面 2026-08-11 就统一叫「质量分」了;
#: 报告是同一批客户看的同一件事,两处说法不该分叉。
_PLAIN_WORDS = (("软分", "质量分"),)

#: 判废里"没有检查名"的那一类叫什么(判决层的第二种判废理由:综合加权分低于阈值,
#: 见 pipeline/verdict.py)。与 UI 总览表用同一个说法。
DROP_SOFT_LABEL = "综合质量分不达标"

#: 判决取值 → 报告里的说法。keep/drop 是实现记法,客户看的是「交付 / 判废」。
VERDICT_CN = {"keep": "交付", "drop": "判废"}


def plain(text) -> str:
    """把一句人看的话里的内部黑话换成界面同款说法。**只用在 markdown 渲染上。**"""
    s = str(text if text is not None else "")
    for bad, good in _PLAIN_WORDS:
        s = s.replace(bad, good)
    return s


def drop_breakdown(d: dict) -> tuple[list, bool]:
    """判废子项 → ([(检查中文名, 条数), …], 相加是否大于总数)。

    判决层有**两种**判废理由:踩中硬门(有检查名),或综合加权分 < 阈值(没有检查
    名)。只列前者的话,子项加不出判废总数 —— 报表上"判废 16、子项 14+1"是自己打
    自己脸。差额补一行 DROP_SOFT_LABEL 就闭合了。
    反过来一条 episode 可能同时踩中两个硬门,那时子项相加**大于**总数,只能改口说
    「其中」并在旁边注明,不能摆成分解式请读者去加。
    UI 侧 manifest.drop_breakdown 是同一套判据的读端副本(UI 不 import 管道代码)。
    """
    hb = d.get("hard_fail_breakdown")
    drop = d.get("verdict_drop")
    if not isinstance(hb, dict) or not isinstance(drop, int):
        return [], False
    items = [(CHECK_CN.get(k, k), n) for k, n in
             sorted(hb.items(), key=lambda x: -x[1])]
    judged = sum(n for _, n in items if isinstance(n, int))
    if judged > drop:
        return items, True
    if judged < drop:
        items = items + [(DROP_SOFT_LABEL, drop - judged)]
    return items, False


def _container_lines(d: dict) -> list:
    """「数据包完整性」小节。有 findings 才出,LeRobot 全正常时整节不占位。"""
    cont = d.get("container")
    if not isinstance(cont, dict) or not cont.get("findings"):
        return []
    _fmt = {"rrd": "RRD(rerun)", "lerobot": "LeRobot"}.get(
        str(cont.get("format")), str(cont.get("format")))
    icon = {"正常": "✅ ", "缺失(已补)": "⚠️ ", "缺失(已溯源补全)": "✅ ",
            "降级": "⚠️ ", "缺失": "❌ "}
    out = [f"## 数据包完整性({_fmt} 输入)",
           "> 体检的是数据包本身带没带管线需要的元信息,不是数据内容。"
           "缺失但已人工补上的项,请核对补的值是否与实际相符。"]
    for f in cont["findings"]:
        out.append(f"- **{f.get('项')}** · {icon.get(str(f.get('状态')), '')}"
                   f"{f.get('状态')} —— {f.get('说明')}")
    out.append("")
    return out


def _sync_plot_coverage(d: dict, report: dict) -> list:
    """曲线画了哪些 —— 覆盖范围必须写明。

    默认配置只给"需要留意"的条目画图,客户翻 details/plots/ 只看到几张,
    很容易误以为只检查了那几条(2026-08-07 用户在 UI 上问的同一个问题,
    报告侧同样要答)。
    """
    sp = d.get("sync_plots")
    if not isinstance(sp, dict):
        return []
    n_plot = sp.get("生成")
    if n_plot is None:
        return []
    mode = str((((report.get("config_effective") or {}).get("pipeline")) or {})
               .get("sync_plots") or "")
    where = sp.get("目录", "details/plots/")
    if mode == "all":
        return [f"- **曲线**: 每条被检查的 episode 都出了图,共 {n_plot} 张,见 `{where}`。"]
    return [f"- **曲线**: 共 {n_plot} 张,见 `{where}`。"
            f"⚠️ 当前配置 `pipeline.sync_plots = {mode or 'flagged'}` "
            f"**只为需要留意的条目画图** —— 没有图不等于没检查,"
            f"逐条结论见上表与下方分节;要逐条都出图请加 "
            f"`--set pipeline.sync_plots=all` 重跑。"]


def _sync_camera_lines(entries: list, cap: int = 30) -> list:
    """逐条打印"哪条 episode 的哪一路怎么了"。

    **带病因不带黑话**:原来只印 lag/corr/峰宽/主次峰比这堆数字,客户读不出
    "所以到底是什么毛病、我该怎么办"(2026-08-07 用户:"你得给出正确的诊断啊")。
    诊断文本与 UI 同源(camera_diagnosis 的产物),两处口径永不分叉。
    """
    out: list = []
    for x in entries[:cap]:
        for cam, r in (x.get("cameras") or {}).items():
            dg = r.get("diagnosis") if isinstance(r.get("diagnosis"), dict) else {}
            lag = "-" if r.get("lag_s") is None else f"{float(r['lag_s']):+.2f}s"
            corr = ("-" if r.get("corr_peak") is None
                    else f"{float(r['corr_peak']):.2f}")
            head = (f"- **{x['episode_id']}** · {cam} · 滞后 {lag} · 相关 {corr}"
                    + (f" — **{dg['label']}**" if dg.get("label") else ""))
            out.append(head)
            if dg.get("text"):
                out.append(f"  - {dg['text']}")
            if dg.get("advice"):
                out.append(f"  - → {dg['advice']}")
            if not dg and r.get("note"):
                out.append(f"  - {r['note']}")
    if len(entries) > cap:
        out.append(f"- …共 {len(entries)} 条(全量见 json)")
    return out


def _sync_soft_sections(lines: list, sh: dict) -> None:
    """「疑似错位」与「假峰 / 测不准」两节。

    这两类都**不判废、不进人工裁决队列、不参与综合质量分**,但必须在报告里出现:
    2026-08-07 ep4 就是因为两个都不沾(既非 flagged 也非负滞后),在报告里
    查无此人 —— 而它恰恰是客户翻曲线时第一个会问的那条。
    """
    sus = sh.get("suspect_episodes") or []
    if sus:
        lines.append("")
        lines.append(f"### 疑似错位,证据不足({len(sus)} 条;**不判废、不进人工队列**)")
        lines.append("> 这一路测出的偏移明显不在零点,而且「完全不错开」已经解释"
                     "不了数据;但证据还不够硬,不足以定论。"
                     "同一路反复出现时多半是真延时,建议抽查。")
        lines.extend(_sync_camera_lines(sus, cap=30))
    noisy = sh.get("noisy_episodes") or []
    if noisy:
        lines.append("")
        lines.append(f"### 假峰 / 测不准({len(noisy)} 条;**不判废、证据偏向对齐**)")
        lines.append("> 这一路虽然测出了偏移,但把两条曲线错开和**完全不错开**,"
                     "相似程度几乎一样 —— 数据说明不了画面和动作没对上,不是错位。"
                     "成因逐条不同(运动正对镜头、离镜头太近、背景有人走动、相机晃动"
                     "等),**每条只写本条实测到的那一种**,见下方逐相机诊断。"
                     "数据照常交付;想让这些路也可判,按诊断给出的方向改善机位或环境。")
        lines.extend(_sync_camera_lines(noisy, cap=30))


def to_markdown(report: dict) -> str:
    d = report["dataset"]
    rb = report.get("机器人") or {}
    _rb_line = ""
    if rb:
        _rb_line = (f"- **机器人型号**: {rb.get('robot_type')}"
                    f"(规格表: {rb.get('registry_profile')}"
                    f"{',质量 ' + str(rb.get('quality')) if rb.get('quality') else ''})")
    _drop_items, _drop_overlap = drop_breakdown(d)
    _identity = ("输入 = 判废 + 精确去重删除 + 交付" if d.get("dedup_removed")
                 else "输入 = 判废 + 交付")
    lines = [
        "# 数据集质检报告",
        f"- **数据集**: {report.get('数据集', '(未知)')}",
        *([_rb_line] if _rb_line else []),
        f"- 生成时间: {report.get('生成时间', '?')} | 代码版本: {report.get('代码版本', '?')}",
        # 数据集注记(2026-07-29):数据集 profile 的 extras.note,读数字前必须知道的
        # 前提(如 bridge 的 state 由 action 累加合成 → stuck 只能弃权)。有才出这行
        *([f"- **数据集注记**: {d['dataset_note']}"] if d.get("dataset_note") else []),
        "",
        # 数据包完整性(2026-08-10):容器缺了什么、按什么补的,放在读任何数字之前
        *_container_lines(d),
        # 总览的口径与 UI 那张总览表**逐行对齐**(2026-08-14 用户定):一份数据只有
        # 一个口径,客户不该在界面和报告里读到两套。此前这里还有一行"硬门拦截(漏斗
        # 中途淘汰)",它与下面的「判废」同名不同义(一个是中途被刷掉的,一个是最终
        # 判废的全部),正是 UI 那次合表要消灭的那种自相矛盾 —— 数字仍在 json 的
        # hard_gate_filtered 里,只是不再摆在客户眼前当第二个口径。
        "## 总览",
        f"- 输入 episode:{d['input_episodes']}",
        f"- 判废:{d['verdict_drop']}"
        + ("(子项按检查逐项计数,**同一条可能同时踩中多项**,相加会大于总数)"
           if _drop_overlap else ""),
        *[f"  - {'其中 ' if _drop_overlap else ''}{_n}:{_c}"
          for _n, _c in _drop_items],
        f"- 精确去重删除:{d['dedup_removed']}"
        + (f"({d['dedup_note']})" if d.get("dedup_note") else ""),
        f"- **交付:{d['delivered']} 条**",
        f"> 口径:{_identity}(三者不重不漏)。",
        # 交付数据集目录(2026-08-10 RRD 出口):同一份报告要能答"数据在哪个目录、
        # 什么格式"。有才出这行(--report-only 与老交付都没有,不占位)。
        *([f"- 交付数据集:{d['交付数据集']}"] if d.get("交付数据集") else []),
        "",
    ]
    ss = d.get("summary_stats")
    if ss:
        lines.append("## 汇总统计")
        lines.append(f"- 通过率: {ss['pass_rate_pct']}%  |  平均质量分: {ss['avg_soft_score']}")
        for n, s in ss["per_check"].items():
            # 检查名也说人话:summary_stats 的键是实现名(json 里照旧),但客户报告里
            # 印 `timestamp_check` 等于没写 —— 界面从来都是「时间戳检查」
            cn = CHECK_CN.get(n, n)
            if s["avg_score"] is not None:            # 可补偿的打分维度:只报均分
                lines.append(f"- {cn}(质量分): 均分 {s['avg_score']}")
            else:                                     # 能直接判废的检查:报三态
                # 「杀」这种词不进客户报告(与「软分/硬门/漏斗」同一批黑话)
                lines.append(f"- {cn}(判废项): 通过 {s['pass']} · 判废 {s['fail']}"
                             f" · 弃权 {s['abstain']}")
                for rsn, cnt in (s.get("abstain_reasons") or {}).items():
                    lines.append(f"  - 弃权原因: {rsn}({cnt} 条)")
        lines.append("")
    lat = report.get("dataset", {}).get("vlm_latency")
    if lat:
        # 延时档案(2026-07-28):客户端视角 = 网络+服务端排队+推理。四类调用体质
        # 不同分开报;逐请求明细在 details/vlm_latency.csv。
        _cn = {"probe": "渐变问询(VOC)", "endstate": "二值复核",
               "caption": "画像 caption", "llm": "归纳/审计 LLM",
               "arbitration": "取证仲裁"}
        lines.append("## 模型调用延时(客户端视角,秒)")
        _tw = (report.get("runtime") or {}).get("total_wall_s")
        if _tw:
            _m, _s = divmod(int(_tw), 60)
            _h, _m = divmod(_m, 60)
            _txt = f"{_h} 小时 {_m} 分 {_s} 秒" if _h else f"{_m} 分 {_s} 秒"
            lines.append(f"\n**整次运行总墙钟:{_txt}**(启动 → 交付落盘可用,含全部阶段)\n")
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
        lines.append("| episode | 判决 | 质量分 | "
                     + " | ".join(CHECK_CN.get(n, n) for n in names) + " |")
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
                    cells.append({True: "通过", False: "判废",
                                  None: "弃权"}[c["passed"]])
            soft = f"{pe['soft_score']:.2f}" if pe.get("soft_score") is not None else "-"
            # keep/drop 是实现记法,客户报告里说「交付 / 判废」(与界面同一套词);
            # ⚠️ 只换这张表里印出来的字,json 里的 verdict 取值一个字不动(数据契约)
            verdict = VERDICT_CN.get(str(pe["verdict"]), str(pe["verdict"]))
            lines.append(f"| {e} | {verdict} | {soft} | " + " | ".join(cells) + " |")
        lines.append("")
    vp = d.get("visual_qc_params")
    if vp:
        lines.append("## 视觉质检参数(可在配置覆盖)")
        lines.append(f"- 清晰度参考线 blur_ref_var: {vp['blur_ref_var']}(绑定分辨率标尺 "
                     f"frame_max_side={vp['frame_max_side']})")
        lines.append(f"- 相机权重: {vp['camera_weights']};总分聚合: {vp['aggregation']}")
        lines.append("")
    sh = d.get("sync_health")
    if sh:
        # 相机流健康度(2026-08-07 逐相机改造的主要产品):同步检查改为逐相机独立
        # 测量后,数据集级的问题形状才看得出来——全库同号同量 = 一次标定就能救回
        # 整批数据;逐条乱跳 = 录制不稳定,只能逐条看。判废仍只发生在"所有可信
        # 相机一致指向同一个 Δ"这一种情形,本节里的标注**都不影响判决**。
        lines.append("## 相机流健康度(视频-动作同步,逐相机)")
        lines.append(f"- **结论倾向**: {sh.get('advice', '')}")
        # 判定口径写死在报告里:客户最容易误读的就是"标注了是不是就等于坏了"
        lines.append("- **判废口径**:只有「**所有可信相机一致指向同一个偏移**」"
                     "才判废整条 episode(那是录制管线的时间轴问题)。"
                     "本节其余所有标注 —— 单路错位、疑似错位、假峰、测不准 —— "
                     "**一律不判废、不进人工裁决队列、不参与综合质量分**,"
                     "视频指针一路不删,数据照常交付。")
        lines.extend(_sync_plot_coverage(d, report))
        _pc = sh.get("per_camera") or {}
        if _pc:
            lines.append("")
            # 表头一律说人话:客户拿到手的是**报告**不是 UI,"IQR" 这种统计黑话
            # 留在这里等于没改(2026-08-07 用户点名"四分位距是个啥")。
            lines.append("| 相机 | 有效读数 | 典型滞后 | 逐条波动 | 疑似错位 "
                         "| 假峰 | 测不准 | 已标注 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for cam, s in sorted(_pc.items()):
                med = ("-" if s.get("median_lag_s") is None
                       else f"{s['median_lag_s']:+.3f}s")
                iqr = "-" if s.get("iqr_s") is None else f"{s['iqr_s']:.3f}s"
                lines.append(f"| {cam} | {s.get('n', 0)} | {med} | {iqr} "
                             f"| {s.get('n_suspect', 0)} | {s.get('n_noisy', 0)} "
                             f"| {s.get('n_abstained', 0)} | {s.get('n_flagged', 0)} |")
            lines.append("")
            lines.append("> **典型滞后**=这一路画面比动作晚多少(>0 画面晚,"
                         "是相机链路延迟,常见且可标定;<0 画面早于动作,"
                         "**无良性解释**,只可能是数据装配错误)。"
                         "**逐条波动**=各条 episode 之间这个数跳得厉不厉害"
                         "(小=录制稳定,大=时快时慢)。"
                         "**疑似错位**=测出的偏移不在零点,且「完全不错开」已解释"
                         "不了数据;**假峰**=测出了偏移,但错开与完全不错开几乎同样像"
                         "(画面干扰所致,证据仍偏向对齐);"
                         "**测不准**=这一路读数不可信的总条数;"
                         "**已标注**=可信且确实错位、或完全无信号。"
                         "典型滞后/逐条波动只统计可信读数(测不准的不掺进分布)。")
        neg = sh.get("negative_lag_episodes") or []
        if neg:
            lines.append("")
            lines.append(f"- ⚠️ **负滞后 {len(neg)} 条**(建议核对转换脚本):")
            for x in neg[:20]:
                cams = ", ".join(f"{c} {v:+.2f}s" for c, v in (x.get("cameras") or {}).items())
                lines.append(f"  - {x['episode_id']}: {cams}")
            if len(neg) > 20:
                lines.append(f"  - …共 {len(neg)} 条")
        fe = sh.get("flagged_episodes") or []
        if fe:
            lines.append("")
            lines.append(f"### 被标注的相机({len(fe)} 条 episode;**只标注不废弃**,"
                         "视频指针一路不删)")
            lines.extend(_sync_camera_lines(fe, cap=30))
        _sync_soft_sections(lines, sh)
        lines.append("")
    # 判废的逐项分布已经并进「总览」那几行(用检查中文名、并补上没有检查名的那一类)。
    # 此前这里还有一节「硬门违规分布」印英文检查名 —— 同一批数字说两遍,而且那遍
    # 用的是客户看不懂的键名。合并成一处之后口径只剩一个(2026-08-14 用户定)。
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
    _unst = (la or {}).get("low_caption_unstable") or []
    if la and (la["high"] or la["mid_for_review"] or _unst):
        lines.append(f"## 标注-画面分歧(高置信 {len(la['high'])} / 人工复核 "
                     f"{len(la['mid_for_review'])} / 我方描述不稳 {len(_unst)})")
        lines.append("> 原始标注与自产描述(VLM 生成)归进了不同技能族。**双方都可能错**:"
                     "自产描述既可能看错也不一定可复现,这里只给复核队列,不判谁对。")
        _n_focus = sum(1 for t in ("high", "mid_for_review") for x in la.get(t) or []
                       if x.get("priority") == "重点")
        if _n_focus:
            lines.append(f"> **重点档 {_n_focus} 条**(任务成败线同时不利——两条独立证据线"
                         "同报警,标注错嫌疑最高,建议优先人工);其余为参考档"
                         "(成败线已放行,分歧多为自产描述噪声)。")
        for f in la["high"][:20]:
            _st = "(重打标 N 次同族,我方描述稳定)" if f.get("caption_stable") else ""
            _pr = f"[{f.get('priority', '参考')}]"
            _tv = f"(成败线:{f.get('task_verdict')})" if f.get("task_verdict") else ""
            lines.append(f"- [高]{_pr} {f['id']} {f['reason']}: 标注「{f['label'][:40]}」"
                         f" vs 自产描述「{f['caption'][:40]}」{_st}{_tv}")
        for f in _unst[:20]:
            lines.append(f"- [降级·我方描述不稳] {f['id']}: 标注「{f['label'][:40]}」"
                         f" vs 重打标「{'」「'.join(c[:40] for c in f.get('recaptions', []))}」")
        lines.append("")
    dropped = report["episodes"]["dropped"]
    if dropped:
        lines.append("## 判废明细(逐条)")
        results = report["episodes"].get("results") or {}
        for e, v in list(dropped.items())[:50]:
            # 理由本身是判决层拼的那一句(单一事实源,见 hard_fail_reason);这里
            # 只把里面的内部黑话换成界面同款说法,不重新拼一句
            lines.append(f"- {e}: {plain(v['reason'])}")
            # 证据直出(2026-07-14 用户定):被拒必须当场看到哪里错,不必翻 reject.json
            for cname, c in (results.get(e, {}).get("checks") or {}).items():
                if c.get("passed") is not False or not c.get("detail"):
                    continue
                try:
                    det = (json.loads(c["detail"]) if isinstance(c["detail"], str)
                           else c["detail"]) or {}
                except Exception:  # noqa: BLE001
                    det = {}
                cname = CHECK_CN.get(cname, cname)
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


def check_detail_reason(check: dict) -> str:
    """检查条目 → 它自己写的那句人话(detail.reason)。拿不到给空串,绝不编。

    detail 在管线里是 JSON 字符串(daft 列),在别处可能已是 dict —— 两种都吃。
    """
    d = (check or {}).get("detail")
    if isinstance(d, str) and d.strip():
        try:
            d = json.loads(d)
        except Exception:  # noqa: BLE001
            return ""
    return str((d or {}).get("reason") or "").strip() if isinstance(d, dict) else ""


def hard_fail_reason(hard_fails: list, checks: dict) -> str:
    """被拒理由的**唯一事实源**:未通过「检查名」:<该检查写的那句人话>。

    2026-08-11 用户定:光报检查名会把人带偏 —— ep000018 显示"未通过「时间戳检查」",
    第一反应是同步出了问题,实际是 0.47 秒的采集残段。所以拼装时必须把检查自己
    写的 detail.reason 带上;report.md 的判废明细、reject.json 的原因、UI 的判决
    横幅与左清单**全部引用这一个串**,不许各拼各的。
    """
    parts = []
    for name in hard_fails:
        cn = CHECK_CN.get(name, name)
        why = check_detail_reason(checks.get(name) or {})
        parts.append(f"未通过「{cn}」:{why}" if why else f"未通过「{cn}」")
    return ";".join(parts)


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


def save_report(report: dict, out_dir: str, verify: bool = False) -> tuple[str, str]:
    """交付三文件:passed.json(通过条目) / reject.json(被拒条目+中文原因+证据) / report.md。

    ⚠️ 一律走 safe_write(本地临时文件 + copyfile):2026-08-14 e2e 抓到 passed.json
    落成 10853 字节全 `\\0` —— 这里原本是 `open(...,"w") + json.dump` **直写 FSX
    挂载**,而一次跑批要对同一路径写三遍(装饰块后、总墙钟回填后、落盘回验后),
    最后那遍恰好把已经回验通过的文件写坏了,且全程零报错。

    verify=True 给**最后那几遍**用:回读一次,只在读到的内容确实是坏的(零填充/
    解析不了)时重写并留一行日志。"暂时读不到"不算故障 —— 这张挂载上新文件普遍
    要 30 秒才可见(pod 实测),那由轮询式落盘回验去确认,不在这里喊狼来了。
    """
    import os

    from .safe_write import write_json, write_text

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
    if audit and (audit.get("high") or audit.get("mid_for_review")
                  or audit.get("low_caption_unstable")):
        # 键名中性化(2026-07-31):不是"审计标注",是标注与自产描述的分歧,双方都可能错。
        # 降级档(我方描述重打标不稳)也进队列——它仍是待人工判定的分歧,只是嫌疑更低;
        # 不进来的话 UI 里就看不见了(UI 只渲染这条队列)。
        # 三层全进队列(2026-08-05 修:high 档此前漏掉,高嫌疑的反而在 UI 看不见);
        # 重点档排最前(attach_task_context 已在层内排序,层间按嫌疑度排)
        _q = ((audit.get("high") or []) + (audit.get("mid_for_review") or [])
              + (audit.get("low_caption_unstable") or []))
        review_out["标注-画面分歧复核队列"] = sorted(
            _q, key=lambda x: x.get("priority") != "重点")

    pj = os.path.join(out_dir, "passed.json")
    rj = os.path.join(out_dir, "reject.json")
    mp = os.path.join(out_dir, "report.md")
    _v = "json" if verify else None
    write_json(pj, passed_view, verify=_v, default=str)
    write_json(rj, {"数据集": ds_name, "机器人": report.get("机器人"),
                    "被拒总数": len(reject_view), "episodes": reject_view},
               verify=_v, default=str)
    write_json(os.path.join(out_dir, "review.json"), review_out,
               verify=_v, default=str)
    write_text(mp, to_markdown(report), verify="text" if verify else None)
    return pj, mp
