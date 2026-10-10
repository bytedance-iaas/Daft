# EEF–视频一致性（DEMO 模块，阶段 5）

接入 v2（D49，F5.9）：一个模块 `eef_video_consistency`（vlm 档）：每条先由 CPU 测量，再请模型复核（一点一轴的请求包）——没有夹爪参考时请模型对整段
标记视频给意见、腕部相机看自运动；注册表 5.0 起（设计 25，D81、D82）只给**带置信度的意见、不判废**：`channels.py` 把每个渠道换成「结论 + 置信度」，
`combine.py` 按分项 × 相机取大合并，冲突才出裁决卡（此前是 `decide.py` 按设计 12 附录 C.9 判过、判废或转人工）；命令行里它和任务成败判定同在 vlm 档，
由 `pipeline/check_stage.py` 的 `StageRun` 逐条调用 `backend/curation/cli/eef_check.py` 的 `EefJudge`（流水线模式下同样逐条交接），
模块参数 `--param` 在 `backend/curation/cli/modparams.py`；`aggregate` 在调用边界把它作为一票否决项加进 v1 的判决配置（A 类不动）。
（F5.4–F5.8 期间是两个建议性模块 `eef_video_consistency` / `eef_video_review`，不影响判决。）

设计：[docs/design/12-eef-video-consistency.md](../../../../docs/design/12-eef-video-consistency.md)；
输入契约：[docs/contracts/eef/](../../../../docs/contracts/eef/)（`eef-video/1.0.0` / `1.1.0` 四段 + `trajectory-bundle/1.0` 单文件容器；1.1.0 只多手持夹爪的 `umi.world_frames`）。
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
| `mcap_media.py` | mcap 数据集（F5.13）：`media.topic` 指定的图像 topic 按 log_time 顺序转成本地 mp4（JPEG 封装、H.264 重封装，不重编码；复用 `ingest/mcap_reader` 的做法），帧号就是消息序号；一条 episode 判完即删。`observations.media_frames` 是两种媒体共用的取帧入口 |
| `observations.py` | 独立观测：`ObservationProvider` 协议、provider 输入白名单（只有媒体位置、点 id、种子，没有投影 / 位姿 / 标定）、种子文件读取校验、观测文件（observation Schema） |
| `template.py` | 夹爪外观模板（`gripper-template/1.0`，F5.8）：读取与校验（Schema、PNG 小图与掩膜解码、每条目 ORB 特征）；`Redetector` 在整幅画面里按相机匹配条目（比率检验 + RANSAC 相似变换、内点下限与比例、尺度范围、歧义放弃、固定随机种子）；`rigid_member_mask` 用跟踪器的成员规则从两个锚点间的帧算夹爪掩膜；`make_entry` / `make_template` / `check` |
| `tracking.py` | P-A provider：种子锚点 + 夹爪刚体特征簇的金字塔 LK（逐步前后向校验）；成员由前后两个锚点决定（受种子约束的 RANSAC），逐帧相似变换带种子走，远端锚点的已知误差线性校正，前后向一致才采纳；遮挡、失跟、分歧一律 `uncertain`，不插值冒充观测 |
| `metrics.py` | 位置残差（像素与毫米等效）、方向夹角（有向 0–180°、无向 0–90°，投影过短为 not_observable）、全局与滑窗 lag（`u_visual(t) ≈ u_declared(t+lag)`，同一掩码、去常量偏移、亚帧抛物线）、残差高频占比 |
| `motion.py`（续） | 背景 / 相机运动：半分辨率逐帧背景特征 + RANSAC 相似变换累积成画面轨迹（遮掉独立观测到的夹爪，不用投影），1 Hz 以上滚动 RMS |
| `segments.py` | 迟滞分段（开 / 关阈值、最短持续、允许短缺口）、滚动中位数、证据帧挑选 |
| `history.py` | 普通 EEF 和 UMI 共用的历史时间窗、提示词说明；普通 EEF 在当前 P/A/B 下方画同一点过去 1 秒的青色轨迹，所有历史点重投影到当前相机；缺失、相机后方、时间断档不连线；缺少三维定义或与声明 P 冲突时只保留当前标记 |
| `assess.py` | 分项状态：`ok / suspect / unknown / unsupported / error`；无 profile 只出曲线（`threshold_uncalibrated`），覆盖不足 `unknown`；episode 级只做「任一相机 suspect 即候选」汇总 |
| `diagnosis.py` | 诊断假设（只在对应分项已 suspect 时算，不改状态）：PnP 外参修正、时间偏移、恒定朝向错（三维拟合 EEF 本体系恒定旋转）、TCP 轴向偏移（一维拟合）、位姿漂移、抖动来源 |
| `profile.py`、`profiles/demo.yaml` | 阈值 profile；`demo` 标 `calibrated: false`，每个数都注明来自哪条基准的噪声底、乘了多少倍；`record` 段是记录比对的阈值 |
| `robots.py` | 记录比对用的内置正向运动学（设计 12 §8.7）：Franka Panda / FR3 的改进 DH，链末是法兰 `panda_link8`，另有具名帧 `panda_hand`（法兰绕 z 转 −45°）与 `panda_hand_tcp`（再沿 z 0.1034 m）；dataset2 上与位姿列逐帧重合 |
| `record_draft.py` | 记录映射的起草（设计 12 §8.7，D-E17）：按 LeRobot `info.json` 的分量名与 `robot_type` 起草位姿与关节角来源（只看 `observation.*` 的实测列，专用列优先于拼接列、拼接列写切片），逐条给出推断的地方（代码 + 参数），有歧义或认不出时不起草并说明原因；CLI 预检放进 EEF 条目的 `drafts.record_mapping` |
| `record.py` | 轨迹与数据集记录（设计 12 §8.7，D-E16，只报告、不参与判决）：`eef-mapping/1.1` 的 `record` 块（1.0 映射的 `eef` 块就是位姿来源）；读 LeRobot 列或 mcap topic；逐帧对齐（`source_state_index` > 帧数相同按帧号 > 时间戳插值）；主指标是按声明关系算的原始残差；工具侧拟合恒定差并与声明比（`constant_mismatch`，推不出期望关系时只报告）；扣掉常量后迟滞分段（`record_deviation`）；时间差（正值 = 记录晚）；数据集内部位姿列与关节角正解互比；有标定时画叠加证据图 |
| `runner.py` | 单 episode 的流式执行：解码 → 观测 → 测量 → 判定 → 诊断 → 产物（`observations/`、`curves/*.parquet`、`evidence/` 叠加图），输出 §11.3 的 `detail`；`attach_record` 加上 `details.record`（没有夹爪参考的意见路径也调它） |
| `channels.py` | 四个渠道（设计 25 §6）：CPU 测量（第三视角，读数超过开阈值的幅度 0 → 3 倍开阈值为 1，× 覆盖率）、自运动（腕部）、模型复核（CPU 可疑的分项看它的候选窗口，其余看别的窗口；按票差定结论，置信度 = \|票差\| / 有答复 × 有答复占比）、模型意见（片段里模型自己的不匹配置信度，按答复占比向 0.5 收）；结论 `issue` / `ok` / `cannot_tell` 换成不一致置信度 p = 0.5 ± 0.5 × 置信度 |
| `combine.py` | 合并（设计 25 §7，D82）：按分项 × 相机取两边的最大 p；一边 ≥ 高档、另一边 < 低档即「冲突」；只有一边时封顶并写明缺的是哪边、为什么；模型多数认为跟错目标时作废 CPU；episode 取最差的格（不跨相机平均），给标签、p 与一句依据；档位与封顶在 profile 的 `merge` 一节（demo 未校准） |
| `preflight.py` | `curation preflight` 里的模块条目：文件校验、逐分项能力表、按 episode 计数；没给文件时由 `cli/preflight` 先按设计 24 生成（或按设计 22 §5.4 走手持夹爪 mcap 的推导）；算不出来时报 `needs_input: trajectory_missing`（`input_hint.field = trajectory_json`，控制台第二屏必填）——能算就可选、算不了就必选；复核模块跟随它复核的模块（不可用报 `eef_base_unavailable`、缺文件同样要上传），再要 VLM 后端 |
| `opinion.py` | 没有夹爪参考时的模型意见（设计 12 §10.5，D-E15）：每路参与的相机整段视频逐帧画上声明的夹爪中心 P（红圈，默认点 `tcp`）、接近方向 A（红箭头，默认轴 `z`，投影太短就换最长的轴或不画）与手指连线 B（橙线，`finger_line` 或 `y`，以 P 为中心向两侧画；绕接近方向的转动只有它看得出来），印帧号，超过 60 秒按 60 秒切段，每段一个请求；模型列出不匹配的片段、各自的不匹配置信度与证据帧（答复 Schema `eef/opinion_output.schema.json`，帧号必须在本段内、证据帧在自己的片段里，不合格给一次修复）；标记视频与证据帧都不落盘（设计 20），记录只留视频元数据与证据帧号；送模型的视频长边上限 448；记录一律 `passed=true`（只给意见、不参与判决）；`summary()` 给报告的意见统计 |
| `review.py` | VLM 复核：窗口（同分项、时间重叠的 CPU 位置 / 朝向候选段合并成候选窗口，加均匀抽查窗口，每路相机各至多 N 个、超出记 `truncated`）；**每个窗口只问一个点 P 和至多一根轴 A**（候选窗口问 CPU 偏得最厉害的点 / 轴，抽查窗口问覆盖最好的点；轴在窗口里投影不足 20 px 就换最长的一根，都不够就不问朝向）；请求包：缩小的整帧、每帧原始裁剪与标记裁剪（声明的 P 红圈、跟踪到的 P 绿十字、声明的 A 红箭头，都标名字），prompt 只给这一点一轴的定义；答复校验（`eef/review_output.schema.json` 1.1、帧号必须来自请求、解释里不许有测量值，不合格给一次修复）、按发送内容缓存、`votes` 把答复变成分项投票 |
| `report.py` | 报告小节摘要：5.0 起 `merged_summary`——按标签与 p 分档的条数、冲突条数、单渠道缺的是什么、判断不了的原因、跟错目标作废的格数，明细表 `eef_opinions`（逐条 × 分项 × 相机）；5.0 以前的记录仍有判过 / 判废 / 转人工条数与原因；另有候选、各分项可疑 / 无法评估的条数、被支持的诊断、覆盖率、复核窗口（有答复 / 失败、冲突）；表 `eef_camera_metrics` / `eef_segments` / `eef_diagnosis` / `eef_review_windows`；`record_summary` / `record_rows` 是轨迹与数据集记录的摘要与明细表 `eef_record` |
| `adapters/` | `unified_sample`（三文件目录 ↔ 单文件包条目）、`world_policy`（客户 World_Policy 参考样本 → 形态 C / 纯图像）、`lerobot_mapping`（按显式的 `eef-mapping/1.0` 映射从 LeRobot 列生成 `trajectory.json`，形态 B，设计 §3.3；不是平台入口）、`umi`（原始 UMI 会话 → LeRobot 与上传件，设计 20）、`umi_mcap`（DAS / GenRobot 手持夹爪 mcap + `umi-calibration/2` → 上传件与 `umi-export-report.json`，设计 22 §5.2） |
| `umi.py` | 手持夹爪：`load_hands`（`shared` / `per_hand` 世界系、缺测）、`fill_gaps`（前后有效位姿相隔不超过插值最大间隔时补，缺省 3 个样本间隔、容半个间隔）、本手投影、提示词 |
| `derive_mcap.py`、`calibrations/das_gripper_demo.json` | 原始手持夹爪 mcap 的轨迹（设计 22 §5.4，D80；设计 24 的生成之外）：逐条按需用 `umi_mcap.episode_bundle` 从录制推出（本地文件或流式读 TOS），缓存、线程安全；单条轨迹包与导出报告写进 `checks/eef_video_consistency/trajectory/`；标定用上传的或内置的 DAS DEMO 假设值（不带客户相机的内参） |
| `egomotion.py` | 腕部相机自身的运动：画面（ORB、去畸变、本质矩阵）对位姿推出的相对运动，旋转差与平移方向差；`export-umi-mcap` 的报告用它抽查帧对（设计 22 §7 第 2 行）；意见模式下整条检查（F5.19）：每三分之一窗口一对帧、每张画面只提一次特征，逐对旋转差、错开位姿找时间差、滚动中位加迟滞分段，阈值在 profile 的 `ego_motion` 一节（未校准） |
| `__main__.py` | 离线命令：`validate`、`run`（`--seeds` 或 `--template`）、`export`、`export-umi`、`export-umi-mcap`、`template-build`、`template-check` |

