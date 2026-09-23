# 12 EEF–视频一致性（DEMO 模块）

> 状态：**v1.2 草案**（2026-09-22 晚）。§0.4 的 D-E1–D-E10 已由需求方确认；本版按需求方的产品口径改为「不动 LeRobot、模块配置上传 trajectory.json」（§0.5、§3.7）。评审通过后落到 daft 仓库 `docs/design/12-eef-video-consistency.md`，格式与 Schema 落到 `docs/contracts/eef/`。
> 工程基线：`070eed9e092240684991e97b78c4bc3870dce67b`（feat/curator-v2）。
> 依据：需求原文 → 2026-09-22 的三层方案 → 参考设计（格式 [12-eef/format.md](12-eef/format.md)、迁移 [12-eef/migration.md](12-eef/migration.md)，其工程稿与计划稿在 `~/ws/ws_general/galbot/eef_video_consistency/03-*.md`、`04-*.md`，已被本篇吸收）→ 验证数据 `~/ws/ws_general/galbot/dataset1`（v2.1，11 条）与 `dataset2`（v3.0，7 条）→ 客户参考 `~/ws/ws_general/galbot/reference/{droid,LVP}`。
> 与参考设计的关系：**格式、独立观测、真值隔离、advisory 先行、工程限制清单全部采纳**；本篇在其上补齐产品化路径（统一入口、预检、注册表、CLI/Daemon/前端接入、诊断归因、验收矩阵、分期与停止条件），并把两处口径改掉（§0.3）。

## 给实施 agent 的开工指引（先读这一节）

1. **分支与纪律**：直接在 `feat/curator-v2` 上做，不开 worktree。A 类目录（`core/`、`registry/`、`ingest/` 等，见 `CLAUDE.md`）一行不改，新代码放 `backend/curation/extensions/eef_consistency/`。每个 feature 一个 commit，提交前跑 `.venv/bin/python -m pytest -q backend/curation/tests --ignore=backend/curation/tests/test_environment.py`、`.venv/bin/python -m pytest -q backend/tests/contracts`、`PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests`（合成数据逐位对账进 CI，旧判决不能变）；提交后推 origin；提交信息英文，不加模型 co-author。每个阶段完成点更新根目录 `feature_list.md`（F5.x）与 `claude-progress.txt`。
2. **读什么，按顺序**：本篇全文（§0.4 决策已拍板、§0.5 产品形态、§0.6 开工前六点、§3.7 单文件包、§11 接入清单、§13 验收矩阵、§14 DEMO 第一刀）→ [12-eef/format.md](12-eef/format.md)（字段、单位、坐标变换、时间）→ [12-eef/schemas/](12-eef/schemas)（sample / frame / calibration / observation / trajectory_bundle 五份 Schema）→ [12-eef/migration.md](12-eef/migration.md)（dataset1、dataset2、客户参考的适配规则）→ `05-modules-and-preflight.md` §7（加一个模块要改哪些地方）→ `02-cli-contract.md` §3.5（`check`）、`06-delivery-and-report.md` §6（报告小节）。
3. **数据在哪（本机）**：`~/ws/ws_general/galbot/dataset2/`：`eef_ds2_lr3/` 是 LeRobot v3.0 数据集（Curator 已能读）、`trajectory.json` 是上传件（七条、两路相机，已校验）、`observations_seed/` 是 P-A 跟踪的种子（`synthetic_fixture`，只供 DEMO）、`corruptions.json` 是真值（只给评估器，检测器与 VLM 输入禁止读）、`preview/` 是叠加与曲线示例；`~/ws/ws_general/galbot/dataset1/`：`eef_ds1_lr2/` 是 v2.1 数据集、`trajectory.json`（单相机、十种轻重两档故障）、`ground_truth/` 真值。工具（导出、校验、叠加、曲线、三维图、种子）在 `~/ws/ws_general/galbot/tools/`，venv 在 `galbot/.venv`；几何与绘制可直接借用 `tools/common.py`。
4. **DEMO 第一刀（F5.1–F5.4，都是 CPU）**：① `trajectory.json` 读取、Schema 与语义校验、逐分项能力预检、几何投影与自洽、L0 数值轨迹 → ② ObservationProvider 协议、P-A（种子 + 多尺度 LK + 前后向校验 + 周期重定位）、观测文件、投影扰动的独立性实验 → ③ 位置 / 方向 / 局部 lag / 数值 / 画面五项指标、迟滞分段、诊断假设、曲线与证据，在 dataset1 + dataset2 上跑出 §13.3 矩阵 → ④ 注册表 1.4（两个模块、`eef_input`、`input_scope`、`affects_dataset_verdict`）、预检、`check` 分派、parts 输出、aggregate 隔离、报告默认表格；`trajectory.json` 先走 CLI 参数 `--param eef_video_consistency.trajectory_json=<path>`。阈值用明确标 `uncalibrated` 的 `demo` profile。
5. **第一刀不做**：不改 LeRobot；不改 `pipeline/verdict.py`；不接 VLM（只留 tape 钩子）；不做上传控件与 Daemon 上传接口（第二刀 F5.5）；不训练模型；不给客户像素精度承诺。
6. **需要向需求方要的**：第二刀的 VLM 后端；真实数据前客户对 §3.4 清单的答复。第一刀不需要额外提供。

## 0. 摘要

### 0.1 一句话

给 Curator v2 加一个**建议性（advisory）**模块「EEF–视频一致性」：客户按统一格式交来视频、末端执行器（EEF）位姿或投影、相机标定；平台把声明的投影与**从画面独立取得**的同一物理点、同一物理方向逐帧比较，输出位置、方向、时间错位、数值抖动、画面共同运动、可评估性六个分项的状态、曲线、证据和诊断假设；VLM 只做抽帧复核与解释，不做测量；首版不参与 keep / drop / held 判决。

### 0.2 分两步走

| 步 | 目标 | 完成标志 |
|---|---|---|
| 第一步 统一数据格式 | 客户交来的 trajectory 的物理意义无歧义：哪个点、哪个坐标系、什么单位、什么时间轴、相机怎么标定、投影落在哪张图的哪个像素空间 | 一份 `trajectory.json`（§3.7 单文件包）通过 Schema 与语义校验；dataset2 七条已转出并校验通过；dataset1 与客户两类参考随 P1 收录；「轨迹物理意义确认清单」（§3.4）客户逐项签认 |
| 第二步 产品上线 | 模块进注册表、预检、任务、报告、前端；用户在模块配置里上传 `trajectory.json`；advisory 分支对全部选中样本运行，旧判决与黄金基线逐位不变 | §11 回归门全过；P6 试用报告 |

### 0.3 本篇对前序方案的两处修正

1. **门的口径**：我 22 日的口头方案说「hard，但只在证据一致时杀」。参考设计的理由成立：在没有独立视觉标注证明测量精度之前，任何自动判废都是把未校准的阈值当承诺。**v1 定为 advisory**（`gate=none`，不影响数据集判决）；升级为硬门的条件写死在 §9.4，需要独立数据上的验收。
2. **归因的地位**：我把「残差形态 → 六种故障签名」当结论层；参考设计指出整段拟合会抹掉固定偏移。折中：**主指标永远是未补偿的原始残差**，归因降级为「诊断假设 + 支持证据」，单独一栏输出（§8.6），供人看，不进状态判定。

### 0.4 需要评审拍板的决策

