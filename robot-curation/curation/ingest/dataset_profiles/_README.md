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

## extras(可选):数据集特有元数据,如速度反归一化系数等