## 手动验证

### UMI 原始数据的动作投影（设计 20）

UMI 原始会话先转换为 LeRobot 与带双手轨迹的 EEF 上传件。需要 `dataset_plan.pkl`、`demos/` 内各路
`raw_video.mp4`、`camera_trajectory.csv` 和显式标定。直接读取 plan 的绝对位置/轴角与相机 CSV，不从视频拟合轨迹。
测试集 `DecisionFacts/Physical-AI-UMI-transfer-objects-from-cups` 的标定示例在
`backend/tests/eef/mappings/umi_cups.json`（只适用于该采集会话；内参取 SLAM 日志，世界变换取 `tx_slam_tag.json`）。

仓库根目录运行：

```bash
PYTHONPATH=backend .venv/bin/python -m curation.extensions.eef_consistency export-umi \
  --dataset-root /path/to/transfer-objects-from-cups \
  --calibration backend/tests/eef/mappings/umi_cups.json \
  --horizon-s 1 --out /path/to/umi-lerobot
```

输出目录必须不存在。默认把原始鱼眼画面等比例缩到最长边 960，保留 plan 选中的全部帧；`--max-side` 控制尺寸。
`--horizon-s 0` 关闭历史轨迹，只画当前几何。会话里有 `demos/gripper_calibration_<相机序列号>_*/gripper_range.json` 时，按序列号把每只手的开口标定写进 `sample.umi.gripper_range`（设计 20「开口标定」；`umi-action-prompt/8` 起只判朝向，这段标定不再进提示词）。标定 JSON 的 `cameras.cameraN` 必须与 plan 顺序一致，
并明确 `K`、`model`、`distortion_coefficients`、`image_size_wh`、`T_camera_tcp`；`T_world_slam` 把 CSV 坐标转到 plan 坐标。
换参考系由 `adapters/umi.py` 的 `slam_to_tag()` 完成：传入 SLAM 系下的 4×4 位姿（或 N×4×4 序列）和 SLAM → tag 变换，
返回 tag 系下的位姿；测试集的变换为 `inverse(tx_slam_tag)`。plan 中已经对齐的 TCP 不再转换。
Camera→TCP 参数作为显式几何来源保存，投影相机位姿取 CSV，不用 EEF 反推相机来掩盖两者的不一致。

