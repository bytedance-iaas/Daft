> 现有数据（dataset1、dataset2、客户 reference/droid 与 reference/LVP）接入统一格式的规则，随 12 篇设计文档进仓库（2026-09-22）。原件在 `~/ws/ws_general/galbot/eef_video_consistency/02-migration.md`。

# 现有数据的接入与迁移

目标是产生新旁路目录，不移动、不覆盖现有 LeRobot、JSONL、标定和评估答案。本交付只转换了两条完整 episode 作为格式样例，其余来源各取一个代表样本；全量转换见实施计划 P1。

## 1. dataset1

| 原字段 | 新字段／处理 |
|---|---|
| `episode_index` | `sample.source.episode_id`，构成稳定 sample ID |
| `timestamp_s`、`frame_index` | 原值保留到主时间线及对应相机局部媒体映射 |
| `source_state_step_index` | `source_state_index`；允许重采样后重复 |
| `source_state_capture_timestamp_ms` | 相机 source_timing；保留“估计采集时刻”语义 |
| `source_robot_read_start_timestamp_ms` | 机器人 source_timing；不是曝光真值 |
| `eef_pose_base.position_m/quaternion_xyzw` | `eef`，`absolute`，`robot_base ← panda_link8` |
| `gripper_closed_fraction` | `gripper.closed_fraction`；无可靠实际宽度，opening 为空 |
| `projection.origin` | `cameras.27432424_left.projection.points.eef_origin` |
| `projection.{x,y,z}_endpoint` | 同一相机的对应端点；轴起点 EEF、轴长 0.08 m |
| `K`、`T_base_camera` | 新相机标定；不再同时保存一份容易不同步的逆矩阵 |
| `visual_measurement=null` | 不创建伪观测；等待独立视觉定位 |

原 JSONL 的欧拉角、旋转矩阵、camera pose 等冗余派生量不进入最小标准；原文件仍保留，需要时从新格式的位姿和标定重算。动作列不复制到 EEF；机器人状态与指令维持区别。

dataset1 当前原生格式为 v2.1，单条 episode 304 帧，11 条共 3,344 帧。工具应读 `meta/info.json` 和真实字段，不靠目录名判断。

## 2. dataset2

| 原字段 | 新字段／处理 |
|---|---|
| `timestamp` | 主 `timestamp_s`，对应它自己的 15 fps 导出视频 |
| `eef.origin_xyz / quat_xyzw` | 绝对 EEF 位姿；单位四元数规范化 |
| `eef.tcp_xyz` | 由明确的 `[0,0,0.16]` 工具偏置说明其意义；不与 origin 混用 |
| `gripper.position` | closed_fraction；open_mm 转米并标 model_assumed |
| `cameras.exterior_1_left / exterior_2_left` | 映射为稳定序列号＋左右目相机 ID |
| `origin_uv / tcp_uv` | 分别为 eef_origin / tcp |
| `axes_uv` | 从 TCP 出发的 0.06 m 轴端点 |
| `fingers_uv[0/1]` | finger_plus_y / finger_minus_y；不推断人体式的左右身份 |
| `depth_m`、`in_view` | 原来只针对 TCP；不能复用到其他点 |
| `z_dir_deg / finger_dir_deg` | 从端点重算，避免派生字段互相矛盾 |
| `truth / corruption_type / fault` | 不进入检测输入；仅离线评估器读取独立文件 |

必须额外读取以下数据：

- Parquet 的 `timestamp_robot_ms`、`timestamp_cam_*_ms`，补充原始时间；不从 `i/15` 反推采集时间。
- v3 episode metadata 的视频文件、chunk/file ID 和 `from_timestamp/to_timestamp`，确定拼接媒体片段。
- `calibration.json` 的 `episodes[episode_index][camera]` 覆盖项，优先于公共外参。

当前目录虽然名叫 `lerobot_v2/`，`meta/info.json` 实际声明 v3.0。状态 `observation.state` 是关节状态，EEF 在 `observation.state.cartesian_position`；适配器不得把前 6 个关节数当作 XYZ＋旋转。

