"""明细 CSV 的中文列名、枚举值与"不适用"折叠(2026-09-02 用户定)。

写盘(pipeline/run.py)、报告(export/report.py)、界面(ui/manifest.py)三方共用这一份对照:
- CSV 直接写中文列名——对 CLI 用户来说 CSV 就是报告;全库无代码按英文列名回读这些表。
- 整列全空的子项(对本数据集不适用,如 droid 的执行器饱和:速度型指令与位置读数不同
  语义,无法直比)**不出这一列**,改成一句话说明;部分为空的子项列保留,空格界面显示「—」。
- 数值不动;只翻列名与枚举值。
"""
from __future__ import annotations

#: 运动质量明细:英文键 → 中文列名(顺序即 CSV 列序)。
MOTION_COLS: list[tuple[str, str]] = [
    ("episode", "条目"), ("score", "运动总分"),
    ("smoothness", "平滑度"), ("spike", "尖刺"), ("stuck", "卡顿"),
    ("gripper_jitter", "夹爪抖动"), ("actuator_saturation", "执行器饱和"),
    ("joint_stability", "末态稳定"), ("path_efficiency", "路径效率"),
    ("fluency", "流畅度"), ("active_ratio", "运动占比"),
    ("idle_head_s", "开头空闲(秒)"), ("idle_tail_s", "结尾空闲(秒)"),
    ("idle_mid_count", "中途停顿次数"), ("idle_mid_total_s", "中途停顿总时长(秒)"),
    ("spike_isolation", "尖刺孤立度"), ("saturation_gap_ratio", "饱和偏差比"),
    ("tail_std", "末段抖动"), ("gripper_flips", "夹爪开合次数"),
]
#: 六维打分子项(可能整列不适用的那些);其余是辅助读数,跟随主项一起折叠即可。
MOTION_SUBDIMS = ("smoothness", "spike", "stuck", "gripper_jitter",
                  "actuator_saturation", "joint_stability", "path_efficiency")
#: 子项不适用时 detail 里留痕的原因键;没有专门原因键的用通用说法。
MOTION_NA_REASON_KEY = {"actuator_saturation": "saturation_reason",
                        "spike": "spike_reason", "stuck": "stuck_reason",
                        "gripper_jitter": "gripper_reason"}
#: 主项不适用时一并折叠的辅助读数列(它们只有主项算了才有值)。
MOTION_COMPANION = {"actuator_saturation": ("saturation_gap_ratio",),
                    "spike": ("spike_isolation",),
                    "gripper_jitter": ("gripper_flips",)}
GENERIC_NA_REASON = "本数据集缺少计算它所需的读数"

#: 视觉质量明细(逐相机)。
VISUAL_COLS: list[tuple[str, str]] = [
    ("episode", "条目"), ("camera", "相机"), ("status", "状态"), ("score", "视觉总分"),
    ("sharpness", "清晰度"), ("exposure", "曝光"), ("integrity", "完整性"),
    ("blur_var_median", "清晰度原值(拉普拉斯方差中位数)"),
    ("clip_frac_median", "过曝/欠曝像素占比"), ("gray_std_median", "灰度标准差"),
    ("frozen_ratio", "冻结帧占比"),
]
VISUAL_STATUS = {"OK": "正常", "PAD": "占位(无画面)"}

#: 运动学违规明细。
KINEMATIC_COLS: list[tuple[str, str]] = [
    ("episode", "条目"), ("type", "违规类型"), ("joint", "关节/轴"),
    ("frame", "帧"), ("value", "实测值"), ("limit", "极限/说明"),
]
KINEMATIC_TYPES = {"joint_limit": "关节超限", "velocity_limit": "关节超速",
                   "ee_reach": "末端超出可达范围", "ee_translation_velocity": "末端平移超速",
                   "ee_rotation_velocity": "末端旋转超速", "format_or_other": "格式/其它"}

#: 卡死事件明细。
STUCK_COLS: list[tuple[str, str]] = [
    ("episode", "条目"), ("axes", "冻结轴"), ("stuck_start_sec", "卡死开始(秒)"),
    ("stuck_seconds", "卡死时长(秒)"), ("freeze_start_sec", "静止开始(秒)"),
    ("freeze_total_seconds", "静止总时长(秒)"), ("total_frames", "总帧数"),
    ("video_file", "视频文件"),
]

NA_CELL = "—"


def collapse_na_columns(rows: list[dict], cols: list[tuple[str, str]], *,
                        candidates=MOTION_SUBDIMS, reasons: dict | None = None,
                        companions: dict | None = None):
    """整列全空的候选子项 → 从表里拿掉并给出原因;部分为空的 → 保留并记原因。

    返回 (中文表头列表, 中文键的行列表, 不适用{中文名: 原因}, 部分不适用{中文名: 原因})。
    只在有行时判"全空"(没有行谈不上适用不适用);原因取 reasons[key],没有就用通用说法。
    """
    reasons = reasons or {}
    companions = companions or {}
    label = dict(cols)
    drop: set[str] = set()
    na: dict[str, str] = {}
    partial: dict[str, str] = {}
    if rows:
        for key in candidates:
            vals = [r.get(key) for r in rows]
            n_none = sum(1 for v in vals if v is None)
            why = str(reasons.get(key) or GENERIC_NA_REASON)
            if n_none == len(vals):
                na[label.get(key, key)] = why
                drop.add(key)
                drop.update(companions.get(key, ()))
            elif n_none:
                partial[label.get(key, key)] = why
    kept = [(k, lb) for k, lb in cols if k not in drop]
    headers = [lb for _, lb in kept]
    out_rows = [{lb: r.get(k) for k, lb in kept} for r in rows]
    return headers, out_rows, na, partial


def translate_rows(rows: list[dict], cols: list[tuple[str, str]],
                   enums: dict[str, dict] | None = None) -> tuple[list[str], list[dict]]:
    """英文键行 → 中文键行(列序按 cols);enums={英文键: {原值: 中文}} 翻枚举值。"""
    enums = enums or {}
    headers = [lb for _, lb in cols]
    out = []
    for r in rows:
        o = {}
        for k, lb in cols:
            v = r.get(k)
            if k in enums and v is not None:
                v = enums[k].get(v, v)
            o[lb] = v
        out.append(o)
    return headers, out


def subdim_notes_lines(sd: dict | None) -> list[str]:
    """报告/界面共用的"子项适用性"文案(输入 report['dataset']['motion_subdims'])。"""
    if not sd:
        return []
    lines = []
    for name, why in (sd.get("不适用") or {}).items():
        lines.append(f"{name}:本数据集不适用——{why}(明细表不列此列)")
    for name, why in (sd.get("部分不适用") or {}).items():
        lines.append(f"{name}:部分条目不适用,明细表中以「{NA_CELL}」标出——{why}")
    return lines