在控制台登记输出目录，勾选 EEF–视频一致性，上传输出的 `trajectory.json`，选择视频模型，不给观测种子/模板。
任务自动使用 UMI 提问：每路只叠加 `umi.camera_hands` 指定的本手（camera0 为蓝色 robot0，camera1 为紫色 robot1），
包含当前中心/朝向/开口与过去 1 秒到当前帧的历史轨迹；另一只手不画标记或文字。模型只看 MARKED 连续视频（`umi-action-prompt/9`），只评估本手接近轴的朝向。
标记视频只在内存里编码后内联发给模型，不保存；报告、裁决卡、任务详情的迷你播放器放原始视频，标记由 Daemon 现场算（`overlay.py`，接口 `GET /tasks/{id}/episodes/{index}/eef-overlay`）、浏览器叠加（设计 22 §3）：除了送模型的那几样，还有三根坐标轴、未来轨迹与判决模式的观测点；每层带 `id` / `group` / `default_on` / `in_model`，播放器的「叠加」菜单按它们勾选。
`action` 类疑点表示动作或抓放时机与可见画面不符；沿用建议性意见，不因此自动判废。
该测试集使用 `doubao-seed-2-1-pro-260915` 的 `minimal` 推理设置完成真实验证；默认推理曾超时。
受控外参和时间偏移仍存在漏检，详细结果见设计 20，不能把模型意见当作已校准的自动判定器。

验证：`cd backend && ../.venv/bin/python -m pytest -q tests/eef/test_umi.py`。
实际像素重投影、当前帧相机锚定、坐标系变换抵消、双手独立性、缺测断线、视频帧对齐和动作证据帧均有回归。

#### 本地手动检查叠加视频

`data/umi-test/render_own_hand.py` 调用正式的 CPU 渲染和连续视频编码路径，不调用 VLM。
先用上面的 `export-umi --horizon-s 1` 生成数据，再在仓库根目录执行：

```bash
PYTHONPATH=backend .venv/bin/python data/umi-test/render_own_hand.py \
  --dataset-root /path/to/umi-lerobot --episode 0 --out data/umi-test/history-hand
.venv/bin/python data/umi-test/build_preview.py \
  --input data/umi-test/history-hand --out data/umi-test/preview
.venv/bin/python -m http.server 8081 --directory data/umi-test/preview
```

打开 `http://127.0.0.1:8081/`，确认每路只有本手标记，曲线只含过去 1 秒到当前帧，夹爪静止时不提前出现后续动作。
渲染器默认读 `<dataset-root>/trajectory.json`，可用 `--trajectory` 指定其他上传件；`--max-side` 默认 720。
历史窗口取上传件里的 `umi.horizon_s`，预览页显示实际值。每路输出一个 `*_marked.mp4`，
`manifest.json` 记录 episode、相机/手对应关系、帧数、时间范围、摘要和提示词版本。
默认参数适用于 `data/umi-test/lerobot`；构建预览页默认输出到 `frontend/dist/umi-preview`，
已有本地 Daemon 服务时可访问 `/curation/umi-preview/index.html`（应在前端 build 后生成，避免被清理）。
脚本已入库，数据、生成的 MP4、JSON 和 HTML 仍由 `data/` 忽略规则排除。
控制台正式报告不依赖此静态预览页：它播放原始视频，标记在线叠加（设计 20）。

### 手持夹爪 mcap（DAS / GenRobot，设计 22 §5.2）

每只手 `robotN` 有 `/robotN/vio/eef_pose`（VIO 机体位姿）、`/robotN/sensor/magnetic_encoder`（开口）、`/robotN/sensor/camera0/compressed`
（腕部鱼眼，H.264）与通常有的 `/robotN/sensor/camera0/camera_info`。录制里没有的（位姿是什么、机体→光学的旋转、相机→指尖中心、开口单位、
缺 `camera_info` 时的内参）写在每种夹爪一份的标定文件里（`umi-calibration/2`，Schema `docs/contracts/eef/umi_calibration.schema.json`，
示例 `docs/contracts/examples/eef-umi-calibration.json`；DAS 的 DEMO 标定在仓库外 `~/ws/ws_general/galbot/umi/das_gripper_demo.json`）。

仓库根目录运行（`--mcap-root` 是数据集目录，episode 编号与平台相同：`episode_<N>.mcap` 按名，否则按排序）：

```bash
PYTHONPATH=backend .venv/bin/python -m curation.extensions.eef_consistency export-umi-mcap \
  --mcap-root /path/to/mcap-dataset --calibration /path/to/das_gripper.json --out /path/to/out/trajectory.json
```

stdout 是一行 JSON（样本数、每条的状态与行数、可疑项数、按假设值的字段）；`trajectory.json` 写出前按平台上传的标准校验过，
旁边是 `umi-export-report.json`：每路相机的配对率、缺测段与低频段（episode 时间）、首个能解的帧、内参来源（`camera_info` / 回退）与换到视频后的
fx / fy，每只手的开口范围，抽查帧对的自运动（画面估计的转动对位姿的转动），以及 `suspects`（设计 22 §7 第 2、3、7、8 行）。
`--no-ego-check` 跳过自运动抽查（快很多）；`--episodes 0 3` 只导出几条。

检查：

1. 报告里每条 `status: ok`；有 `camera_info` 的相机 `intrinsics: camera_info`，没有的走回退；标定文件去掉 `intrinsics_fallback` 再导一次，
   缺 `camera_info` 的相机变成 `unsupported` 并写明原因，其他相机照常导出。
