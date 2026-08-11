"""数据包完整性(容器体检):缺什么、按什么补的,必须写进报告留档。

2026-08-10 用户定:同事转的 rrd 把源数据里本来有的 robot_type / fps / 任务文本
全丢了,而这些发现只活在启动提示行里 —— 报错管拦路,报告管留痕。这个文件钉住:
① 判定逻辑(container_findings 纯函数)每种缺失/补救组合给对状态和说明;
② 报告渲染(to_markdown)真的出现「数据包完整性」小节;
③ UI 概览(overview_markdown)带一行紧凑摘要;
④ LeRobot 全正常时整节不占位。
"""
from __future__ import annotations

from curation.export.report import container_findings, to_markdown
from curation.ui.manifest import overview_markdown

_ROBOT_HIT = {"robot_type": "so101", "embodiment_id": "so101",
              "registry_profile": "so101", "quality": "verified"}
_ROBOT_MISS = {"robot_type": "unknown", "embodiment_id": "unknown",
               "registry_profile": "(未注册)", "quality": None}


def _by_item(findings: list[dict]) -> dict:
    return {f["项"]: f for f in findings}


# ───────────────────────── RRD 输入 ─────────────────────────

def test_rrd_all_supplied_manually():
    """so101 式 rrd + --embodiment + ingest.rrd_fps:两项「缺失(已补)」都要点名补的值。"""
    info = {"fps": 30.0, "time_source": "config", "has_task_text": False,
            "recording_name": "Episode 0 · ~28.8 MiB · Grab the red cube · 593 frames"}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT, rrd_fps_arg=30.0))
    assert got["机器人型号"]["状态"] == "缺失(已补)"
    assert "--embodiment so101" in got["机器人型号"]["说明"]
    assert got["帧时间信息"]["状态"] == "缺失(已补)"
    assert "ingest.rrd_fps=30" in got["帧时间信息"]["说明"]
    assert "请核对" in got["帧时间信息"]["说明"]      # 人工补的值要提醒核对
    assert got["任务文本"]["状态"] == "缺失"
    assert "Grab the red cube" in got["任务文本"]["说明"]   # 录制名线索要露出
    assert "无标注" in got["任务文本"]["说明"]


def test_rrd_self_contained():
    """bridge 式 rrd(自带帧时间戳 + /task):时间与任务文本「正常」,型号仍缺。"""
    info = {"fps": 5.0, "time_source": "video_timestamps", "has_task_text": True,
            "recording_name": ""}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT))
    assert got["帧时间信息"]["状态"] == "正常"
    assert got["任务文本"]["状态"] == "正常"
    assert got["机器人型号"]["状态"] == "缺失(已补)"
    assert got["溯源信息"]["状态"] == "缺失"
    assert "source_dataset" in got["溯源信息"]["说明"]   # 给转换方的具体建议
    assert len(got) == 4                             # RRD 恒出四项,"正常"也留档


# ───────────────────── 内嵌 / 溯源(2026-08-10 三级降级) ─────────────────────

def test_rrd_embedded_metadata_is_normal():
    """属性内嵌 robot_type / fps / task:三样全「正常」——文件自包含,无需补救。"""
    info = {"fps": 30.0, "time_source": "properties", "has_task_text": True,
            "task_source": "embedded", "robot_type_source": "embedded",
            "robot_type_file": "so101"}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT))
    assert got["机器人型号"]["状态"] == "正常"
    assert "内嵌" in got["机器人型号"]["说明"]
    assert got["帧时间信息"]["状态"] == "正常"
    assert got["任务文本"]["状态"] == "正常"


def test_rrd_provenance_backfill_states():
    """三样都靠溯源读回:状态「缺失(已溯源补全)」,溯源信息「正常」。"""
    info = {"fps": 5.0, "time_source": "provenance", "has_task_text": True,
            "task_source": "provenance", "robot_type_source": "provenance",
            "robot_type_file": "so101",
            "provenance": {"source": "/mnt/tos/datasets/so101-pick-place",
                           "reachable": True}}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT))
    assert got["机器人型号"]["状态"] == "缺失(已溯源补全)"
    assert got["帧时间信息"]["状态"] == "缺失(已溯源补全)"
    assert got["任务文本"]["状态"] == "缺失(已溯源补全)"
    assert got["溯源信息"]["状态"] == "正常"
    assert "so101-pick-place" in got["溯源信息"]["说明"]


def test_rrd_provenance_unreachable_degrades():
    """溯源路径写了但访问不到:溯源信息「降级」,其余按实际缺失走。"""
    info = {"fps": 30.0, "time_source": "config", "has_task_text": False,
            "provenance": {"source": "tos://elsewhere/gone", "reachable": False}}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT, rrd_fps_arg=30.0))
    assert got["溯源信息"]["状态"] == "降级"
    assert "访问不到" in got["溯源信息"]["说明"]
    assert got["帧时间信息"]["状态"] == "缺失(已补)"      # 溯源没帮上,人工补的


def test_rrd_flag_vs_file_conflict_is_named():
    """用户 --embodiment 与文件派生型号不一致:照用用户的,但报告点名。"""
    info = {"fps": 5.0, "time_source": "video_timestamps", "has_task_text": True,
            "robot_type_source": "flag", "robot_type_file": "widowx"}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT))
    assert got["机器人型号"]["状态"] == "缺失(已补)"
    assert "widowx" in got["机器人型号"]["说明"]
    assert "不一致" in got["机器人型号"]["说明"]