| 编号 | 决策 | 默认 |
|---|---|---|
| D-E1 | 检测输入唯一契约是 `eef-video/1.0.0`；LeRobot 原生数据经适配器 + 映射配置生成样本，不往 LeRobot 里加字段 | 是 |
| D-E2 | 精确数值检查只对「有独立可见对应」的物理点做；记录原点、模型 TCP、可见特征三者分开报告，带 assurance 等级 | 是 |
| D-E3 | v1 `assessment_mode=advisory`，`gate=none`，`affects_dataset_verdict=false`；对全部选中样本运行（含被旧硬门拒绝的） | 是 |
| D-E4 | 逐相机独立测量，永不跨相机平均；episode 级只汇总分项状态 | 是 |
| D-E5 | 独立观测 provider 不得看到待检投影；评估真值只进离线评估器 | 是 |
| D-E6 | 两个内部模块：`eef_video_consistency`（frame 档）与 `eef_video_review`（vlm 档）；注册表加 `input_scope`、`affects_dataset_verdict` 两个字段（C1 → 1.4） | 是 |
| D-E7 | 视觉 provider 阶梯：人工初始化 + CPU 跟踪（P2 基准）→ 自动初始化 + 跟踪 + 周期重定位（v1 生产）→ 客户夹爪关键点模型（v2） | 是 |
| D-E8 | lag 符号：`u_visual(t) ≈ u_declared(t + lag)`，lag > 0 表示记录流落后于画面 | 是 |
| D-E9 | 腕部相机 v1 只做静态空间分项（位置、方向），不做时序与轨迹分项 | 是 |
| D-E10 | 客户 World_Policy 样本：位姿可视化格不作相机；`eef_delta_gt` 按「相对首帧」解释但标 `unresolved` 直到客户签认 §3.4 清单 | 是 |

### 0.5 产品形态（需求方 2026-09-22 晚定）

- 本模块当前是 **DEMO 状态**。
- **LeRobot 数据集一个字段都不改**，平台把它当普通数据集读；EEF 相关的一切信息都在用户另外上传的一份 `trajectory.json` 里。
- 上传点在**质检模块的详细配置**（新建任务第二屏，按 `param_schema` 生成的表单）：勾选「EEF–视频一致性」后出现文件项，要求上传约定格式的 `trajectory.json`；文件按 §3.7 校验，校验结果直接变成该模块的预检能力。
- 文件与数据集的绑定靠 `episode_index`；文件里没有的 episode 该模块报 `unsupported: projection_missing`，不影响其它模块。

### 0.6 开工前六点（需求方 2026-09-22 晚确认）

1. 第一刀先走 CLI 路径参数；上传控件与 Daemon 上传接口放第二刀。
2. 视觉观测用 P-A：随数据集附种子文件；dataset2 的种子由原版几何生成并标 `synthetic_fixture`，只用于 DEMO，不做精度验收；客户数据由人点。
3. 允许明确标 `uncalibrated` 的 `demo` 阈值 profile，让分项能显示 ok / suspect。
4. 第一刀不含 VLM 复核，只留 tape 钩子。
5. 直接在 feat/curator-v2 上按 feature 提交并推送，同步 `feature_list.md` 与 `claude-progress.txt`，每次提交前过 v1 单测、契约测试与 parity。
6. dataset1 也转成 `trajectory.json` 作为第二套 DEMO 数据。

## 1. 目标、范围与非目标

### 1.1 需求还原

客户原文：「校验 EEF 轨迹的准确性，把轨迹投影到视频上，人工或 VLM 检查是否偏移、朝向是否正确、中心点是否对准夹爪」；「客户已有外参，能自己算出投影点，需要的是投影后与视频中实际可见点的一致性校验，以及轨迹本身的合理性判断（偏移、抖动、朝向）」。

拆成两件独立的事：

| 事 | 输入 | 本质 | 本篇章节 |
|---|---|---|---|
| A 轨迹自身合理性 | 位姿流 | 纯数值：尖峰、不连续、高频能量、开合合理性 | §8.4 |
| B 投影与画面一致性 | 位姿或投影 + 标定 + 视频 | 同一物理点 / 方向在图像平面上的比较 | §6–§8 |

### 1.2 客户数据的两种来历（假设，待确认）

`reference/droid/*/sample.json` 带 `redis_ref`、`model_payload_meta.encoder_class=wan`、`latent_shape`，是一个视频生成 / 世界模型训练缓冲区的样本：2×2 网格视频（右外部、左外部、腕部、位姿可视化）、文本、65 步相对 EEF 序列作为条件。由此推断本模块有两种用法，流水线相同：

1. **真实数据质检**：DROID 类真实视频 + 记录的 EEF 条件序列，查条件与画面是否一致；
2. **生成视频质检**：模型按 EEF 条件生成的视频，查生成的夹爪运动是否跟随条件。

区别只在 `media.origin`（§3.6 建议新增字段）与报告措辞；生成视频的「相机标定」取条件视频所用相机的标定。**这条假设要在 §15 向客户确认。**

### 1.3 范围

- v1：离线 episode；固定外部相机为主，腕部相机做静态空间分项；一只目标夹爪；多机位逐路分析；仅二维投影输入也能跑空间与局部时序分项。
- v1 输出：六个分项的状态、曲线、证据、覆盖率、诊断假设；VLM 复核意见；不判废。
- 非目标：训练通用夹爪模型；人手 / 第一人称数据（LVP 类）的 EEF 检查（`unsupported`）；移动相机的完整诊断；从视频反推轨迹（SLAM）。

## 2. 术语与物理意义

| 术语 | 定义 | 本设计的处理 |
|---|---|---|
| 记录原点 `eef_origin` | 机器人本体记录的 EEF 坐标系原点，DROID 是 `panda_link8` 法兰 | 图像上通常没有可见表面特征，**不直接参与精确像素比较** |
| 模型 TCP `tcp` | 由工具模型从原点外推的指尖中心，dataset2 用 `+0.16 m` 沿 z | `assurance=model_assumed`；能与可见指尖对应时参与比较，误差报告必须带来源等级 |
| 可见特征 | 指尖、壳体角点、两指开合线等真能在画面里指出来的点 / 线 | 独立观测只标注它们；P0 建立「可见特征 ↔ 工具模型」对应 |
| 投影 `projection` | 声明的几何按声明的标定算出的像素坐标；客户提供或平台重算 | 待检对象，永不覆盖 |
| 观测 `observation` | 从画面独立取得的像素坐标 | 分文件保存，provider 看不到投影 |
| 真值 `evaluation` | 合成样本的注入参数与原始几何 | 只有离线评估器读 |
| 相机类型 `mount` | `fixed_external / wrist / moving` | 腕部相机的投影是固定像素，只有静态空间分项有意义 |
| 主时间线 | 视频 PTS（`timebase=video_pts`）或仅次序（`index_only`） | `index_only` 不输出秒、Hz、m/s |
| lag | `u_visual(t) ≈ u_declared(t + lag)`，正值 = 记录流落后 | dataset2 ep5 为 +0.333 s，dataset1 ep9/10 为 −0.200 / −0.533 s |

## 3. 第一步：统一数据格式

### 3.1 采用 `eef-video/1.0.0`

格式本体见 [12-eef/format.md](12-eef/format.md) 与 [12-eef/schemas](12-eef/schemas)，本篇不重复；采纳理由：它把「哪个点、哪个坐标系、什么单位、什么时间、哪个像素空间」全部显式化，缺失填 `null` 而不是零，投影 / 观测 / 真值三分离，且已有六组样例与校验器通过。