2. 标定文件的 `body_to_optical` 改成单位阵再导一次：报告出现 `row: 2` 的 `calibration_suspect`（画面转动与位姿对不上）。
3. 控制台登记这个 mcap 数据集（本地挂载、内置 UMI 模版），新建任务勾 EEF–视频一致性、上传 `trajectory.json`、不给夹爪参考、选视频模型；
   跑完后报告 Episode 明细的「模型意见」写的是手持夹爪的说明和「位姿缺测：前后相隔不超过 100 ms 的已插值补上（…）」；
   点「可视化」，每路相机只画本手（中心点、两指连线与开口、坐标轴、过去轨迹），右侧「详细信息」的「叠加」一节有「插值最大间隔」，
   改大后有缺测的地方轨迹连上、改小后断开（只影响画面）。

4. 同一个任务的 Episode 明细，「模型意见」下面是「自运动一致性（腕部相机）」：好 / 不好与一句话，每路腕部相机的旋转差中位与 P95、时间差与置信、
   覆盖率、不一致的片段（帧、秒、原因、轻 / 中 / 重、证据帧）和画面匹配不足的段落，注明「阈值未校准」和「按假设值」。`00001(1).mcap` 两路一致、
   旋转差中位 0.5–0.6°；把导出文件里 robot1 的位姿整体后移 15 帧（0.5 s）再跑：这一路「不一致」，时间差约 +0.47 s；用 `body_to_optical`
   写成单位阵的标定重新导出再跑：片段全是「重」。报告 EEF 小节多「读过自运动」「与位姿不一致」两个数和两张片段图，有不一致时发现里多一条
   info 级的「腕部相机的运动与记录的位姿不一致」（不影响判决）。

5. 不传 trajectory.json（设计 22 §5.4）：同一个数据集新建任务，勾 EEF–视频一致性，第二屏 trajectory.json 留空（现在是可选项），「夹爪标定」也留空，
   选视频模型。预检通过；跑完后 Episode 明细的 EEF 区块第一行是「轨迹由平台从录制推出 · 内置的 DEMO 标定（das_gripper）· 按假设值：…」，
   下面每只手一行配对率与内参来源；`00001(1).mcap` 的模型意见、自运动与传导出文件的任务一致；`umi_sample.mcap` 两路写「不支持（这一路没有
   camera_info，标定文件里也没有它的 intrinsics_fallback）」，这一条不问模型、不转人工（「模型意见」块写「没有问模型，也不转人工」，
   记录的 `opinion.status` 是 `not_assessable`），任务的待裁决是 0。再建一个任务，「夹爪标定」传带 `intrinsics_fallback` 的那份（如本机的
   `das_gripper_demo.json`）：`umi_sample` 两路可看。运行目录 `checks/eef_video_consistency/trajectory/` 里每条一个 `episode_<N>.json` 和
   `.report.json`；迷你播放器的叠加照常。LeRobot 数据集（`eef_ds2_lr3`）不传 trajectory.json 时预检是「需要上传」（平台算不出它的轨迹），模块照常可勾选，第二屏 trajectory.json 必填。

验证：`cd backend && ../.venv/bin/python -m pytest -q tests/eef/test_umi_mcap.py tests/eef/test_umi.py tests/eef/test_egomotion.py tests/eef/test_derive_mcap.py`
（合成的仿 DAS 录制在 `tests/eef/das_mcap.py`，不依赖客户数据；`test_egomotion.py` 的真数据用例在本机有 DAS 录制与导出时才跑）。

### 按数据集声明生成机械臂的轨迹（设计 25 §3–§4.1，F5.23）

在 `backend/` 下执行。dataset2 的声明草稿缺两路相机的内参，补上（数据集外的 `calibration.json`）、工具改成 dataset2 的值后，平台从位姿列生成轨迹：

```bash
D=~/ws/ws_general/galbot/dataset2
../.venv/bin/python -m curation.cli preflight --input $D/eef_ds2_lr3 --modules eef_video_consistency --vlm-backend ark --json \
  | jq '.modules[0] | {availability, reason_code, src: .trajectory_source.kind, missing: [.trajectory_source.missing[]?.code]}'
```

没有声明时是 `needs_input` / `declaration_incomplete`、`missing_declaration`，缺 `intrinsics_missing`（两路）与 `no_drawable_camera`。用 `tests/eef/test_declaration.py`
里 `test_dataset2_generated_from_its_declaration_is_the_reference` 的做法写一份声明（`curation.declaration.draft.draft_lerobot` 起草，补 `fx_cx_fy_cy` 与工具；种子文件按相机序列号编号，两路相机的 `camera_id` 改成 `27432424_left`、`28221883_left` 才对得上），
存成 `decl.json`，再带 `--declaration decl.json` 预检：`available`、`generate`。`check` 带同一个 `--declaration` 与 `--param eef_video_consistency.observation_seeds=$D/observations_seed`
跑 ep 0 与 6：`checks/eef_video_consistency/trajectory/episode_000000.json` 等逐条生成；ep0「一致」，ep6 位置「不一致」且诊断支持 `extrinsics_error`
（与上传 `trajectory.json` 的结果相同）；`details.trajectory_source.kind` 是 `generated`，记录比对 `source: internal`（位姿列对关节角正解）。

### 不用模型、相机与适用参数（设计 25 §4.2–§4.3，F5.24a）

接上一节（`decl.json` 已写好），在 `backend/` 下：

```bash
P="--param eef_video_consistency.observation_seeds=$D/observations_seed"
../.venv/bin/python -m curation.cli preflight --input $D/eef_ds2_lr3 --modules eef_video_consistency --declaration decl.json --json \
  | jq '.modules[0] | {availability, cams: [.cameras[] | [.camera_id, .mount, .drawable]], params: .applicable_params, notes: [.notes[] | select(startswith("vlm_backend_missing"))]}'
R=$(mktemp -d)
../.venv/bin/python -m curation.cli check --modules eef_video_consistency --input $D/eef_ds2_lr3 --run-dir $R --episodes 0,6 \
  --declaration decl.json $P --no-vlm --json | jq '.modules.eef_video_consistency.episodes'
jq -c '[.details.merged.episode.label, ([.details.merged.cells[] | select(.flags | index("single_source")) | .missing] | unique)]' \
  $R/checks/eef_video_consistency/results.jsonl
```

预检：`available`，两路 `fixed_external`、都能画，`applicable_params` 里有 `observation_seeds` / `gripper_template`、没有停用的 `record_mapping`，
`notes` 末尾一条 `vlm_backend_missing: …`（加 `--vlm-backend ark` 或 `--param eef_video_consistency.use_vlm=false` 时没有）。`check --no-vlm` 不发任何模型请求，
两条照常出结论，单渠道的格写 `no_vlm_backend`；换成 `--param eef_video_consistency.use_vlm=false`（带不带 `--vlm-*` 都一样）写 `vlm_off`。
`check --modules task_success … --no-vlm` 是用法错误（退出码 2）。DAS（`$D/umi_das`）的预检两路都是 `wrist`、归 `robot0` / `robot1`，`applicable_params` 里没有夹爪参考。
把声明里一路相机的 `mount` 写成 `fixed_external` 并加假设 `{"code": "mount_declared_fixed"}`（控制台声明抽屉里「会动」的相机点「视为固定」就是这样），
生成的记录 `details.trajectory_source.declared_fixed` 列出它，报告 EEF 小节的 `declared_fixed_cameras` 也列出它。

