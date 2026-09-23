# EEF–视频一致性（DEMO 模块，阶段 5）

设计：[docs/design/12-eef-video-consistency.md](../../../../docs/design/12-eef-video-consistency.md)；
输入契约：[docs/contracts/eef/](../../../../docs/contracts/eef/)（`eef-video/1.0.0` 四段 + `trajectory-bundle/1.0` 单文件容器）。
本目录是 B 类新代码，不碰 A 类目录；检测器与观测 provider 只读白名单输入，真值只在仓库外的离线评估器里读。

| 文件 | 内容 |
|---|---|
| `contracts.py` | 枚举、分项、状态、原因码、`Issue`（样本 / 帧 / 相机 / 点 / JSON 路径级定位） |
| `load.py` | `trajectory.json` 读取与校验：严格 JSON → 真值键拒绝 → 容器 Schema → 四段 Schema → 跨段语义 → 媒体存在 → 提供投影与重算投影之差（超 0.05 px 记 `input_inconsistent` 警告，两份都保留、不覆盖） |
| `geometry.py` | SE(3)、四元数、工具点（`fixed` / `linear_gripper`）、针孔 / Brown(5) / 鱼眼(4)、H 变换、投影链 |
| `timeline.py` | 主时间线、线性插值与 SLERP、零阶保持、按缺口分段、按 lag 平移轨迹 |
| `capability.py` | 逐分项能力预检（相机级、episode 级、数据集级）；文件里没有的 episode 报 `unsupported: projection_missing` |
| `motion.py` | L0 数值轨迹：折叠重采样重复帧、按机器人时钟的真实 Δt、线 / 角速度、稳健尖峰、2 Hz 以上高频能量与 1 秒滚动 RMS |
| `video.py` | 按真实 PTS 流式解码一路视图的片段：v3 拼接视频从 `clip_start_s` 起 seek，帧号从片段第一帧记 0，不留整段 |
| `observations.py` | 独立观测：`ObservationProvider` 协议、provider 输入白名单（只有媒体位置、点 id、种子，没有投影 / 位姿 / 标定）、种子文件读取校验、观测文件（observation Schema） |
| `tracking.py` | P-A provider：种子锚点 + 夹爪刚体特征簇的金字塔 LK（逐步前后向校验）；成员由前后两个锚点决定（受种子约束的 RANSAC），逐帧相似变换带种子走，远端锚点的已知误差线性校正，前后向一致才采纳；遮挡、失跟、分歧一律 `uncertain`，不插值冒充观测 |
| `adapters/` | `unified_sample`（三文件目录 ↔ 单文件包条目）、`world_policy`（客户 World_Policy 参考样本 → 形态 C / 纯图像） |
| `__main__.py` | 离线命令：`validate` |

## 手动验证

在 `backend/` 下执行（DEMO 数据在仓库外 `~/ws/ws_general/galbot/`，可用 `CURATOR_EEF_DEMO_DATA` 改位置；
没有这份数据时相关测试自动跳过）：

1. 单测：`../.venv/bin/python -m pytest -q tests/eef`，应全部通过（本机有 DEMO 数据时约 15 秒）。
2. 校验 dataset2 的上传件并看能力表：

   ```bash
   ../.venv/bin/python -m curation.extensions.eef_consistency validate \
     --trajectory ~/ws/ws_general/galbot/dataset2/trajectory.json \
     --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 --all-points-observable
   ```

   应看到 `report.valid: true`、`total_frames: 2009`、`total_points_checked: 28126`、
   `max_reprojection_difference_px` 约 0.013；`capability.availability: available`，七条 episode 都是 `available`。
   去掉 `--all-points-observable` 时位置 / 方向两项是 `needs_input: observation_seed_missing`（还没给观测种子）。
   dataset1 换成 `dataset1/trajectory.json` 与 `dataset1/eef_ds1_lr2`：11 条、3344 帧、13376 个投影点、差约 3e-13 px。
3. 真值隔离：把 `trajectory.json` 复制一份，在任意位置加一个 `"truth": {}` 键再跑 `validate`，
   应退出码 1，`report.errors[0].code` 为 `forbidden_key` 并给出 JSON 路径。
4. 自洽不覆盖：把某帧某点的 `uv_px` 改动 3 px，`validate` 仍通过，但 `report.warnings` 里有一条
   `input_inconsistent`，带相机、点与帧号。
5. 独立观测报告（F5.2，约 1 分钟；在仓库根执行）：

   ```bash
   PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.observation_report
   ```

   逐 episode × 相机打印「点=可见比例/对真值 P95 像素」，最后是汇总与独立性实验：25 路的可见比例中位数约 0.66、
   P95 误差中位数约 3.2 px；两条 `observations_identical: true`（投影平移 30 px，观测逐位不变）。
   种子与真值都是 `synthetic_fixture`，这些数只说明跟踪器在 DEMO 数据上的表现，不是精度验收。