```text
<dataset>/
├── <lerobot 数据集目录>/             LeRobot 数据集本身，只放 meta/ data/ videos/（dataset2 里当前叫 eef_ds2_lr3/）
├── eef_consistency/
│   ├── samples/<sample_id>/
│   │   ├── sample.json              入口：视图与媒体、点与轴定义、坐标系、时间基
│   │   ├── frames.jsonl             每帧：时间、源时间、eef、gripper、各相机投影
│   │   ├── calibration.json         已解析的有效声明标定（可空）
│   │   └── raw_pose_sequence.json   未解释的源序列（可选）
│   └── mapping.yaml                 LeRobot → 样本 的映射配置（§3.3）
├── observations/                    运行产物：独立观测（不回写输入）
└── evaluation/                      仅评估器可见：真值与注入参数
```

### 3.2 两种可检查形态、两种保留形态

| 形态 | 必须有 | 可空 | 能做的分项 |
|---|---|---|---|
| A 二维投影输入 | 视频、逐帧投影点、点与轴的物理意义 | eef、标定 | 位置、可观测方向、局部时序、画面共同运动 |
| B 三维几何输入 | 视频、绝对位姿（或可恢复绝对位姿）、标定 | 投影（平台重算） | A 的全部 + 数值轨迹 + 投影自洽 |
| C 未知语义序列 | 图片或视频可缺失，原序列存旁路 | eef=null | 只做结构检查；不做绝对投影 |
| D 纯图像 | 图片 | 全空 | EEF 一致性 `unsupported` |

能力由预检从真实输入推导（§5），不接受 `can_check=true` 之类的自报。A、B 同时提供时，重算投影与提供投影的差作为「输入自洽」项报告，不覆盖客户投影。

### 3.3 辅助工具：从 LeRobot 列生成文件的 `mapping.yaml`（不是平台入口）

平台入口只有 §3.7 的 `trajectory.json`。为了让客户不必手写逐帧记录，配套一个离线导出工具：按映射配置从 LeRobot 列生成 `trajectory.json`；配置必须显式，**禁止靠维数猜哪几列是 EEF**。这是给客户和我们自己用的便利工具，平台运行时不读 LeRobot 之外的映射：

```yaml
schema_version: eef-mapping/1.0
eef:
  pose_key: observation.state.cartesian_position     # 或 observation.state 的切片
  layout: xyz_rpy_xyz_extrinsic                      # 枚举：xyz_rpy_xyz_extrinsic | xyz_quat_xyzw | xyz_quat_wxyz | xyz_rotmat
  frame_id: panda_link8
  reference_frame: robot_base
  units: {position: m, angle: rad}
  pose_type: absolute
gripper:
  key: observation.state.gripper_position
  closed_fraction: identity                          # identity | one_minus | {min:..,max:..}
  tool_model: {max_opening_m: 0.085, finger_axis: local_y, tcp_offset_m: [0, 0, 0.16], assurance: model_assumed}
cameras:
  exterior_1_left:
    video_key: observation.images.exterior_1_left
    camera_id: 27432424_left
    mount: fixed_external
    calibration: {intrinsics_fx_cx_fy_cy: [...], distortion: {model: pinhole, image_space: rectified, coefficients: []},
                  extrinsics: {mode: static, cam2base_xyz_rpy_key: camera_extrinsics.exterior_1_left}}
    media_transform: identity                        # 或 3×3 H：标定图像 → 媒体像素
timing:
  video_timebase: lerobot_timestamp                   # 主时间线 = frame_index / fps
  source_clocks:
    - {channel: robot_state, key: timestamp_robot_ms, unit: ms, clock_id: droid_recorded_ms, semantics: robot read_start}
    - {channel: exterior_1_left, key: timestamp_cam_exterior_1_left_ms, unit: ms, clock_id: droid_recorded_ms, semantics: estimated camera capture}
```

dataset1（v2.1，`observation.state=[x,y,z,rpy,gripper]`）与 dataset2（v3.0，`observation.state.cartesian_position` + 逐帧 `camera_extrinsics.*` + 旁路 `calibration.json`）各写一份 `mapping.yaml`，作为适配器的两条回归用例。v3 拼接视频必须按 episodes 表的 `from_timestamp` 定位，不能从 0 秒读。

### 3.4 轨迹物理意义确认清单（客户签认）

格式统一的完成标志不是 Schema 通过，而是下面每一项都有客户的明确回答并写进样本：

| # | 问题 | 写进哪里 | 客户 World_Policy 样本当前状态 |
|---|---|---|---|
| 1 | EEF 坐标系是法兰、工具中心还是指尖中心？相对哪个参考系？ | `eef_frame`、`reference_frame`、`point_definitions` | 未知（DROID 默认 `panda_link8` ← `robot_base`，待签认） |
| 2 | 位置单位、旋转表示与顺序（xyzw/wxyz、欧拉序） | `position_m`、`quaternion_xyzw`；适配器换算 | 量级像米（65 步最大 9 cm），四元数像 xyzw；未签认 |
| 3 | 绝对位姿还是相对位姿？相对首帧（`inverse(T0)×Tt`）还是相邻增量？位置差是世界系平移差还是变换？ | `pose_type`、`relative_to.convention`、`anchor_pose` | 首步为单位变换 → 像相对首帧；`anchor_pose` 缺失，无法恢复绝对投影 |
| 4 | 起点绝对位姿（anchor）从哪来？ | `relative_to.anchor_pose` | 缺失；可由 `name` 里的 DROID episode + step 回查原始 h5 |
| 5 | 夹爪标量的定义与方向（0 开 1 合？） | `gripper.closed_fraction`、`opening_m` | `gripper_sequence` 恒 0.4846，语义未知 |
| 6 | 视频每一路对应哪台相机、哪个镜头（左目 / 右目）、什么安装方式 | `views[].camera_id`、`mount` | 文本说明「右外部 / 左外部 / 腕部」，无序列号 |
| 7 | 内参、畸变模型、外参方向（相机→基座还是基座→相机）、标定分辨率 | `calibration.json` | 「客户已有外参」但未交付；DROID 可用 KarlP 精修值 |
| 8 | 媒体像素空间与标定像素空间的关系（缩放、裁剪、网格拼接） | `H_media_from_calibration`、`decoder_crop_regions` | 1280×720 → 416×240 格子，`decode_meta` 给了 quad 变换与 8 px 夹爪条 |
| 9 | 时间轴：视频帧率、每步对应哪一帧、各时钟的语义 | `timebase`、`source_timing` | 65 步 ↔ 65 帧的对应未声明；只交付了首帧图 |
| 10 | 容差：偏移多少毫米、朝向多少度算问题 | 阈值 profile（§12） | 未知 |

第 4 项有捷径：`Droid_episode_046899_step_000093` 可回溯 DROID 原始 episode，anchor 位姿、逐步位姿、精修标定都能从原始数据取到，客户只需确认「就是这一条的这一段」。

### 3.5 客户参考样本的适配规则

沿用 [12-eef/migration.md §3–§4](12-eef/migration.md)：网格视频拆成三路相机视图加一格 `pose_visualization`（不作相机，不作观测源）；无序列号时用 `source_view_0/1/2` 局部稳定 ID；`eef_delta_gt` 原样进 `raw_pose_sequence.json`，`eef=null`，`timebase=index_only`，直到 §3.4 签认后再转成 `relative_to_start` + `anchor_pose`；LVP 收录为纯图像，`unsupported`。

