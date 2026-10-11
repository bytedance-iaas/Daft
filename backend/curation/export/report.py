"""The container section of a report (mcap / Lance): what the data integrity layer found about the container format
and how the episodes were cut from it (``pipeline/reporting.py``)."""
from __future__ import annotations


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
                        "说明": f"型号 {emb} 不在规格库,运动学极限整项跳过"
                                "(其余检查照常)。"})
        else:
            out.append({"项": "机器人型号", "状态": "缺失",
                        "说明": "RRD 文件不带 robot_type 字段,又无溯源信息、未指定 "
                                "--embodiment,运动学极限无规格可查,本次整项跳过。"
                                "要检查请重跑时加 --embodiment <型号>(如 so101)。"})
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
            out.append({"项": "溯源信息", "状态": "部分补全",
                        "说明": f"属性带原始数据集路径 {prov['source']},但当前环境"
                                "访问不到 —— 无法用于补全,按各项实际缺失处理。"})
        else:
            out.append({"项": "溯源信息", "状态": "缺失",
                        "说明": "属性里没有原始数据集路径。建议转换时写入 "
                                "source_dataset 与 source_episode_index(各一行),"
                                "此后缺失元数据即可自动回源补全。"})
    elif input_format == "mcap":
        # mcap(2026-09-18):留痕口径与 RRD 同族但字段更少 —— 没有溯源约定,
        # 型号/时间/标注三项该说清"文件自带还是人工补的"。
        # (lance V1 的 meta/ 就是标准 info.json,走下面 else 的 LeRobot 口径。)
        _carrier = "metadata 记录"
        rt_src = str(info.get("robot_type_source") or "")
        file_rt = str(info.get("robot_type_file") or "")
        if registry_hit and emb != "unknown":
            if rt_src == "embedded":
                out.append({"项": "机器人型号", "状态": "正常",
                            "说明": f"{_carrier}内嵌 robot_type={emb},规格类检查全自动。"})
            else:
                note = ""
                if file_rt and file_rt != emb:
                    note = (f" ⚠️ 但文件派生的型号是 {file_rt},与人工指定不一致 —— "
                            "已按人工指定为准,请核实哪个才对。")
                out.append({"项": "机器人型号", "状态": "缺失(已补)",
                            "说明": f"数据不带 robot_type;已按 --embodiment {emb} "
                                    f"人工指定,规格类检查照常执行。{note}"})
        elif emb and emb != "unknown":
            out.append({"项": "机器人型号", "状态": "已跳过",
                        "说明": f"型号 {emb} 不在规格库,运动学极限整项跳过(其余检查"
                                "照常);需要支持请提供该机器人的关节规格。"})
        else:
            out.append({"项": "机器人型号", "状态": "缺失",
                        "说明": f"数据的 {_carrier}不带 robot_type,又未指定 "
                                "--embodiment,运动学极限无规格可查,本次整项跳过。"
                                "要检查请重跑时加 --embodiment <型号>。"})
        if str(info.get("time_source") or "") == "log_time":
            out.append({"项": "帧时间信息", "状态": "正常",
                        "说明": "消息自带 log_time,时间轴取自 action topic,全自动。"})
        if info.get("has_task_text"):
            out.append({"项": "任务文本", "状态": "正常",
                        "说明": "数据自带任务描述,按有标注数据处理。"})
        else:
            out.append({"项": "任务文本", "状态": "缺失",
                        "说明": "数据里没有任务文本,按无标注数据处理"
                                "(任务意图走自产描述补标线)。"})
    else:
        rt = str(robot.get("robot_type") or "unknown")
        # 说法与管道实际行为对齐(2026-09-16):查不到规格表 = 运动学极限整项跳过
        # (run.py),不是逐条弃权;人工指定了型号的按指定的说
        if registry_hit:
            if rt == "unknown":
                out.append({"项": "机器人型号", "状态": "缺失(已补)",
                            "说明": f"info.json 未声明 robot_type;已按指定型号 {emb} "
                                    "查规格,运动学极限照常执行。"})
        elif emb and emb != "unknown":
            out.append({"项": "机器人型号", "状态": "已跳过",
                        "说明": f"型号 {emb} 不在规格库,运动学极限整项跳过(其余检查"
                                "照常);需要支持请提供该机器人的关节规格。"})
        else:
            out.append({"项": "机器人型号", "状态": "缺失",
                        "说明": "info.json 未声明 robot_type,运动学极限无规格可查,"
                                "本次整项跳过;要检查请用 --embodiment <型号> 指定。"})
    return out
