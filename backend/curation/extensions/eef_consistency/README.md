# EEF–视频一致性（DEMO 模块，阶段 5）

接入 v2（F5.4）：注册表 1.4 的两个建议性模块 `eef_video_consistency`（frame 档）与 `eef_video_review`（vlm 档，第一刀不提供）；
命令行的 `check` 分派在 `backend/curation/cli/eef_check.py`，模块参数 `--param` 在 `backend/curation/cli/modparams.py`，
`aggregate` 在调用边界把它滤出判决，`report` 给它一节建议性摘要。

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
| `metrics.py` | 位置残差（像素与毫米等效）、方向夹角（有向 0–180°、无向 0–90°，投影过短为 not_observable）、全局与滑窗 lag（`u_visual(t) ≈ u_declared(t+lag)`，同一掩码、去常量偏移、亚帧抛物线）、残差高频占比 |
| `motion.py`（续） | 背景 / 相机运动：半分辨率逐帧背景特征 + RANSAC 相似变换累积成画面轨迹（遮掉独立观测到的夹爪，不用投影），1 Hz 以上滚动 RMS |
| `segments.py` | 迟滞分段（开 / 关阈值、最短持续、允许短缺口）、滚动中位数、证据帧挑选 |
| `assess.py` | 分项状态：`ok / suspect / unknown / unsupported / error`；无 profile 只出曲线（`threshold_uncalibrated`），覆盖不足 `unknown`；episode 级只做「任一相机 suspect 即候选」汇总 |
| `diagnosis.py` | 诊断假设（只在对应分项已 suspect 时算，不改状态）：PnP 外参修正、时间偏移、恒定朝向错（三维拟合 EEF 本体系恒定旋转）、TCP 轴向偏移（一维拟合）、位姿漂移、抖动来源 |
| `profile.py`、`profiles/demo.yaml` | 阈值 profile；`demo` 标 `calibrated: false`，每个数都注明来自哪条基准的噪声底、乘了多少倍 |
| `runner.py` | 单 episode 的流式执行：解码 → 观测 → 测量 → 判定 → 诊断 → 产物（`observations/`、`curves/*.parquet`、`evidence/` 叠加图），输出 §11.3 的 `detail` |
| `preflight.py` | `curation preflight` 里两个模块的条目：文件校验、逐分项能力表、按 episode 计数；没给文件报 `unsupported: trajectory_missing`（第一刀界面拿不到文件），复核模块一律 `unsupported` |
| `report.py` | 报告小节摘要（候选、各分项可疑 / 无法评估的条数、被支持的诊断、覆盖率）与三张表 `eef_camera_metrics` / `eef_segments` / `eef_diagnosis` |
| `adapters/` | `unified_sample`（三文件目录 ↔ 单文件包条目）、`world_policy`（客户 World_Policy 参考样本 → 形态 C / 纯图像） |
| `__main__.py` | 离线命令：`validate`、`run` |

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
6. 离线评估一条 episode（F5.3，在 `backend/` 下）：

   ```bash
   ../.venv/bin/python -m curation.extensions.eef_consistency run \
     --trajectory ~/ws/ws_general/galbot/dataset2/trajectory.json \
     --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 \
     --seeds ~/ws/ws_general/galbot/dataset2/observations_seed --episodes 0 5 6 --out /tmp/eef_run
   ```

   每条打印 `overall` 与分项状态：ep0 全 `ok`（`overall: assessed`）；ep5 `temporal_alignment: suspect`；
   ep6 `position_2d: suspect`。`/tmp/eef_run/details.jsonl` 里 ep6 的 `cameras` 只有 `27432424_left` 的位置是 suspect，
   `diagnosis` 里 `extrinsics_error` 为 `supported: true`、`delta_translation_mm` 约 30、`delta_rotation_deg` 约 2；
   ep5 两路的 `lag_s` 约 +0.33。`evidence/000006/27432424_left/*.jpg` 上红圈是声明投影、绿叉是独立观测。
   `--profile none` 时所有分项都是 `unknown`（`threshold_uncalibrated`），只出曲线。
7. 受控异常矩阵（F5.3 验收，约 2 分钟；在仓库根执行）：
   `PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.matrix`，应打印 18 行 `PASS`、`"cells_ok": 114`
   与 4 行轻重档 `PASS`；说明见 [tools/eef_eval/README.md](../../../../tools/eef_eval/README.md)。
8. 在 v2 命令行链路上跑（F5.4，在 `backend/` 下，约 1 分钟）：

   ```bash
   G=~/ws/ws_general/galbot/dataset2; R=/tmp/eef_cli; mkdir -p $R
   P="--param eef_video_consistency.trajectory_json=$G/trajectory.json"
   ../.venv/bin/python -m curation.cli preflight --json --input $G/eef_ds2_lr3 --modules timestamp_check,eef_video_consistency $P > $R/preflight.json
   ../.venv/bin/python -m curation.cli plan --preflight $R/preflight.json --modules timestamp_check,eef_video_consistency --episodes 0-6 --out $R/plan.json
   ../.venv/bin/python -m curation.cli check --modules timestamp_check --input $G/eef_ds2_lr3 --run-dir $R --episodes 0-6
   ../.venv/bin/python -m curation.cli check --modules eef_video_consistency --input $G/eef_ds2_lr3 --run-dir $R --episodes 0-6 $P
   ../.venv/bin/python -m curation.cli aggregate --run-dir $R --phase funnel --revision 1 --episodes 0-6
   ../.venv/bin/python -m curation.cli aggregate --run-dir $R --phase final --revision 1 --episodes 0-6 --input $G/eef_ds2_lr3
   ../.venv/bin/python -m curation.cli report --run-dir $R --revision 1
   ```

   `preflight.json` 里 `eef_video_consistency` 是 `available`，带 `subitems` 与 `episode_counts: {available: 7}`（不给 `--param` 时是
   `unsupported: trajectory_missing`）；`plan.json` 多一个 `advisory_frame` 阶段、`episodes: selected`；EEF 的 `check` 打印
   「abstain 7」（建议性模块不投票）；`aggregate` 的 keep / 终判清单与不选这个模块时相同；`revisions/r0001/report.md` 里
   「EEF–视频一致性」一节写着「建议性结果，不影响判决（阈值未校准）：候选 6 · 全部可评估 1 …」，`tables/` 下有
   `eef_camera_metrics`、`eef_segments`、`eef_diagnosis` 三张表（ep6 只有 `27432424_left` 位置 suspect，诊断 `extrinsics_error`）。