### 两半：CPU 半段与模型半段（设计 23 §2.1、设计 25 F5.24b）

接上一节，先起假模型（`tests/cli/fakevlm_server.py`，或仓库根 `.claude/launch.json` 的 `curator-fakevlm`，端口 8766），在 `backend/` 下：

```bash
V="--vlm-endpoint http://127.0.0.1:8766/v1 --vlm-model fake-vlm"
R=$(mktemp -d)
../.venv/bin/python -m curation.cli check --modules eef_video_consistency --input $D/eef_ds2_lr3 --run-dir $R --episodes 0,6 \
  --declaration decl.json $P $V --prep --json | jq -c '.modules.eef_video_consistency.episodes'
ls $R/scratch/vlm/000000/eef_video_consistency | head; wc -l < $R/checks/eef_video_consistency/results.jsonl
../.venv/bin/python -m curation.cli check --modules eef_video_consistency --input $D/eef_ds2_lr3 --run-dir $R --episodes 0,6 \
  --declaration decl.json $P $V --prepared --json | jq -c '.modules.eef_video_consistency.episodes'
jq -c '[.episode_index, .details.merged.episode.label, .details.halves]' $R/checks/eef_video_consistency/results.jsonl
ls $R/scratch/vlm 2>/dev/null || echo "packages gone"
```

`--prep` 打出 `{"total":2,"ok":2,"error":0}`，假模型的日志里没有请求；`scratch/vlm/000000/eef_video_consistency/` 下有 `prep.json`、
`requests.json` 和每个复核窗口的图与视频（`r000_i00.jpg`、`r000_v00.mp4` …），`results.jsonl` 还是空的（0 行）。`--prepared` 才发请求，写出两行结果，
`details.halves` 是 `{"vlm_prep": …, "vlm": …}`（两半各用了多少秒），结论与一次 `check` 相同；之后请求包没了。
同样的命令加 `--param eef_video_consistency.use_vlm=false`（或把 `$V` 换成 `--no-vlm`）只跑 `--prep`：直接写结果行，`halves` 只有 `vlm_prep`。
`../.venv/bin/python -m curation.cli plan --preflight <预检 JSON> --modules eef_video_consistency,task_success` 的阶段里有
`vlm_prep`（CPU 块）和 `after: vlm_prep` 的 `vlm`；加 `--param eef_video_consistency.use_vlm=false` 后 EEF 只在 `vlm_prep` 里。

### 原有 EEF 数据

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
8. 在 v2 命令行链路上跑（D49，在 `backend/` 下；需要一个 VLM 后端，下面用方舟预设 `ark`，环境变量 `ARK_API_KEY` 里放密钥）：

   ```bash
   G=~/ws/ws_general/galbot/dataset2; R=/tmp/eef_cli; mkdir -p $R
   P="--param eef_video_consistency.trajectory_json=$G/trajectory.json"
   ../.venv/bin/python -m curation.cli preflight --json --input $G/eef_ds2_lr3 --vlm-backend ark --modules timestamp_check,eef_video_consistency $P > $R/preflight.json
   ../.venv/bin/python -m curation.cli plan --preflight $R/preflight.json --modules timestamp_check,eef_video_consistency --episodes 0-6 --out $R/plan.json
   ../.venv/bin/python -m curation.cli check --modules timestamp_check --input $G/eef_ds2_lr3 --run-dir $R --episodes 0-6 --survivors-out $R/numeric.txt
   ../.venv/bin/python -m curation.cli check --modules eef_video_consistency --input $G/eef_ds2_lr3 --run-dir $R --episodes @$R/numeric.txt $P --vlm-backend ark
   ../.venv/bin/python -m curation.cli aggregate --run-dir $R --phase funnel --revision 1 --episodes 0-6
   ../.venv/bin/python -m curation.cli aggregate --run-dir $R --phase final --revision 1 --episodes 0-6 --input $G/eef_ds2_lr3
   ../.venv/bin/python -m curation.cli report --run-dir $R --revision 1
   ```

   `preflight.json` 里 `eef_video_consistency` 是 `available`，带 `subitems` 与 `episode_counts: {available: 7}`（不给 `--param` 时是
   `needs_input: trajectory_missing`——平台算不出这个数据集的轨迹，要传文件；给了文件但不给 `--vlm-backend` 时仍是 `available`，`notes` 末尾一条
   `vlm_backend_missing: …` 提醒，注册表 5.2 起模块不再必须有模型）；`plan.json` 的 `vlm`
   阶段是 `eef_video_consistency`；EEF 的 `check` 打印判完的条数与按细码的发现数（注册表 5.0：`inconsistent` 只报告、`conflict` 请人看），
   每条记录的 `details.merged` 写着每个分项 × 相机两边的结论与 p、合并后的标签与标记，`episode` 是整条的标签、p 与一句依据，`details.review` 是每个复核窗口
   （问的点与轴、模型答复）；`revisions/r0001/verdicts.jsonl` 里没有一条因为它是 `drop`，有冲突的条在 `review` 里列着 `("conflict", "eef_check")`；
   `report.md` 的「EEF–视频一致性」一节写「结论(意见,不判废):不一致 … 可能不一致 … 一致 … 判断不了 …」、冲突条数、只有一个渠道的原因与判断不了的原因；
   `tables/` 下有 `eef_camera_metrics`、`eef_segments`、`eef_diagnosis`、`eef_review_windows`、`eef_opinions` 等表。没有 VLM 后端时，第 10 步的测试用假模型走同一条路。
