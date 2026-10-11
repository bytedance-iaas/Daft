# 数据集语义 profile（可扩展）

未来出现带新规则的数据集，**新增一个 YAML** 即可让系统正确解释它，零代码改动，
不影响已有数据集。未命中任何 profile 的数据集自动回退数值指纹推断。
读取在 `ingest/dataset_semantics.py`（`resolve_semantics`），结果随行流到各检查；下面写明每个字段在 v2 里谁读。

## 匹配（match）：三条件全命中才采用本 profile
- robot_type：匹配 meta/info.json 的 robot_type
- action_names：匹配 action 字段名（顺序敏感）
- codebase_version_prefix：匹配版本前缀（可选）

## suggest_embodiment（可选）
- info.json 的 robot_type 认不出时，预检建议的本体型号

## action 段
- space：joint | ee
- control_mode：absolute（绝对目标）| delta（增量）| velocity（速度）| unknown。
    运动学极限与运动质量按它选判据
- unit：rad | deg | deg_normalized | normalized | pixel | meter+rad ...（只记在读出的行上，v2 的检查与报告都不读）
- gripper_dims：夹爪列下标（如 [6]；双臂 [6,13]）
- gripper_closed：high（缺省，数值大 = 闭合，droid 约定）| low（数值大 = 张开，夹爪**宽度**制，如 umi）。
    原来给 v1 的仲裁取证找闭合 / 松爪事件用，仲裁已删（D71）；v2 没有代码按它判定，
    它只和夹爪列一起写进行上的 semantics_extras（`gripper`），任务成败判定的请求把整份 extras 原样放进提示词的 "Camera guidance"
- angle_dims：姿态 / 关节角列下标（差分前解绕 / 测地用）
- euler_triplet：true = angle_dims 是 EE 的 rpy 三元组（用四元数测地里程表）
- rotation_blocks / translation_dims：非 rpy 的姿态表示（如 umi 的 rot6d）按块声明。
    声明了 rotation_blocks 时，angle_dims、euler_triplet、gripper_dims 连同它一起写进 extras 的 `layout`，运动质量按块处理；
    没声明的 profile，运动质量走老规则（EE 取第 3–5 列 rpy，关节取全部列），angle_dims / euler_triplet 不起作用
- stuck_strategy（运动质量的执行器卡死检测）：
    cmd_delta_vs_pos   —— 绝对目标：diff(action) vs diff(proprio)
    increment_vs_pos   —— 增量：|action| vs diff(proprio)
    velocity_dual_scale—— 速度：各用自身尺度（免物理标定系数）
    abstain            —— 不可判（诚实弃权）
    auto               —— 按 control_mode 自动选

## state 段
- space：joint | ee

## cameras 段（可选）：{相机短名: view} 或 {相机短名: {view: ...}}
- view：front | rear | wrist | side | unknown
- 原来按它给左右镜像提示、全腕部数据集的提示和仲裁的腕部线 / 外部线分路（v1 的 `camera_view.py`），这些代码已删（D71、D89）。
    v2 没有代码按 view 分支，它只写进行上的 semantics_extras（`cameras`），同样随整份 extras 进任务成败判定提示词的 "Camera guidance"

## extras（可选）：数据集特有元数据
- 运动质量读其中的 `velocity_scale_translation_empirical`（droid 的经验速度系数）；读取时还会往 extras 里加 `velocity_calibration`
    （末端速度 / 增量指令的数据集按样本行标定，执行器饱和要用），这一项不写在 profile 里
- 其余键（如 `note`）只随 semantics_extras 流到行上，和整份 extras 一起进任务成败判定的提示词