补充两条产品规则：

- 生产入口必须拿到**逐帧 RGB 媒体**，`redis_ref` 指向的 `.pt` 是模型潜变量，不是可检输入；网格视频要按 `decoder_crop_regions` 拆格并写 `H`。
- 若客户能给 DROID episode 标识，适配器优先从原始数据回填绝对位姿与标定，把样本从形态 C 升级到形态 B。

### 3.6 对格式 1.0 的增补建议（1.1，向后兼容）

| 增补 | 位置 | 理由 |
|---|---|---|
| `media.origin`，取值 recorded、generated、unknown | `sample.views[].media` | 区分真实数据与生成视频质检（§1.2） |
| `sample.tool_model`（可选）：`max_opening_m`、`finger_axis`、`tcp_offset_eef_m`、`assurance` | `sample.json` | 让 `linear_gripper` 点定义有出处，避免每个点各写一遍 |
| `frames.jsonl.cameras[].projection.points[].source_point_id`（可选） | 投影点 | 客户自算投影时标明它算的是哪个物理点 |
| 样本级 `expected_tolerances`（可选）：`position_mm`、`orientation_deg` | `sample.json` | 客户容差进样本，阈值 profile 可引用 |

以上都是可选字段，不改 1.0 语义；Schema 升 `eef-video/1.1.0`。

### 3.7 平台入口：`trajectory.json` 单文件包

约定格式的三个文件（sample / frames / calibration）在产品上打成**一个文件**上传，容器 Schema 见 [12-eef/schemas/trajectory_bundle.schema.json](12-eef/schemas/trajectory_bundle.schema.json)：

```json
{"schema_version": "eef-video/1.0.0", "container": "trajectory-bundle/1.0",
 "dataset": {"id": "galbot/dataset2", "lerobot_codebase_version": "v3.0", "fps": 15, "episode_count": 7},
 "media_uri_base": "lerobot_root",
 "samples": [{"episode_index": 0,
              "sample": {"...": "sample.json 原样，annotations_path 固定为 \"#frames\"，calibration_path 为 \"#calibration\" 或 null"},
              "calibration": {"...": "calibration.json 原样，或 null"},
              "frames": [{"...": "frames.jsonl 的每一行"}]}]}
```

规则：

| 项 | 约定 |
|---|---|
| 绑定 | `samples[].episode_index` 对应 LeRobot 的 `episode_index`；`sample.source.episode_id` 必须等于它的字符串 |
| 媒体 | `views[].media.uri` 相对 LeRobot 数据集根（含 `meta/` 的目录），v3 拼接视频用 `clip_start_s / clip_end_s` 定位；平台按任务的数据集根解析，不允许绝对路径与外部 URL（DEMO） |
| 校验 | 上传即校验：容器 Schema → 每个样本用参考校验器（四份 Schema、跨文件语义、媒体存在、重算投影与提供投影之差 < 0.05 px）；失败返回样本 / 帧 / 字段级错误 |
| 真值隔离 | 文件里出现 `truth / ground_truth / corruption_type / fault` 任一键即拒绝（校验器 `FORBIDDEN`） |
| 覆盖 | 文件可只含数据集的一部分 episode；缺的 episode 该模块 `unsupported: projection_missing` |
| 体量 | DEMO 只收 `.json`；dataset2 七条两路相机约 3 MB。后续 `.jsonl.gz` 或分片再议 |
| 版本 | 文件 hash 进 `input_hash`；换文件重跑视为新输入，旧结果按 revision 保留 |

产出与工具（本机 `~/ws/ws_general/galbot/`）：`tools/export_trajectory_json.py`（原生标注 → `trajectory.json`），`tools/validate_trajectory_json.py`（校验 + 可拆回三文件目录）。dataset2 的七条已产出并校验通过（`dataset2/reports_trajectory_validation.json`：2009 帧、28126 个投影点、最大重投影差 0.013 px）。

## 4. 总体架构

### 4.1 数据流

```text
统一样本 / LeRobot+mapping ──► 适配器 ──► 语义与能力预检（§5）
                                              │
              ┌───────────────────────────────┼─────────────────────────────┐
              ▼                               ▼                             ▼
     声明位姿 / 投影 / 时间映射        原视频流式解码               标定与像素变换
              │                               │
              ├──► L0 数值轨迹（§8.4）         ├──► L1a 独立观测 provider（§7）
              │                               ├──► L1c 背景共同运动（§8.5）
              ▼                               ▼
     L1b 逐相机残差：位置 / 方向 / 局部 lag（§8.1–8.3）
              │
              ▼
     分段与分项状态（§9） ──► 诊断假设（§8.6） ──► 证据与曲线（§10.4）
              │
              ▼
     L2 VLM 复核（§10，可选）──► 报告 / 表 / 抽屉（§11）
```

三层的职责边界：L0 只看位姿流；L1 只做几何与信号处理，纯函数、不读网络；L2 只回答分类问题，不产生像素测量。

### 4.2 与现有模块的关系

| 模块 | 借用什么 | 不借什么 |
|---|---|---|
| `video_action_sync` | 逐相机的全局 lag 作为候选先验（若已运行且输入 hash 一致） | 不作硬依赖；本模块自己的局部 lag 更精，将来可反哺 |
| `motion_quality` | spike / smoothness / gripper_jitter 的实现思路 | 它看的是 action/state 关节流，不替代笛卡尔 EEF 流的抖动检测 |
| `visual_quality` | 清晰度、曝光、冻结作为可评估性线索 | 不区分相机运动与夹爪运动 |

辅助结果必须带输入 hash、时间范围、相机 ID 才能被引用；对不上就忽略并记原因。

### 4.3 代码位置

新增 `backend/curation/extensions/eef_consistency/`（B 类新代码，不碰 A 类目录）：

```text
contracts.py     输入 / 输出 dataclass、枚举、reason code
load.py          sample/frames/calibration 读取、Schema 与语义校验
adapters/        lerobot_mapping.py · unified_sample.py · world_policy.py
geometry.py      SE(3)、工具点、畸变模型、投影链、H 变换
timeline.py      PTS 与状态配对、插值、有效区间、跨时钟规则
observations.py  ObservationProvider 协议、观测文件读写
tracking.py      自动初始化、LK 跟踪、前后向校验、周期重定位
metrics.py       位置、方向、局部 lag
motion.py        L0 数值轨迹、背景共同运动
segments.py      迟滞阈值、分段、证据帧选取
diagnosis.py     诊断假设（PnP 修正、lag、恒定朝向、轴向偏移、漂移、抖动来源）
review.py        VLM 请求包、输出 Schema、预算、缓存键
report.py        parts 行、曲线、表、证据清单
runner.py        流式执行与编排（唯一做 I/O、解码、模型调用的地方）
```

几何、指标、诊断均为纯函数并可用合成场景做交叉验证（§13.1）。

## 5. 预检与可评估性

### 5.1 静态预检：逐分项能力

在平台三态（`available / needs_input / unsupported`，05 篇 §4）之上，本模块的预检返回逐分项能力表：