9. 控制台上传与 Daemon 执行（F5.5 / F5.9）：先跑 `../.venv/bin/python -m pytest -q tests/orchestr/test_eef_tasks.py -m "slow or not slow"`
   （约 45 秒，含一个真跑 CLI 的端到端任务），应全部通过。再真起 Daemon（仓库根的 `.claude/launch.json` 里的
   `curator-daemon-eef`：开发用主密钥、不鉴权、本地数据根是 `~/ws/ws_general/galbot/dataset2`），在 `backend/` 下：

   ```bash
   G=~/ws/ws_general/galbot/dataset2; U='http://localhost:8080/curation/api/v1/uploads'
   curl -s -X POST "$U?kind=eef_trajectory&name=trajectory.json" -H 'Content-Type: application/json' \
     --data-binary @$G/trajectory.json | python3 -m json.tool | head -30
   sed 's/"status": "valid"/"status": "valid", "truth": 1/' $G/trajectory.json > /tmp/bad.json
   curl -s -X POST "$U?kind=eef_trajectory&name=bad.json" -H 'Content-Type: application/json' --data-binary @/tmp/bad.json \
     | python3 -m json.tool | head -20
   cat $G/observations_seed/*/*.jsonl > /tmp/seeds.jsonl
   ```

   第一次上传返回 201，`handle` 是 `upload:upl-…`（9 位小写字母，D45），`validation.summary` 里 `samples: 7`、`frames: 2009`、
   `max_reprojection_difference_px` 约 0.013；第二次返回 400 `validation_failed`，`details.errors` 每条带
   `field`（JSON 路径）、`sample_id`、`frame_index`、`camera_id`、`point_id`，`code: forbidden_key`。
   然后在浏览器打开 <http://localhost:8080/curation/tasks/new>：数据来源选本地路径 `eef_ds2_lr3`，「快速质检」不会勾上
   「EEF–视频一致性」（第一屏的卡片上不再提示要上传的文件，2026-09-24 第四轮），手动勾上后模型配置出现（它要用模型复核）；第二屏的
   trajectory.json 选 `$G/trajectory.json`（按钮先写「上传中…」、文件发完写「校验中…」，通过后显示文件名、sha256 前 12 位与摘要），
   「夹爪参考」下拉默认是「观测种子」，选 `/tmp/seeds.jsonl`（`.jsonl` 由控制台转成 JSON 数组），另有复核窗口数、每窗口帧数两项；
   先选 `/tmp/bad.json` 能看到逐条定位的错误。
   观测种子与夹爪外观模板合成「夹爪参考」一项（注册表 1.10 的 `x-choice-group`），自 1.12 起可以都不给（D-E15）：
   这时预检照常 `available`，notes 里写 `vlm_opinion`，模块不做 CPU 测量、只请模型看整段视频给意见（见下面第 16 步），不参与判决。两个文件可以同时上传，
   后传完的不会把先传完的冲掉（2026-09-24 在 galbot 上遇到过：trajectory.json 大、后传完，种子的句柄丢了）。
   创建并开始后：任务的运行目录有 `inputs/uploads.json` 与两份文件副本，`plan.json` 的 `vlm` 阶段有这个模块，报告里有
   「EEF–视频一致性」一节；它不判废任何条目（注册表 5.0），冲突的条进人工裁决。直接在 `modules[].params` 里填服务器路径会被 400 拒收（Daemon 只认 `upload:` 句柄）。
10. 输出口径与复核（F5.22，设计 25 §6–§7；复核 F5.10）：`../.venv/bin/python -m pytest -q tests/eef/test_combine.py tests/eef/test_review.py tests/cli/test_eef_review.py`
    （约 30 秒），应全部通过——第一个逐条覆盖渠道换算与合并（取大、冲突、单渠道封顶、跟错目标作废 CPU、意见与自运动）；第二个检查每个请求只画
    一点一轴、prompt 只定义画出来的东西；第三个在迷你数据集上用脚本化的答复把 CPU 正常被模型否定（冲突，唯一出卡）、候选段被模型确认（不一致，
    先经过一次格式修复）、可疑但模型没给意见（超时、引用不存在的帧、解释里写了「约 2 cm」：只有 CPU，封顶 0.8）、模型不反对（一致）、文件里没有
    （判断不了）都走一遍，录进 tape 后在新的运行目录离线回放，记录逐项相同，`aggregate` 谁也不判废、只把冲突那一条放进 `eef_check`。模型对照真值的准确率用
    `PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.review_eval --dataset dataset2 --stand-in`（假模型，只查流程）
    或带 `--endpoint/--model/--api-key-env` 的真实后端跑（见 `tools/eef_eval/README.md`）。
    冲突的条每个复核窗口都有标记图（窗口的 `evidence`，裁决卡用），别的条只有模型反驳或冲突的窗口有图。
11. 冲突进裁决（F5.11，C1 1.9 的复核种类 `eef_check`；注册表 5.0 起只有冲突出卡，设计 25 §7.3）：
    `../.venv/bin/python -m pytest -q tests/cli/test_eef_adjudication.py tests/results/test_eef_queue.py`（约 15 秒）与
    `../.venv/bin/python -m pytest -q tests/orchestr/test_eef_tasks.py -k settles -m "slow or not slow"`（约 30 秒），应全部通过：
    冲突的条留在 passed、进 review 并计入待裁；判「一致」保留，判「不一致」进拒绝（理由「人工裁决判为 EEF 与视频不一致」，人判的、不可复议），
    「拿不准」照旧待裁；执行裁决后出新结果版本、交付标为过期，重新导出后交付里没有判废的那条。注册表 5.0 以前开跑的任务（`run.json` 的
    `registry_version` 是 4.x）照旧口径：模块自己的判废照样判废、可复议（`test_eef_adjudication.py` 的旧世界与 `test_eef_queue.py` 的 4.4 任务）。
    界面上：第 9 步的任务跑完后打开「人工裁决」，冲突的条是一张卡片，写着「来源：EEF–视频一致性 · EEF 与画面核对」、先是逐格表（两边各自的结论与 p、
    合并后的标签、冲突的格底色标出），再是 CPU 分项读数（可疑的格子标橙）、模型逐窗口答复（位置 / 朝向 / 绿十字跟对了三张票、偏移、
    模型原话、与 CPU 冲突的窗口带橙框）和每个窗口的标记图，按钮是「一致，判过」「不一致，判废」「拿不准」；视频下面不再重复这些图。
    报告页（F5.12，5.0 起）：「EEF–视频一致性」一节的「冲突」下面写着人工裁决里待裁几条；「Episode 明细」里这个模块的一块第一行是
    「标签 · 置信度 · 冲突 / 只有一个渠道」与一句依据，下面是逐格表，再往下与裁决卡片同一组内容（CPU 分项、逐窗口答复与标记图、CPU 证据帧），
    页面上方的证据帧不再重复这些图；`report.md` 的这一节有一行「人工裁决:待裁 N 条…」。测试：`../.venv/bin/python -m pytest -q tests/cli/test_eef_check.py`（报告一节）与前端
    `npx vitest run src/pages/report`（「the EEF block」一条）。
12. 从 LeRobot 列生成 trajectory.json（设计 §3.3）：`../.venv/bin/python -m pytest -q tests/eef/test_mapping.py`（约 15 秒），应全部通过；再手动：

    ```bash
    ../.venv/bin/python -m curation.extensions.eef_consistency export --mapping tests/eef/mappings/dataset2.yaml \
      --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 --out /tmp/ds2_mapped.json
    ../.venv/bin/python -m curation.extensions.eef_consistency validate --trajectory /tmp/ds2_mapped.json \
      --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 --all-points-observable | head -20
    ```

    第一条打印 `samples: 7`、`frames: 2009`；第二条 `report.valid: true`、七条 episode 都 `available`（投影由平台按位姿与标定重算，
    包里 `projection` 为 `null`）。把映射里的 `layout` 删掉或写成 `auto`，`export` 以退出码 2 报「eef.layout」——映射必须写明，不猜列宽。