def test_rrd_no_embodiment_says_the_way_out():
    """没给 --embodiment:状态「缺失」,说明必须给出路(--embodiment / --skip)。"""
    info = {"fps": 5.0, "time_source": "video_timestamps", "has_task_text": True}
    got = _by_item(container_findings("rrd", info, _ROBOT_MISS))
    assert got["机器人型号"]["状态"] == "缺失"
    assert "--embodiment" in got["机器人型号"]["说明"]
    assert "kinematic_limits" in got["机器人型号"]["说明"]


def test_rrd_embodiment_not_in_registry():
    """给了 --embodiment 但规格库查不到:不冒充"已补",老实说弃权。"""
    robot = {"robot_type": "myarm", "embodiment_id": "myarm",
             "registry_profile": "(未注册)", "quality": None}
    info = {"fps": 5.0, "time_source": "video_timestamps", "has_task_text": True}
    got = _by_item(container_findings("rrd", info, robot))
    assert got["机器人型号"]["状态"] == "缺失"
    assert "myarm" in got["机器人型号"]["说明"]
    assert "弃权" in got["机器人型号"]["说明"]


def test_rrd_fps_from_properties():
    """录制属性带 fps:算「正常」(文件自带,非人工补)。"""
    info = {"fps": 30.0, "time_source": "properties", "has_task_text": True}
    got = _by_item(container_findings("rrd", info, _ROBOT_HIT))
    assert got["帧时间信息"]["状态"] == "正常"
    assert "属性" in got["帧时间信息"]["说明"]


# ───────────────────────── LeRobot 输入 ─────────────────────────

def test_lerobot_all_normal_is_silent():
    """info.json 齐全 + 规格库命中:零 findings,报告整节不占位。"""
    assert container_findings("lerobot", {"fps": 30}, _ROBOT_HIT) == []


def test_lerobot_unregistered_robot_degrades():
    robot = {"robot_type": "ur5e", "embodiment_id": "ur5e",
             "registry_profile": "(未注册)", "quality": None}
    got = _by_item(container_findings("lerobot", {"fps": 30}, robot))
    assert got["机器人型号"]["状态"] == "降级"
    assert "ur5e" in got["机器人型号"]["说明"]
    assert "弃权" in got["机器人型号"]["说明"]


def test_lerobot_missing_robot_type():
    got = _by_item(container_findings("lerobot", {"fps": 30}, _ROBOT_MISS))
    assert got["机器人型号"]["状态"] == "缺失"
    assert "--embodiment" in got["机器人型号"]["说明"]


# ───────────────────────── 渲染:报告 + UI ─────────────────────────

def _report_md(container: dict | None) -> str:
    d = {"input_episodes": 1, "hard_gate_filtered": 0, "verdict_keep": 1,
         "verdict_drop": 0, "dedup_removed": 0, "delivered": 1,
         "hard_fail_breakdown": {}}
    if container:
        d["container"] = container
    return to_markdown({
        "数据集": "d", "机器人": {}, "生成时间": "t", "代码版本": "v",
        "dataset": d, "skills": {}, "config": {},
        "episodes": {"results": [], "dropped": []},
    })


def test_report_renders_container_section():
    info = {"fps": 30.0, "time_source": "config", "has_task_text": False,
            "recording_name": ""}
    md = _report_md({"format": "rrd",
                     "findings": container_findings("rrd", info, _ROBOT_HIT,
                                                    rrd_fps_arg=30.0)})
    assert "## 数据包完整性(RRD(rerun) 输入)" in md
    assert "机器人型号" in md and "缺失(已补)" in md
    assert "不是数据内容" in md                       # 前言:体检对象是容器
    # 小节必须出现在总览之前 —— 读任何数字之前先知道容器状态
    assert md.index("数据包完整性") < md.index("## 总览")


def test_report_omits_section_when_clean():
    md = _report_md(None)
    assert "数据包完整性" not in md


def test_overview_markdown_one_liner():
    info = {"fps": 30.0, "time_source": "config", "has_task_text": False}
    m = {"name": "d", "robot": "so101", "generated_at": "t", "code_version": "v",
         "dataset": {"delivered": 1, "input_episodes": 1,
                     "container": {"format": "rrd",
                                   "findings": container_findings(
                                       "rrd", info, _ROBOT_HIT, rrd_fps_arg=30.0)}},
         "episodes": {}, "audit_queue": []}
    md = overview_markdown(m)
    assert "数据包完整性" in md
    assert "机器人型号" in md and "详见质检报告" in md


def test_overview_markdown_silent_when_absent():
    m = {"name": "d", "robot": "so101", "generated_at": "t", "code_version": "v",
         "dataset": {"delivered": 1, "input_episodes": 1},
         "episodes": {}, "audit_queue": []}
    assert "数据包完整性" not in overview_markdown(m)


def test_overview_robot_dict_renders_human_readable():
    """机器人字段是 dict 时不能把原始 dict 打在页面上(2026-08-10 发现的存量毛病)。"""
    m = {"name": "d", "generated_at": "t", "code_version": "v",
         "robot": {"robot_type": "so101", "embodiment_id": "so101",
                   "registry_profile": "so101", "quality": "approximate"},
         "dataset": {"delivered": 1, "input_episodes": 1},
         "episodes": {}, "audit_queue": []}
    md = overview_markdown(m)
    assert "{" not in md
    assert "so101(规格表 so101,质量 approximate)" in md