```json
{"module": "eef_video_consistency", "availability": "available",
 "subitems": {
   "position_2d":        {"availability": "available",   "reason_code": null},
   "orientation_2d":     {"availability": "available",   "reason_code": null},
   "temporal_alignment": {"availability": "available",   "reason_code": null},
   "state_motion":       {"availability": "unsupported", "reason_code": "pose_semantics_unknown"},
   "camera_motion":      {"availability": "available",   "reason_code": null},
   "vlm_review":         {"availability": "needs_input", "reason_code": "vlm_backend_missing"}},
 "cameras": {"27432424_left": {"mount": "fixed_external", "calibration": "declared", "media": "ok"}}}
```

顶层规则：任一分项 `available` 则模块 `available`；全部 `unsupported` 才置灰；`needs_input` 只用于用户能补的项（VLM 后端、mapping 缺字段、anchor 缺失）。

reason code 目录：`projection_missing`、`pose_semantics_unknown`、`anchor_pose_missing`、`calibration_missing`、`calibration_direction_ambiguous`、`video_missing`、`media_transform_unknown`、`clock_alignment_unknown`、`visual_point_mapping_missing`、`axis_mapping_missing`、`moving_camera_unsupported`、`wrist_camera_spatial_only`、`decode_failed`、`vlm_backend_missing`。执行失败与输入缺失分开编码。

### 5.2 动态可评估性

逐帧标记：遮挡、模糊、出画、跟踪不稳、方向投影长度退化、静止导致 lag 不可辨识。输出四个分母分开的覆盖：请求帧数、有媒体映射帧数、观测可见帧数、有效比较帧数；`coverage = 有效比较 / 请求`，另报「可见帧内定位成功率」。投影出画但夹爪在画面内是异常线索，不允许通过剔除这些帧抬高通过率。

## 6. 几何与时间

### 6.1 投影链

```text
p_reference = T_reference_eef · p_eef
p_camera    = inverse(T_reference_camera) · p_reference
uv_calib    = project(K, distortion_model, p_camera)          # pinhole | opencv_brown(5) | opencv_fisheye(4)
uv_media    = normalize(H_media_from_calibration · [u, v, 1])
```

约定：右手系、列向量、主动旋转，`T_A_B` 把 B 中坐标变到 A；四元数存 xyzw 单位长度，`q` 与 `−q` 同旋转；工具点按 `point_definitions` 的 `fixed / linear_gripper` 模型算，`linear_gripper` 用当帧 `closed_fraction` 插值。去畸变是非线性，不能伪装成 H；缩放裁剪只能进 H，不能同时改 K 又套 H。

自洽检查：形态 B 且客户给了投影时，重算并存 `declared_vs_recomputed`（逐点像素差、最大 / 中位），差异超过源量化精度报 `input_inconsistent`，两份都保留。

### 6.2 时间

- 主时间线取视频实际 PTS 映射到片段局部时间；状态按真实采集时间配对，只能保证导出时间一致时标低置信。
- 机器人位置线性插值，旋转最短弧 SLERP，夹爪零阶保持；最大插值间隔默认 2× 局部中位采样间隔，超过按缺口分段。
- 跨时钟（`clock_id` 不同）未确认映射时不算精确 lag，只报 `clock_alignment_unknown`。
- 重采样重复帧（dataset1 的 17 帧）允许重复 `source_state_index`，主时间必须严格递增；抖动分析要考虑这种共同背景。

## 7. 独立视觉观测

### 7.1 provider 协议与独立性

```python
class ObservationProvider(Protocol):
    def locate(self, frames: FrameStream, targets: PointTargets, ctx: Context) -> ObservationBatch: ...
```

传入对象**不含**投影坐标、baseline、注入参数、原始 raw_extras；观测文件写 `projection_visible_to_localizer=false` 与输入图像 hash。严格独立路径不使用投影 ROI；若实验用投影辅助初始化，单独标记 `seeded_by_projection=true`，并做「投影平移 30 px、观测应不动」的扰动实验，且不计入验收。

### 7.2 provider 阶梯（D-E7）

| 阶 | 方法 | 用途 | 通过条件 |
|---|---|---|---|
| P-A 人工初始化 + CPU 跟踪 | 人在若干帧点出指尖 / 壳体角点，多尺度 LK 传播，前后向误差、patch 一致性、形状约束，每窗口重定位 | P2 测量实验、建立噪声底 | 留出验收帧上 P95 定位误差 < 最小可检异常像素幅度的 1/3 |
| P-B 自动初始化 + 跟踪 + 周期重定位 | 开放词汇检测 / 分割给出夹爪区域与指尖候选，LK 跟踪，遮挡后重新确认；跟踪质量差即弃权 | v1 生产 | 在 P-A 的验收集上达到同等误差，且错目标率、遮挡弃权率可接受 |
| P-C 客户夹爪关键点模型 | 用合格数据的投影做伪标签自举训练，人工验收集校准 | v2 生产 | 同上，且跨 episode 分组验收 |
| P-X VLM 定位 | 实验 provider | 仅实验 | 同一套独立标注验证后才能进数值主链 |

跟踪不能把被抓物体或桌面纹理当夹爪：多点几何关联 + 周期性重定位；失跟后禁止用插值冒充观测。

### 7.3 不确定性与标注协议（P0）

- 候选可见点：指尖 ×2、壳体稳定角点、两指开合线（无向）；逐项确认与 `panda_link8` 原点 / 模型 TCP 的对应关系。
- 标注说明：点定义、遮挡、部分可见、工具对称性、有向 / 无向；标注者看不到投影。
- 首批 50–100 个时间位置 × 两路相机，覆盖运动、旋转、接触、遮挡、边缘；20% 由第二人复标，得到人的重复误差。
- 划分「初始化 / 开发帧」与「独立验收帧」，同源变体与邻近帧进同一组，防泄漏。
- 告警阈值必须高于可重复定位噪声；达不到就报「测量精度不足」，不调阈值掩盖。

## 8. 指标与诊断

### 8.1 位置

同一时间、相机、物理点：`e_t = ‖u_declared(t) − u_visual(t)‖₂`，同时报 u/v 残差、中位数、P95、异常持续时间；归一化 `e_norm = e_px / s_t`，`s_t` 取观测侧稳定尺寸（夹爪壳体尺度），不取投影预测的尺度；两指合拢时距离退化，尺度不可靠只报像素。有 TCP 深度时另报近似毫米：`e_mm ≈ e_px · Z / f`，并标 `model_assumed`。

### 8.2 方向

投影方向 a 与观测方向 b 的二维单位向量：有向轴 `acos(clamp(a·b))` 范围 0–180°，无向线 `acos(clamp(|a·b|))` 范围 0–90°；投影长度低于阈值（轴朝向镜头）标 `not_observable`；两指身份未识别时只做无向线；多机位分别保留。

### 8.3 局部时间错位

全局粗搜（互相关）给候选，滑动窗口给局部 lag，输出相关峰、次峰差、有效运动能量、重叠比例、置信区间；静止、周期、多峰、窗口太短时弃权。主报告保留未补偿残差，补偿后残差只作诊断，且前后用同一 mask。符号按 D-E8。

### 8.4 EEF 数值轨迹（L0）

只分析实际 state：按真实 Δt 求线速度、角速度、加速度与稳健尖峰；旋转增量用 `log(R_tᵀ R_{t+1}) / Δt`；高频能量占比在足够长的近似均匀窗口上算，频段低于 Nyquist（15 Hz 数据不辨识 ≥ 7.5 Hz）；四元数符号翻转不告警；接触、快速旋转、控制停顿要上下文，不靠 jerk 高就判坏。只有 action 没有 state 时该项缺失，不改成查指令。dataset2 ep3（关节不变、笛卡尔流抖）是回归用例。

