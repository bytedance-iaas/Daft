# 24 · EEF 的轨迹由平台生成，用户不上传 trajectory.json

> 状态：**已落地（2026-10-09）**。来源：需求方 2026-10-09——「我是用户，我不管 trajectory.json」「就是 EEF 任务的时候生成这个文件，
> 并且前端也不能要求有这个文件」。注册表 4.3。

## 1. 做法

建任务只给数据集地址。EEF 模块要的轨迹包（`trajectory.json`，格式见 `docs/contracts/eef/`、设计 12）由模块在运行时自己得到
（`extensions/eef_consistency/derive.py`），写进任务运行目录 `inputs/eef/trajectory.json`；模型请求、报告与裁决里的投影叠加
（`daemon/viz/eef_overlay.py`）都读这一份。来源依次是：

1. 任务参数 `trajectory_json`（仍可传，换一份轨迹时用；控制台不再要求）；
2. **从数据集生成**：数据集带 `meta/umi_calibration.json` 与 `meta/info.json` 时——
   - `observation.state` 里每只手的 `robotN_pos_x/y/z`（TCP 位置，米）、`robotN_rot6d_0..5`（旋转矩阵的前两列，Gram–Schmidt 还原）、
     `robotN_gripper_width`（开口）；
   - `meta/umi_calibration.json` 里每路相机的 `K`、模型与畸变、标定图尺寸、`T_camera_tcp`；
   - 腕部相机位姿 = 手位姿 × `inverse(T_camera_tcp)`；视频尺寸与标定图尺寸之比写成 `H_media_from_calibration`；
     第 j 路相机属于第 j 只手；`horizon_s` 取 1 秒；
3. 数据集根目录自带的 `trajectory.json`。

都没有：预检里 EEF 是 `unsupported`（`trajectory_missing`，写明缺末端位姿与相机标定），不再是 `needs_input`；运行时整个模块失败并写明原因。

## 2. 前提与限制

- 第 2 条的数据只有经 `export-umi` 导出的 LeRobot 数据集才有：`observation.state` 的列名与 `meta/umi_calibration.json`（手写的
  `umi-calibration/1` 标定的拷贝）都是它写的。实测 cups、Trossen 两个数据集，生成结果与 `export-umi` 写出的 `trajectory.json`
  逐帧一致（手位姿与相机位姿零误差）。
- 所以「只给地址」目前只对导出过的数据集成立。客户的原始 UMI / 手持夹爪数据要能直接跑，缺的是相机标定（内参、畸变、相机到 TCP 的
  变换）的来源：从原始会话的 SLAM 日志与标定读、按相机或夹爪型号给缺省、或由客户给——下一步。
- 生成结果里不带 `umi.gripper_range`（开口标定在原始会话里，不在 LeRobot 里）；`umi-action-prompt/9` 起提示词不用它。

## 3. 改动

| 位置 | 改动 |
|---|---|
| C1 注册表 4.3 | `eef_video_consistency.params.trajectory_json` 不再必填 |
| CLI `preflight` | 不传轨迹时按 §1 判断：能生成或有自带文件 → 生成到临时文件校验，`notes` 写明来源；都没有 → `unsupported` |
| CLI `check`（`eef_check`） | 不传轨迹时生成到运行目录再读 |
| Daemon `eef-overlay` | 任务没有上传件时读运行目录里生成的那份（工作目录被清过就先从交付恢复） |
| 前端 | 上传框随参数 Schema 变为可选；模拟数据的预检改为 `available` |
