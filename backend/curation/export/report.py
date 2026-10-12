"""The container section of a report (mcap / Lance): what the data integrity layer found about the container format
and how the episodes were cut from it (``pipeline/reporting.py``)."""
from __future__ import annotations


def container_findings(input_format: str, info: dict, robot: dict) -> list[dict]:
    """数据包(容器)完整性体检:文件里带没带管线要用的元信息。

    检的不是数据内容,是容器本身 —— 源数据里本来有、转换/打包时丢了的字段:缺了什么、
    我们按什么补救的、哪些检查因此受影响,客户和转换方都要能从报告里看到。

    返回 [{"项", "状态", "说明"}];状态 ∈ 正常 / 缺失(已补) / 降级 / 缺失。
    LeRobot 输入一切正常时返回 [](info.json 本来就是它的标配,不占版面)。
    """
    out: list[dict] = []
    registry_hit = robot.get("registry_profile") not in (None, "", "(未注册)")
    emb = str(robot.get("embodiment_id") or "")
    if input_format == "mcap":
        # mcap(2026-09-18):没有溯源约定,型号/时间/标注三项该说清"文件自带还是人工补的"。
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
                                "(不做任务成败判定,其余检查照常)。"})
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