### 8.5 背景与相机运动

排除机器人 / 夹爪 / 被操作物体区域，对静态背景跟踪特征，稳健估计平移 / 仿射并报拟合质量；背景支持不足报 `unknown`。三种组合作为**支持证据**：背景与夹爪共同移动 → 画面整体运动；背景稳而夹爪局部振荡 → 末端局部运动；state 抖而画面不抖 → 记录不一致。

### 8.6 诊断假设（不进状态判定）

每个假设给出：支持证据、拟合量、拟合后残差改善、置信等级。仅在对应分项已为 `suspect` 时计算，输出到 `diagnosis[]`：

| 假设 | 计算 | 支持条件 | 对应回归样本 |
|---|---|---|---|
| 外参错误 | 用 (TCP 三维, 观测二维) 对做 PnP 重拟合，得 ΔT | ΔT 小（cm / 度级）且拟合后残差降到噪声底；同一相机跨 episode 一致 → 升数据集级 | dataset2 ep6 |
| 时间偏移 | §8.3 的 lag | 补偿后残差显著下降，且与 `video_action_sync` 不矛盾 | dataset2 ep5，dataset1 ep9/10 |
| 恒定朝向错 | 方向残差的中位数与离散度 | 方向残差恒定、位置残差不随之变化 | dataset2 ep2（绕 z），dataset1 ep3/4（绕 y） |
| TCP 轴向偏移 | 残差方向与投影接近轴方向的相关 | 残差沿接近轴、幅度随深度变化 | 法兰 vs 指尖定义错（DROID 之坑） |
| 位姿漂移 / 运动学误差 | 低频残差，排除以上三者后剩余 | 与关节位形相关时倾向运动学 | dataset2 ep1，dataset1 ep1/2 |
| 抖动来源 | 残差高频能量 + §8.5 组合 | 见 8.5 三种组合 | dataset2 ep3/ep4，dataset1 ep5–8 |

固定残差向量、多机位共同偏差只能支持假设，不能单凭形态断言根因；PnP 拟合值不回写标定。

## 9. 分项状态、分段与汇总

### 9.1 分项状态

| status | 含义 |
|---|---|
| `ok` | 输入、覆盖、测量精度、已校准阈值均满足，未见异常 |
| `suspect` | 可靠证据支持异常，仍为建议性结果 |
| `unknown` | 有输入，但遮挡 / 退化 / 阈值未校准 / 覆盖不足，不足以判断 |
| `unsupported` | 缺该项输入或首版不支持的场景 |
| `error` | 解码、模型、执行异常 |

阈值 profile 为空时只出曲线，不出 `ok`。

### 9.2 分段

开始 / 结束双阈值（迟滞）、最小持续时长、允许短间断；先标 invalid 帧再在连续有效区间统计；保留原始帧 mask，合并区间保留多个 reason code。

### 9.3 汇总

逐相机分项状态 → episode 级只做「任一相机 suspect 则展示候选；部分可评估；全部不可评估」三种汇总，不做平均分，不输出伪精确总分。

### 9.4 升级为硬门的条件（v2）

同时满足才允许把某一分项改为 `gate=hard`：独立 episode 分组验收上误报率与漏报率达到与客户签认的容差匹配；测量噪声 P95 低于最小可检异常的 1/3；连续两批真实数据的 advisory 结果经人工抽检确认；升级只针对该分项，并沿用同步模块「所有可信相机一致指向同一缺陷才杀」的纪律。

## 10. VLM 复核

### 10.1 采样与请求包

两类窗口：均匀抽查（每 episode × camera 最多 3 个）与 CPU 候选 / 跟踪不稳窗口（最多 3 个），每窗口最多 6 帧；预算触顶记 `review_truncated`。请求包含原帧、带帧号的局部裁剪、观测点与投影用不同颜色的叠加图、相邻帧短序列、点定义与可观测限制；不含故障名、真值、注入幅度、预设结论；首次定位类问题只给未叠加原图。

### 10.2 输出协议

```json
{"review_status": "support | refute | uncertain | not_observable",
 "target_visible": true,
 "tracking_target_correct": "support | refute | uncertain",
 "position_support": "support | refute | uncertain | not_observable",
 "orientation_support": "support | refute | uncertain | not_observable",
 "background_motion_support": "support | refute | uncertain",
 "offset_direction": "none | up | down | left | right | toward_fingers | away_from_fingers | unclear",
 "offset_magnitude_class": "none | within_finger_width | one_to_two_finger_widths | over_two_finger_widths | unclear",
 "evidence_frame_ids": [12, 15, 18],
 "reason_codes": ["occlusion"],
 "explanation": "..."}
```

严格 Schema；引用的帧号必须来自本请求；超范围坐标、虚构帧、不可解析一律拒绝，最多一次格式修复重试。VLM 不产生厘米误差或三维旋转数值。CPU 与 VLM 冲突时双方证据保留、转人工复核；VLM 指出跟错目标则候选标「待核实」并触发一次独立重定位。

### 10.3 运行控制

复用现有 VLM client 的超时、并发、费用、失败记录与录制回放带（C2 `parity/vlm-tape-entry`）；调用种类新增 `eef_review`（按 C3 1.1 用模块 id 形式命名），实验定位另记 `eef_localize`。缓存键：视频 hash、时间窗口、点定义 hash、prompt/schema/model 版本、图像预处理配置。回归用固定 tape，不拿在线随机输出当验收。

### 10.4 证据

每个候选段：最差 3 帧的原图与叠加图、残差随时间曲线、lag 扫描曲线、背景运动曲线、VLM 解释。证据模式沿用 `flagged | all | off`。

## 11. 第二步：接入 Curator v2

### 11.1 注册表（C1 → 1.4）

| id | 中文名 | level | gate | needs | stage | depends_on | 新字段 |
|---|---|---|---|---|---|---|---|
| `eef_video_consistency` | EEF–视频一致性 | episode | none | `video`, `eef_input` | frame | — | `input_scope=all_selected`，`affects_dataset_verdict=false` |
| `eef_video_review` | EEF–视频一致性 · VLM 复核 | episode | none | `video`, `vlm` | vlm | `eef_video_consistency` | 同上 |

- `eef_input` 是新的能力需求，由专门的探测函数判定：任务配置里有通过校验的 `trajectory.json`。NEEDS 的 AND 列表表达不了「投影 OR 位姿+标定」，两种形态都在文件内部区分。
- `input_scope=all_selected`：planner 不按漏斗筛选，对任务选中的全部样本运行，包括已被旧硬门拒绝的。
- `affects_dataset_verdict=false`：`aggregate` 在调用边界过滤掉它，`verdict.py` 不改；`records` 里 `passed=None, score=None`，分项进 `detail`，界面不把它译成弃权。
- 前端展示为一个模块「EEF–视频一致性」加可选「VLM 复核」开关；选复核自动带上基础模块。
- 参数（`param_schema`，按 D38 每项带 title / description / default）：**`trajectory_json`（文件，必填）**、`observation_provider`、`threshold_profile`、`lag_search_s`、`interpolation_gap_factor`、`allowed_camera_mounts`、`evidence_mode`、`review_enabled`、`review_windows_per_camera`、`review_frames_per_window`。
- 文件型参数是新的表单控件：`param_schema` 用 `{"type": "string", "format": "upload", "x-accept": [".json"], "x-max-mb": 64}` 声明；Daemon 新增上传接口把文件存到任务的 inputs 目录并返回句柄与 hash；CLI 侧对应 `--param eef_video_consistency.trajectory_json=<path>`。DEMO 第一刀先做 CLI 路径参数，上传控件在 P4 补。