13. 留出集验收（F5.7，在仓库根执行，约 2 分钟）：`PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.acceptance --dataset dataset3 --jobs 4`，
    需要仓库外的 `galbot/dataset3`（构建方法见 `tools/eef_eval/README.md`）。末尾打印的汇总应为 `detected: 19`、`false_alarm_episodes: 0`；
    分组报告写在 `tools/eef_eval/reports/acceptance.md`。

14. 夹爪外观模板（F5.8，锚点的第二种来源，与种子二选一）。先从 dataset2 第 0 条的种子建模板（约 20 秒），再检查重检测器，
    再用模板而不是种子跑三条 episode：

    ```bash
    G=~/ws/ws_general/galbot/dataset2
    ../.venv/bin/python -m curation.extensions.eef_consistency template-build --trajectory $G/trajectory.json \
      --lerobot-root $G/eef_ds2_lr3 --seeds $G/observations_seed --episodes 0 --every 15 --dataset dataset2 \
      --tool-name robotiq_2f85 --out $G/gripper_template.json
    ../.venv/bin/python -m curation.extensions.eef_consistency template-check --template $G/gripper_template.json \
      --trajectory $G/trajectory.json --lerobot-root $G/eef_ds2_lr3 --seeds $G/observations_seed --episodes 0 6 --every 15
    ../.venv/bin/python -m curation.extensions.eef_consistency run --trajectory $G/trajectory.json \
      --lerobot-root $G/eef_ds2_lr3 --template $G/gripper_template.json --episodes 0 5 6 --out /tmp/eef_run_template
    ```

    `template-build` 打印 `entries: 27`、`masked_entries: 27`、`skipped_static: 13`（开头静止的一秒多没有条目）、两路相机、
    `methods: ["synthetic_fixture"]`（种子是真值反推的，模板也只供 DEMO）。`template-check` 每路一行：ep0 两路 `detected` 约 10–14 / 20，
    对种子的 `reference_p95_px` ≤ 1；ep6 同样（视频与 ep0 相同，账本不同不影响重检测）。`run --template` 的结论与第 6 步用种子时一致：
    ep0 全 `ok`，ep5 两路 `temporal_alignment: suspect` 且 `time_offset` 被支持，ep6 只有 `27432424_left` 位置 suspect 且 `extrinsics_error`
    被支持；`details.jsonl` 里 `template_sha256` 有值、`seeds_sha256` 为 null，`cameras.<相机>.observation.seed_method` 为 `gripper_template`，
    `redetections` 形如 `10/147`（每 15 帧一次成功，失败的帧逐帧重试）。命令行链路上把模板当参数传（`--param eef_video_consistency.gripper_template=FILE`），
    没有种子目录时预检仍是 `available`，notes 里写着模板的 hash 与条目数；控制台第二屏的「夹爪参考」下拉换成「夹爪外观模板」（`.json`），
    同一个按钮上传，换选项会丢掉已传的另一种（二选一）；上传即校验（`POST /uploads?kind=eef_gripper_template`，返回条目数、可用条目数、
    相机、掩膜条目数与提示，控制台显示「N 个模板条目 · 相机 …」）。
    单测：`../.venv/bin/python -m pytest -q tests/eef/test_template.py`（合成场景 7 条 + DEMO 数据 1 条，约 15 秒）。
15. mcap 数据集（F5.13）：`../.venv/bin/python -m pytest -q tests/cli/test_eef_mcap.py`（约 1 分钟），应全部通过：对账工具的迷你
    mcap 数据集与迷你 LeRobot 数据集是同一批画面，把 trajectory.json 的相机改成 `"uri": "episode_<N>.mcap", "topic":
    "/observation.images.exterior", "clip_start_s": 0, "clip_end_s": null`，逐帧画面与 LeRobot 视频同号同图，预检 `available`，
    `check` 的分项读数与结论和 LeRobot 版逐项相同；假 TOS 上与本地读数逐条相同，`.mcap` 只按区间读、不整文件下载（`streams.objects`）；Lance 仍是
    `unsupported`。手动：`cd .. && PYTHONPATH=tools .venv/bin/python -m parity make-fixture --format mcap --out /tmp/mini_mcap`
    做一份 mcap 数据集，把测试里的 trajectory.json 照上面改写后，
    `../.venv/bin/python -m curation.cli preflight --input /tmp/mini_mcap --modules eef_video_consistency --vlm-backend ark --param eef_video_consistency.trajectory_json=<文件>`
    应是 `available`。写 trajectory.json 时 `frame_count` 是该 topic 的帧数，`video_frame_index` 是逐帧对应的第几条图像消息。
    已有 LeRobot 版 trajectory.json、数据又有逐帧一一对应的 mcap 版时，用 `tools/eef_convert.py` 转，不用手改：
    `.venv/bin/python tools/eef_convert.py to-mcap --trajectory 旧.json --out 新.json --check <mcap 数据集目录>`（在仓库根执行；
    相机的 topic 默认是 `/observation.images.<LeRobot 视频键>`，别的写法用 `--map 相机=/topic`；`--check` 按平台的编号规则取文件名，
    并核对每路 topic 存在、帧数等于 `frame_count`、画面尺寸等于 `image_size_wh`，有不符就逐条列出、退出码 1）；反方向是
    `to-lerobot --dataset <LeRobot 数据集目录>`（读 `meta/` 算出视频文件与片段起止）。种子和夹爪模板不用转。
    测试：`../.venv/bin/python -m pytest -q tests/cli/test_eef_convert.py`（迷你数据集来回转、核对报错、dataset2 的 v3 片段来回不变）。