样例使用 episode 6，它的第一路相机声明存在外参偏差，用于验证迁移后错误仍然保留。该信息写在迁移与评估文档中，不能传给检测器。两路相机的有效声明已分别解析进 参考设计的 `examples/dataset2_000006/calibration.json`（仓库外），不再需要运行时猜 override 优先级。

TCP 16 cm、沿局部 Y 开合、85 mm 最大开口都是源构建工具假设，没有独立安装标定证明。位置误差报告必须带上物理点与来源等级；不能直接称作“真实夹指中心偏差”。

原 episode 4 的答案中，图像仿射更新了点，但方向角和入画标记没有同步更新。离线评估适配器应从变换后的点重算派生量；不要修改检测输入以追随答案。原文件的 hash 和修正记录要保留。

## 3. 客户 reference/droid

已知：一个 sample 有四张首帧视图、65 步 XYZ／xyzw／gripper 序列。前三格由来源文字声明为相机画面，第四格是从位姿生成的彩色图。

迁移决策：

1. 保留 sample、view 组织，但 view 3 的 kind 设为 pose_visualization。
2. 相机无真实序列号时使用 `source_view_0/1/2` 等局部稳定 ID，并保留来源映射；之后不能按“左／右”文字猜测外参配对。
3. 原 `eef_delta_gt[3]` 存入 `raw_pose_sequence.json`，不因为字段带 gt 就视作独立视觉真值。
4. 65 行 `eef=null`、`timestamp_s=null`，timebase=index_only；只有第 0 行能映射已存在的首帧图片。
5. 未来补齐单位、参考系、delta 约定、起点姿态后才转入解释后的 eef；补齐实际视频与时钟后才能运行完整时序检查。

尚需客户确认：平移单位与归一化、相对变换顺序、是相邻增量还是相对首帧、gripper 标量定义、视频路径和帧对应、相机标定及物理点定义。未知项已经写入样例，不阻断规范交付。

## 4. 客户 reference/LVP

保留图像和样本信息，eef/gripper/calibration 为空。它可被数据入口接受，但 EEF–视频一致性显示“不具备输入：没有 EEF 或投影”。不能填充零轨迹，也不能把空结果解释成通过。

来源中有 `fps=16` 并不表示存在动态视频；该包只有一帧，新的 image media 不保存虚构时序。

## 5. 跨数据集对齐与异常保留

dataset1 和 dataset2 来自同一原始 episode，但导出方式不同：304 帧对 287 帧，视频末帧时间分别约 20.2 s 和 19.066667 s。各自用各自媒体；比较转换结果时按原始状态映射和原始时间对应，不能按相同行号或相同导出时间硬对齐。

迁移不执行重新标定、时间自动补偿、轨迹平滑、故障标签驱动的修复。允许的表示变换只有明确且可追溯的单位换算、字段组织、四元数规范化和几何派生。源 hash、适配器版本、配置 hash、转换摘要进入迁移 manifest；产品检测器只读标准白名单输入。

## 6. 全量转换的验收

- dataset1：11 条、3,344 行；dataset2：7 条、2,009 行；客户参考各 10 个 sample 全部可收录。
- 每个 episode 视频定位、相机、帧数、时间、点定义与源记录逐一核对；v3 拼接视频不能总从 0 秒读取。
- 未注入转换误差：dataset1 几何差应接近浮点误差；dataset2 在源舍入容差内，当前样例最大约 0.0132 px。
- episode 6 的声明外参偏差仍在；同一视频的其它机位不被替换或污染。
- 在隔离的检测目录中不存在答案字段；VLM 输入不包含故障名称、合成参数、baseline 参考几何、源码注释或 dataset notes。
- 原目录的 SHA-256 不变；输出写临时目录、校验完成后原子提交；失败时留下可定位的错误，不覆盖已有成品。