### 11.2 改动清单（对照 05 篇 §7 与参考设计 §10）

| 位置 | 改动 | A/B 类 |
|---|---|---|
| `contracts/modules.py`、`docs/contracts/modules.json` | 两个 ModuleSpec、`eef_input` need、两个新字段；契约修订记录 | B |
| `cli/preflight.py` + C2 `preflight.schema.json` | 逐分项能力表；不要求旧模块先跑 | B |
| `cli/runctx.py`、`cli/check.py` | 扩展模块参数与旧 `KNOWN_CHECKS` 分流；显式分派新 runner | B |
| `pipeline/check_stage.py`（或新 stage runner） | frame 档新增 EEF CPU runner，vlm 档新增复核 runner，不借 `task_success` 的分支 | B |
| `planner/plan.py`、`planner/executor.py` | `input_scope=all_selected` 分支、CPU→review 依赖、VLM 预算合并 | B |
| `pipeline/aggregate.py` 调用边界 | 过滤 `affects_dataset_verdict=false` 的模块，旧 verdict 函数原样 | B（不碰 verdict.py） |
| `pipeline/records.py` 适配 | parts 行沿用 result-record 契约，分项在 detail，执行失败单独记录 | B |
| `export/`、`backend/daemon/results/tables.py` | TableSpec：`eef_camera_metrics`、`eef_segments`、`eef_diagnosis` | B |
| `backend/daemon/orchestr/delivery.py` | 证据 / 曲线 / 观测目录进交付过滤 | B |
| `frontend/src/pages/report/ModuleSection.tsx`、`features/report/EpisodeDrawer.tsx` | 先用默认表格渲染器上线；二期做时间轴 + 相机切换 + 原图/叠加/观测切换 | 前端 |
| `backend/daemon/routes/`（上传） + `docs/contracts/openapi.yaml` | 任务输入文件上传接口（POST，返回句柄与 hash）；曲线 / 证据接口按需 | B + 契约 |

不改 `core/`、`registry/`、`ingest/` 等 A 类目录；旧算法原样调用。

### 11.3 输出布局

```text
checks/eef_video_consistency/parts/0001.jsonl     每 episode 一行（result-record），detail 含分项、覆盖、诊断
checks/eef_video_consistency/curves/<ep>/<cam>.parquet   逐帧残差、方向差、lag、mask
checks/eef_video_consistency/observations/<ep>/<cam>.jsonl   独立观测（observation.schema）
checks/eef_video_review/parts/0001.jsonl          复核记录
evidence/eef/<ep>/<cam>/…                         原图、裁剪、叠加、短片、manifest
revisions/rXXXX/tables/eef_camera_metrics.parquet · eef_segments.parquet · eef_diagnosis.parquet
```

`parts` 行的 `detail` 最少包含：`schema_version`、`module_version`、`config_hash`、`input_hash`、`capabilities`、`cameras[cam].subitems[...]{status, metrics, coverage, reasons}`、`segments[]`、`diagnosis[]`、`review{status, truncated}`、`evidence[]`、`timing{decode_s, track_s, metrics_s, vlm_calls}`。

### 11.4 报告小节

| 摘要指标 | 图 / 专用视图 | 明细 |
|---|---|---|
| 各分项 suspect / unknown / unsupported 条数；可评估覆盖率分布 | 时间轴卡：残差与 lag 曲线、候选段高亮、相机切换、原图 / 叠加 / 观测切换（二期） | 逐相机分项表、候选段表、诊断表；点击进逐条抽屉（03 篇 §6） |

缺失显示「无法评估」，不显示 0 像素；物理点与 assurance 等级随数值一起显示。

### 11.5 资源与并发

新 runner 首版独立流式解码（不复用 `make_frame_checks` 的帧）；每 worker 只保留短窗口、关键点与指标；跟踪可降分辨率但保存精确缩放映射，像素统一换回 media 尺寸。CPU 并发按 episode × camera 限制，VLM 并发走全局预算；记录解码、跟踪、指标、VLM 各段耗时与峰值内存，性能目标在第一批完整运行后据实定。

## 12. 参数与阈值

```yaml
eef_video_consistency:
  assessment_mode: advisory
  observation_provider: auto_seeded_tracker        # human_seeded_tracker | auto_seeded_tracker | keypoint_model
  allowed_camera_mounts: [fixed_external, wrist]   # wrist 只做空间分项
  threshold_profile: null                          # 未校准 → 只出曲线与 unknown
  lag_search_s: [-1.0, 1.0]
  interpolation_gap_factor: 2.0
  evidence_mode: flagged
  observation_cache: true
eef_video_review:
  enabled: false
  uniform_windows_per_camera: 3
  candidate_windows_per_camera: 3
  max_frames_per_window: 6
  max_format_retries: 1
```

阈值 profile 是独立文件：按物理点 / 方向、观测方法、相机分辨率、相机类型给阈值，并记录来源实验与版本；禁止按故障标签选阈值。**实验起点**（标 uncalibrated，仅供 P3 起步）：位置中位数 > 20 mm 等效或 > 2% 画宽且持续 > 30% 可见帧；方向中位数 > 10°；lag ≥ 2 帧且极小值清晰；残差高频标准差 > 5 mm；PnP ΔT > 10 mm 或 1.5°。正式值来自 §13.3 的扫描。

## 13. 测试与验收

### 13.1 结构与几何

相机变换方向、xyzw/wxyz、q/−q、米/毫米、正确与未知相对位姿、三种畸变模型、缩放裁剪 H、点在相机后、轴端点出画、静态 / 逐帧外参、v3 非零片段起点、非单调与缺口时间、仅二维投影、纯图像。几何单测用可解析场景并与独立实现（OpenCV `projectPoints`）交叉验证。

### 13.2 独立视觉测量

留出验收帧上报：定位误差、方向误差、错目标率、可见帧覆盖、遮挡弃权率、跟踪持续长度；投影扰动实验证明观测独立。P95 定位误差 < 最小可检异常像素幅度的 1/3。

### 13.3 受控异常矩阵

两套同源合成数据合计 18 条，只用于功能回归，不作泛化准确率：