16. 没有夹爪参考时的模型意见（D-E15，设计 12 §10.5）。本机没有模型密钥时先看要发出去的标注视频：

    ```bash
    ../.venv/bin/python - <<'PY'
    import base64, os
    from curation.extensions.eef_consistency import load, opinion as OP
    root = os.path.expanduser("~/ws/ws_general/galbot/dataset2/eef_ds2_lr3")
    r = load.load_bundle(os.path.expanduser("~/ws/ws_general/galbot/dataset2/trajectory.json"), lerobot_root=root)
    s = r.samples[2]
    for cid in sorted(s.cameras):
        frames = [i for i in range(s.n_frames) if s.cameras[cid].video_frame_index[i] >= 0]
        req = OP.build_request(s, cid, frames, OP.pick_point(s, cid), OP.pick_axis(s, cid, frames),
                               media_root=root, model="x", options={"max_side": 720})
        open(f"/tmp/ep2_{cid}.mp4", "wb").write(base64.b64decode(req.videos[0].url.split(",", 1)[1]))
        print(cid, req.videos[0].frames, "frames")
    PY
    ```

    应看到两路相机各 287 帧；视频左上角印着帧号，夹爪附近有红圈 P、红箭头 A 与穿过两指的橙线 B，以及到当前 P 结束的过去 1 秒青色轨迹
    （ep2 注入的是绕接近方向转 30°：
    P、A 与 ep0 一样，只有 B 的方向不同；脚本里 `build_request` 要带上 `finger_id=OP.pick_finger_axis(s, cid, frames, "z")` 才画 B）。
    暂停检查：历史线随动作逐步出现，不提前显示后续轨迹；不足 1 秒时只显示已有历史。历史点统一投影到当前相机，
    相机移动时不应当直接连接过去帧的像素。证据 JPEG 应与同帧视频的标记一致。只有二维投影、没有时间或三维末端与声明 P 冲突时省略历史线。
    便携回归：`../.venv/bin/python -m pytest -q tests/eef/test_opinion.py tests/eef/test_umi.py`（没有 dataset2 时只跳过原有两项真实数据测试）。
    两类提示词共用「过去到当前」说明，保留各自标记定义；普通 EEF 的 `aspect=action` 表示轨迹运动时序与可见动作不符。
    有模型时在控制台新建任务：勾 EEF，第二屏只传 `dataset2/trajectory.json`、夹爪参考留空；跑完后报告的 EEF 小节写「模型意见 7」、
    有不匹配片段的条数与置信度分布，不列判过 / 判废 / 转人工；Episode 明细里每条列出片段（帧、秒、中心 / 朝向、不匹配置信度、
    模型的话、标注证据帧）。对照 `dataset2/meta/corruptions.json`：ep0 原版应当干净，ep1–6 的片段应当落在注入的故障上。
    命令行等价：`check --modules eef_video_consistency --param eef_video_consistency.trajectory_json=<不带种子目录的副本>`
    （trajectory.json 旁边有 `observations_seed/` 或 `gripper_template.json` 时会被当作夹爪参考自动用上，要验证意见模式就复制到别处）。
17. 轨迹与数据集记录（F5.15，设计 12 §8.7，D-E16；只报告，不参与判决，不要夹爪参考、不调模型）。
    单测：`../.venv/bin/python -m pytest -q tests/cli/test_eef_record.py`（约 1 分钟），应全部通过。手动：先写映射——列名、布局、
    单位、坐标系都由人写明，不猜：

    ```bash
    cat > /tmp/ds2_record.json <<'JSON'
    {"schema_version": "eef-mapping/1.1", "record": {
      "pose": {"key": "observation.state.cartesian_position", "layout": "xyz_rpy_xyz_extrinsic",
               "units": {"position": "m", "angle": "rad"}, "frame_id": "panda_link8", "reference_frame": "robot_base"},
      "joints": {"key": "observation.state.joint_position", "units": "rad", "robot": "franka_panda",
                 "reference_frame": "robot_base"}}}
    JSON
    G=~/ws/ws_general/galbot/dataset2
    ../.venv/bin/python -m curation.cli preflight --json --input $G/eef_ds2_lr3 --vlm-backend ark --modules eef_video_consistency \
      --param eef_video_consistency.trajectory_json=$G/trajectory.json --param eef_video_consistency.record_mapping=/tmp/ds2_record.json
    ```

    预检的 `subitems.record_consistency` 是 `available`（不给映射是 `unsupported: record_mapping_missing`；列名写错是
    `record_columns_missing`，notes 里写着哪一列不在数据集里）。在第 8 步的 EEF `check` 后面加同一个
    `--param eef_video_consistency.record_mapping=/tmp/ds2_record.json` 再跑：每条的 `details.record.sources` 有 `pose` 与 `joints`；
    位姿列 7 条全 `ok`（trajectory.json 就是从这几列导出的）；关节角正解 ep1 `suspect`（`constant_mismatch` 与 `record_deviation`，
    按声明关系的残差 P95 约 38 mm），ep2 `constant_mismatch`（与声明相差转 30°），ep3 `record_deviation`，其余 `ok`；
    `details.record.internal.consistent` 在 ep1–3 为 false；每条的判过 / 判废 / 转人工与不给映射时完全一样。
    `checks/eef_video_consistency/evidence/000001/record/` 下每路相机 3 张叠加图（帧 84、148、266，也列在 `details.record.evidence`）：
    红圈是上传的轨迹，橙圈是关节角正解，都带前后 15 帧的拖尾。`report.md` 的 EEF 一节多一行「轨迹与数据集记录(只报告,不参与判决)…」，
    `tables/` 多一张 `eef_record`。mcap 数据集把 `key` 换成 `topic`（加 `fields`，如 `"topic": "/arm/joint_states", "fields": "position"`）。
    不想手写（F5.16，D-E17）：LeRobot 数据集的预检已经起草好一份，
    `../.venv/bin/python -m curation.cli preflight --json --input $G/eef_ds2_lr3 --modules eef_video_consistency`
    输出里 EEF 条目的 `drafts.record_mapping`：`document` 的位姿是 `observation.state.cartesian_position`（`frame_id: null`，参考点未声明）、
    关节角是 `observation.state.joint_position`（`franka_panda`），基座都是 `"@upload"`；`assumptions` 逐条列出推断的地方（选实测不选指令、
    外旋 xyz、单位 m / rad、参考点未声明、同一基座、型号对应、正解到法兰）。把 `document` 存成文件当映射传，比对结论与上面相同：关节角
    ep1 / ep2 / ep3 照旧，位姿列 7 条一致、注明恒定差未检查（数据集内部互比也只到拟合常量为止，ep2 的 30° 在这里看不出）。mcap 数据集
    给 `format_not_drafted`。
    控制台：第二屏「数据集记录映射」上方是「从数据集元数据起草」一块（来源、推断的地方），「确认使用」即把草稿作为上传件提交，
    「修改」给 JSON 编辑框（比如把位姿的 `frame_id` 填成 `panda_link8`，这一路的恒定差也会判），「撤销」清掉；也可以照旧上传上面的 JSON，
    到货即校验，显示「比对来源 关节角、位姿列 · 机器人 franka_panda · 列 …」；
    跑完后「Episode 明细」和裁决卡片的 EEF 区块末尾是「轨迹与数据集记录」：每个来源一块（读哪一列、怎么对齐、按声明 / 扣掉恒定差后
    两组残差、声明 / 拟合 / 相差的恒定差、时间差、随时间变化的差、位置与姿态两张残差曲线），然后是数据集内部一致与否和叠加图
    （不再出现在「CPU 证据帧」里）；报告的 EEF 小节多「与数据集记录比过」「与记录不一致」等数、一张原因图和一张按来源的状态表。

## 回退

回退就是新建任务时不勾选这个模块（预设与「全选可用」本来就不勾）：不勾时计划里没有它，判决配置与接入前完全相同，旧流程逐字节不变。
勾了以后它参与判决（D49）：判废的条目进拒绝清单，转人工的出裁决卡片（`eef_check`，人判后按判过 / 判废重算）；阈值仍是未校准的 demo。
