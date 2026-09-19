# 数据集语义 profile(可扩展)

未来出现带新规则的数据集,**新增一个 YAML** 即可让系统正确解释它,零代码改动,
不影响已有数据集。未命中任何 profile 的数据集自动回退数值指纹推断。

## 匹配(match):三条件全命中才采用本 profile
- robot_type: 匹配 meta/info.json 的 robot_type
- action_names: 匹配 action 字段名(顺序敏感)
- codebase_version_prefix: 匹配版本前缀(可选)

## action 段
- space: joint | ee
- control_mode: absolute(绝对目标) | delta(增量) | velocity(速度) | unknown
- unit: rad | deg | deg_normalized | normalized | pixel | meter+rad ...(仅记录/报告)
- gripper_dims: 夹爪列下标(如 [6];双臂 [6,13])
- gripper_closed: high(缺省,数值大=闭合,droid 约定)| low(数值大=张开,夹爪**宽度**制,如 umi)。
    仲裁取证找闭合/松爪事件前按它翻转;本体未进规格库时仲裁的夹爪列也从这里取
- angle_dims: 姿态/关节角列下标(差分前解绕/测地用)
- euler_triplet: true=angle_dims 是 EE 的 rpy 三元组(用四元数测地里程表)
- stuck_strategy:
    cmd_delta_vs_pos   —— 绝对目标:diff(action) vs diff(proprio)
    increment_vs_pos   —— 增量:|action| vs diff(proprio)
    velocity_dual_scale—— 速度:各用自身尺度(免物理标定系数)
    abstain            —— 不可判(诚实弃权)
    auto               —— 按 control_mode 自动选

## state 段
- space: joint | ee

## cameras 段(可选):{相机短名: view} 或 {相机短名: {view: ...}}
- view: front | rear | wrist | side | unknown(左右镜像提示、全腕部数据集的打分/复核提示、
    仲裁腕部线/外部线的分路按它给;没声明按相机名含 wrist 判)
- 多夹爪数据集**不需要**相机↔夹爪列对号:仲裁把所有夹爪列的握持段合在一起,按打分层强探针
    的重叠选出"让任务达成的那段",所有相机看同一段(2026-09-18)

## extras(可选):数据集特有元数据,如速度反归一化系数等