| 数据 | 注入 | 位置 | 方向 | lag | 数值轨迹 | 画面运动 | 应出的诊断 |
|---|---|---|---|---|---|---|---|
| d1 ep0 / d2 ep0 | 基准 | ok+覆盖 | ok | ok | ok | ok | — |
| d1 ep1/2、d2 ep1 | 低频漂移 2.5 / 7 / 4 cm（d2 另 8°） | suspect，重档 > 轻档 | d2 轻微 | ok | ok（低频） | ok | 位姿漂移 |
| d1 ep3/4（绕 y 15°/40°）、d2 ep2（绕 z 30°） | 恒定朝向错 | 绕 y 时 TCP 会动，允许 | suspect 恒定 | ok | ok | ok | 恒定朝向错 |
| d1 ep5/6、d2 ep3 | 高频位姿抖动 | 高频残差 | — | ok | suspect | ok | 抖动来源 = 记录 |
| d1 ep7/8（整帧平移 3/10 px）、d2 ep4（±6 px ±0.5°，仅 ext1） | 画面抖动 | 高频残差 | — | ok | ok | suspect（d2 仅 ext1） | 抖动来源 = 画面 |
| d1 ep9/10（视频滞后 3/8 帧）、d2 ep5（记录滞后 5 帧） | 时间错位 | 随速度的残差 | — | −0.200 / −0.533 s；+0.333 s | ok | ok | 时间偏移，符号正确 |
| d2 ep6 | ext1 声明外参错 3 cm / 2° | ext1 恒定偏移，ext2 ok | — | ok | ok | ok | 外参错误（ext1），PnP 修正 ≈ 注入量 |
| 客户 droid / LVP | 缺字段 | 精确弃权 / unsupported，无伪造通过 | | | | | — |

评估器读 `evaluation/` 的真值算命中；d1 的 `boundary_padding=true` 帧与仅 3–16 s 注入的区间由评估器 mask，检测器不得读答案。补充扫描：用 dataset2 的构建脚本换种子与幅度（漂移 1/2/4 cm、lag 1/2/3/5 帧、抖动 2/4/8 mm），找每类的检出下限并写入阈值 profile 的来源。

### 13.4 工程回归

关闭新模块：黄金基线与接口快照逐位一致（`python -m parity compare`）；打开新模块（含缺输入、异常、报错、VLM 超时）：旧 keep / drop / held 与交付清单一致；补 abort / resume、append-only parts、缓存失效、revision 一致、证据远端可读。VLM 用固定 tape 测 malformed / timeout / 引用不存在帧 / 与 CPU 冲突。

### 13.5 泛化验收（P6）

先冻结阈值，再在未用于选阈值的独立 episode（不同背景、工具、视角、遮挡）上报误报、漏报、定位误差、方向误差、时间误差、覆盖与弃权率，按机器人 / 相机 / 任务分组；样本不足只报计数与范围。

## 14. 实施计划

| 阶段 | 交付 | 人日 | 前置 | 停止 / 继续条件 |
|---|---|---:|---|---|
| P0 物理对应与标注协议 | 可见点清单、`annotation_protocol.md`、人工观测、复标一致性、split | 2–3 | 格式 | 至少一种点 / 方向可重复标注，重复误差明显小于最小可检异常；否则先调可见特征 |
| P1 格式、适配器、预检 | `extensions/eef_consistency` 的 contracts/load/geometry/timeline/adapters；三种入口；dataset1 11 条、dataset2 7 条、客户参考各 10 个全量转换；`mapping.yaml` 两份；逐分项预检 | 3–4 | 可与 P0 并行 | 源 hash 不变；重算投影在源量化精度内；ep6 偏差保留；答案不进检测输入 |
| P2 视觉测量实验 | provider 协议、P-A 与 P-B、流式解码、跟踪质量门、独立性扰动实验 | 4–6 | P0 标注、P1 读取器 | P95 达标才进入精确告警；不达标则 P3 只做数值轨迹与候选可视化 |
| P3 指标、诊断、离线报告 | 位置 / 方向 / lag / 数值 / 画面、分段、诊断、曲线与证据、受控异常矩阵 | 4–6 | P2 | 矩阵全部行为符合 §13.3；无阈值时不输出 ok |
| P4 接入 v2 | 注册表 1.4、预检、CLI、planner、aggregate 隔离、records、表、交付、前端默认渲染 | 4–6 | P3 契约稳定 | §13.4 回归门全过 |
| P5 VLM 复核 | 采样、请求包、Schema、预算、缓存、tape、冲突处理 | 2–3 | P3 证据格式、P4 调度 | 复核失败显示「未完成」，不影响旧判决 |
| P6 综合验收与试用 | 独立 episode 验收报告、性能记录、试用回执 | 3–4 | P4、P5 | 首轮只对指定数据集开 advisory；回退 = 取消勾选 |
| 合计 | | **22–32** | | 视 P2 结果调整 |

**DEMO 第一刀（在 feat/curator-v2 上直接做）**：P1 的契约与读取（`trajectory.json` 读取、校验、能力预检）+ L0 数值轨迹 + 几何投影与自洽 + 注册表 1.4 + CLI `check` 分派 + `parts` 输出 + 报告默认表格；视觉观测用 P-A（种子 + CPU 跟踪）；不含 VLM；对 dataset2 跑通并出受控矩阵的 CPU 部分。旧判决与 parity 逐位不变是每次提交的门。

提交拆分：① contract and adapters ② independent observations ③ cpu assessment ④ v2 advisory integration ⑤ vlm review ⑥ pilot validation。每次提交附行为、验证范围、未解决限制；每个阶段完成点按仓库纪律更新 `feature_list.md` 与 `claude-progress.txt`。

## 15. 未决事项

| 谁 | 事项 | 影响 |
|---|---|---|
| 客户 | §3.4 清单 10 项，特别是 EEF 定义、相对位姿约定、anchor、相机 ↔ 视图、逐帧媒体、容差 | 形态 C → B 的升级；阈值 profile |
| 客户 | §1.2 两种用法哪种为主；生成视频是否也要查 | `media.origin` 与报告措辞 |
| 我们 | P0 的可见点是否能与模型 TCP 建立可靠对应；dataset2 的 16 cm / 85 mm 仍是 model_assumed | 精确像素误差能否用「指尖中心」口径报告 |
| 我们 | 是否把本模块的局部 lag 反哺 `video_action_sync` | v2 算法升级，另立工作包 |
| 已定 | DEMO 第一刀的范围与视觉 provider 的种子来源见 §0.6 | — |

## 附录 A · 与前序方案的对照

| 我 22 日方案 | 本篇 | 变化原因 |
|---|---|---|
| 三层 L0/L1/L2，VLM 只判读 | 保留（§4.1、§10） | — |
| 残差形态六种签名作为归因结论 | 降为诊断假设（§8.6） | 整段拟合会抹掉固定偏移；主指标须保留原始残差 |
| hard 门 + 同步模块纪律 | advisory（§0.3、§9.4） | 测量精度未验证前不承诺判废 |
| 输入两种形态 | 保留并落到 `eef-video/1.0.0` 与 `mapping.yaml`（§3） | 需要可校验的契约而不是口头约定 |
| 检测器：开放词汇 + SAM2 起步 | provider 阶梯 P-A→P-B→P-C（§7.2） | 先用人工初始化建立噪声底，再自动化 |
| 用 dataset2 验收 | dataset1 + dataset2 受控矩阵 + 独立 episode 泛化（§13） | 同源变体不能当泛化 |

用户最初的方案（同步 / 运动 / 视觉质量输出作输入；投影是结构化数据；抽帧 VLM 比对；统计出结论）对应到：§4.2 辅助证据、§3 统一格式、§10 复核、§9 汇总。

## 附录 B · 名词对照

`eef_origin` 记录原点 · `tcp` 模型指尖中心 · `finger_line` 两指连线（无向） · `approach axis` 接近轴（EEF z） · `provided / recomputed` 客户投影 / 平台重算 · `observation` 独立观测 · `assurance` 来源等级 `declared / model_assumed / independently_calibrated / unknown / synthetic` · `advisory` 建议性、不判废 · `input_scope` 运行范围 · `affects_dataset_verdict` 是否影响判决。
