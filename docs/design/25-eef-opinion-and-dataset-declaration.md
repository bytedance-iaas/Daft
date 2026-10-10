# 25 · EEF–视频一致性重构：意见与置信度、数据集声明、能生成就能画

> 状态：**开工稿 v1.1（2026-10-09 晚，三轮评审后）**。评审答复见 §11（第三轮是对照代码的核实，改了 D84 的提醒方式、§7 的发现粒度与边界、
> 旧任务的缺省级别冻结，F5.24 拆成 a / b）；§10 是开工时的缺省选择，实测后可改。D81–D86 已登记到 00 §7、账本已加 F5.22–F5.26；
> **F5.22 输出口径、F5.23 数据集声明、F5.24a 第二屏与 VLM 开关已落地**（注册表 5.2、C4 5.1.0，§9.1–§9.3 是落地记录），下一步 F5.24b。
> 来源：2026-10-09 晚的讨论——「能不能画、能不能比，都不取决于 mcap 还是 LeRobot」「记录能还原就不该要用户上传轨迹」「模块都设计成只输出意见，
> 不再有判废」「两个渠道不平均、取大、冲突请人看」「完整版可视化展示原始数据里直接看得到的东西，迷你版服务任务」「数据集配置复用 mcap 的配置、做得更通用」。
> 工程基线：`feat/curator-v2` @ 8c4d89f8b（F5.21，trajectory.json 能算就可选、算不了就必选；注册表 4.4、C4 4.6.0、C7 `viz-mapping/1.1`）。
> 取代 / 修订：设计 12 §0.4 的 D-E11、D-E12、D-E13（合并判决、转人工口径、裁决线）与 §9.4；设计 12 §0.5 的「夹爪参考二选一」口径；设计 22 §2.2 的分工表；
> 设计 24 §2 的「机械臂数据缺标定」留待下一步——本篇就是那一步。不动：设计 12 §6–§8 的几何、观测独立性（D-E5）、逐相机不平均（D-E4）、
> 诊断假设不进判定（§0.3）、设计 17 的发现与策略层、设计 22 §3 的叠加机制与对时。

## 0. 开工指引（先读这一节）

1. **读的顺序**：§1 为什么改 → §2 两根轴与产品形态（一张图看全）→ §7 合并与最终输出（口径变化最大的一节）→ §3 数据集声明 → §4 预检 → §5 叠加 →
   §6 渠道 → §8 改动清单 → §9 分期与验收 → §10 开工时的缺省选择 → §11 评审记录。
2. **原则**：模块只报发现，级别由策略定（设计 17）；EEF 不再有判废，人可以判废、机器不判废（§7.5）。两个渠道的结论不平均（D82）。
   观测 provider 看不到声明的投影（D-E5）不变。所有「按假设值」「阈值未校准」的注记保留，置信度是个序、不是概率（§7.2）。
3. **分期**：先登记 D81–D86 与账本 F5.22–F5.26，再 F5.22 输出口径（别的都建立在它上面）→ F5.23 数据集声明与机械臂轨迹生成 → F5.24a 预检按两根轴给第二屏、VLM 开关 →
   F5.24b EEF 拆两半（设计 23 的段基础设施，动 planner 与派发）→ F5.25 数据集级叠加与完整版「可视化」→ F5.26 数据集级标定可疑（§9）。
4. **纪律**：直接在 `feat/curator-v2` 上按 feature 提交；A 类目录不动；每个 feature 过 `tests/eef`、`tests/contracts` 与锁、`tests/cli`、`tests/daemon`、前端五件套、
   对账回放（本篇改默认策略里 EEF 细码的级别，不勾 EEF 的任务判决逐位不变是回归门）。改契约按 `docs/contracts/README.md` 的流程，C4 的文案中英两版。
5. **数据**：dataset2（`eef_ds2_lr3`，Franka + 两路外部相机，有种子与模板，有注入真值）验 CPU 渠道与合并；`umi_das`（DAS mcap）与 cups / Trossen（UMI 原始会话与导出件）
   验腕部相机、生成与叠加；三者都在本机与 TOS，见设计 22 §0 的验收数据表。

## 1. 为什么改

今天的 EEF 模块是三周里按需求方逐条改出来的，几个口径互相缠在一起，用户看不懂：

| 缠在一起的 | 今天 | 问题 |
|---|---|---|
| 轨迹从哪来 vs 轨迹里的声明从哪来 | `trajectory.json` 一个文件装五样东西：位姿、相机标定、工具模型、单位与坐标约定、时间配对（设计 12 §3.7） | 机械臂数据的位姿明明在数据集里，却要用户整份上传；上传件里缺不了的其实是后四样「声明」 |
| 夹爪参考 vs 能不能画 | 种子 / 模板是 CPU 测量的锚点，与轨迹来源无关 | 容易被理解成「数据集能画就不用给」；其实它跟着相机类型走（§2.1） |
| 判废 vs 意见 | 有夹爪参考 → 一票否决 + 转人工（D-E11）；没有 → 只给意见（D-E15）；腕部相机 → 自运动 info | 同一个模块三种结论语义，报告里三套统计 |
| CPU 与 VLM 的关系 | CPU 测、VLM 复核投票、按表判废 / 转人工（D-E12） | 表有十行，用户记不住；需求方已决定不再判废 |
| 叠加在哪看 | 只有任务级接口，迷你播放器里看 | 完整版「可视化」页按数据集打开，没有任务就画不了 |
| 记录比对的意义 | 「上传轨迹 vs 数据集记录」（D-E16） | 轨迹由平台从记录生成之后，这一比有一半是循环论证（§6.1） |

本篇把它们拆成**两根轴**（§2）、**一份声明**（§3）、**一套输出口径**（§7）。

## 2. 两根轴与产品形态

### 2.1 两根轴

**轴一：轨迹从哪来**（预检按数据集与声明推导，§4）

| 来源 | 条件 | 用户要做的 |
|---|---|---|
| 生成 | 数据集有位姿记录（位姿列、关节角正解、手持夹爪的 TCP / VIO），声明里有相机标定与工具模型 | 什么都不用传；想换一份轨迹可以上传覆盖 |
| 缺声明 | 位姿在数据里，相机标定或工具模型没有 | 在数据集页补声明（§3），不是传轨迹 |
| 缺位姿 | 数据里没有可用位姿 | 上传 trajectory.json（必选） |
| 不支持 | 没有相机视频 | — |

**轴二：相机类型**（声明里每路相机一个 `mount`，§3.2）

| 相机 | 画面里夹爪 | 能做的渠道 |
|---|---|---|
| 第三视角 `fixed_external` | 会动 | CPU 测量（要夹爪参考）、VLM 意见 |
| 腕部 `wrist`，属于本手 | 不动 | 自运动一致性（CPU）、VLM 意见 |
| 会动的 `moving`（无逐帧相机位姿的头部相机） | 不定 | 不支持 |

### 2.2 产品形态（一张图）

```text
登记数据集 ──► 声明（C7 扩，§3）：来源→角色 · 语义 · 标定；内置模版起草、表格确认、版本化
     │
     ▼
预检（§4）：每路相机 mount 与可画性；轨迹来源（生成 / 缺声明 / 缺位姿）；每个分项的能力
     │
     ├──► 数据集「可视化」页（完整版）：能生成就有「叠加」菜单（数据集级轨迹，§5.1）
     │
     ▼
新建任务 · 第二屏（§4.3）：
     轨迹：来源=生成 → 可选（上传即覆盖并多做「上传件对记录」）；来源=缺位姿 → 必选
     夹爪参考：有第三视角相机才出现；给了 → CPU 测量渠道；没给 → 该相机只有 VLM 渠道
     VLM 辅助：开关；只改渠道组成，不改输出形状
     │
     ▼
运行（§6）：渠道各自出「结论 + 置信度 + 读数」→ 合并（§7）→ 发现（不一致 / 冲突 / 记录不符 / 自运动 / 标定可疑）
     │
     ▼
报告 · Episode 明细 · 裁决卡（只有「冲突」）· 迷你播放器叠加（任务冻结的轨迹，§5.2）
```

### 2.3 决策

| 编号 | 决策 |
|---|---|
| D81 | **EEF 只出意见与置信度，不判废。** 细码 `inconsistent` 缺省 info，严重度随置信度档位；新细码 `conflict`（两个渠道结论相反）缺省 review、裁决线 `eef_check`；人在卡上答「不一致」仍可让它 blocking（人可以判废，机器不判废）。`unsettled`、`opinion_mismatch` 停用，旧记录保留 |
| D82 | **渠道不平均。** 每个渠道对它能看的分项给结论与置信度；按分项、按相机合并：不一致置信度取各渠道的最大值；结论相反且都不低 → 标「冲突」；只有一个渠道 → 封顶；模型多数窗口认为跟错目标 → CPU 该分项作废；episode 取最差的分项与相机，不跨相机平均（D-E4 不变） |
| D83 | **数据集声明取代上传件里的声明部分。** C7 从「mcap 字段映射」扩成通用的数据集声明：来源→角色、语义（单位、布局、参考点、参考系）、标定（相机 mount / 内参 / 外参来源、工具模型、手持夹爪的几项）；LeRobot / mcap / Lance / 原始会话都有一份；内置模版 + 关键词 + `robot_type` 起草、表格确认、版本化、另存为模版复用、开跑时冻结进 `run.json`。轨迹由声明 + 数据集记录生成；上传 trajectory.json 只作覆盖 |
| D84 | **第二屏按两根轴。** 轨迹按来源可选 / 必选；夹爪参考只在有第三视角相机时出现；「VLM 辅助」是开关、缺省开，不改输出形状；EEF 的 `needs` 不再含 `vlm`；没有 VLM 后端时照常建任务，预检照常 `available` 并附一条提醒（`notes` 里的 `vlm_backend_missing`，不用 `needs_input`——它会让 Daemon 拒建任务），运行时模型渠道按「缺」处理。EEF 按设计 23 的 D76 拆两半：不调模型的一切在 CPU 块的 `vlm_prep` 段，只发请求收答案的在 VLM 块的 `vlm` 段；关掉 VLM 就只剩前一半 |
| D85 | **叠加分两级。** 完整版「可视化」读数据集级生成的轨迹（按数据集 + 声明版本缓存）；迷你版读任务冻结的那份；同一个叠加函数。上传覆盖时，记录算出的轨迹作为虚线图层组同画 |
| D86 | **数据集级标定可疑。** 同一路相机上多数条目同方向、同量级的恒定偏差，诊断层升为一条数据集级发现 `calibration_suspect`（info），条目级发现加注「可能是标定或假设值」；不逐条出卡 |

## 3. 数据集声明（C7 扩）

### 3.1 定位

**就是今天 mcap 的字段映射（C7）**，一份按数据集保存、按版本冻结的文档，**可视化与质检共用**，扩成它的超集并推广到所有格式：C7 的「来源→角色」原样保留，再加「语义」和「标定」两层。
`eef-mapping/1.1` 的 `record` 块、`umi-calibration/2`、设计 12 §3.3 的 `mapping.yaml` 都并进来，不再各自一份。手写、导入或从模版起草都行；
「另存为模版」承担「同一套采集装置复用标定」（每种夹爪一份、每台机器人一份），不新造资源类型。

**一份文档、一个版本号**（第二轮评审定）：契约 C7 改名为 `dataset-declaration/1.0`，文件 `docs/contracts/dataset-declaration.schema.json`（`viz-mapping.schema.json` 留一版做别名，
下次改契约时删）；`viz-mapping/1.x` 的文件照样有效，读进来就是只有第一层的声明。数据集上的字段 `Dataset.viz_mapping` 改名 `declaration`，C4 保留旧名一版做别名。

### 3.2 内容

| 层 | 内容 | 按什么一份 | 起草 | 来源 |
|---|---|---|---|---|
| 来源→角色 | 相机、深度、曲线组（state / action / other）、任务文本、分段、忽略、时间线 | 数据集 | 能 | C7 现状；LeRobot / Lance 由 `info.json` 自动得出，用户可改 |
| 语义 | 位姿来源：列或 topic、布局（`xyz_rpy_xyz_extrinsic` / `xyz_quat_*` / rot6d）、单位、`frame_id`（法兰 / 指尖 / VIO 机体）、`reference_frame`；关节来源：列或 topic、切片、单位、机型；开口来源：列、单位、`closed_fraction` 换算 | 记录来源 | 能起草 | 分量名与 `robot_type`（D-E17 的起草器 `record_draft.py`），缺的按惯例并逐条列为假设 |
| 标定 · 相机 | `mount`（`fixed_external` / `wrist` / `moving`）；腕部相机属于哪只手或哪条臂；内参 `K`、畸变模型与系数、标定图尺寸；外参：静态 `T_reference_camera`、或逐帧相机位姿列、或腕部相机的 `T_camera_tcp`；媒体像素变换 `H`（裁剪、缩放） | 相机 | 看情况 | `mount` 按关键词给缺省（§3.3）；内参：mcap `camera_info` 能，LeRobot 要上传；外参：DROID 式 `camera_extrinsics.*` 列能，否则要上传 |
| 标定 · 工具 | TCP 相对位姿点的偏移、手指开合轴、最大开口；点与轴的定义（P、A、B 怎么挑） | 机器人 + 夹爪的组合，按数据集声明；与相机无关 | 只预填 | 不是常数：换夹爪、位姿列本来就是 TCP，值都不同。`robot_type` 认得出时预填该机型官方夹爪的值并标 `model_assumed`（Franka Hand：TCP 沿 z 0.1034 m、手指沿 y、开口 0.08 m），用户在声明里改；客户给 CAD 值后改 `declared` |
| 标定 · 手持夹爪 | `pose_frame`、`body_to_optical`、`T_camera_tcp`、开口单位与比例、`intrinsics_fallback`、`intrinsics_scaling`、配对容差 | 夹爪型号 | 内置缺省 | 今天的 `umi-calibration/2` 与 `calibrations/das_gripper_demo.json` |
| 时间 | 哪列 / 哪个字段是时间戳，位姿流与视频的时钟与容差 | 数据集 | 能 | C7 `timeline` 现状 + 设计 22 §5.2 的配对规则 |

每一项都带 `assurance`（`declared` / `model_assumed` / `unknown`）与起草时的假设代码；报告与叠加里「按假设值」的注记由此而来，`declared` 才去掉。

### 3.3 起草规则（补 D-E17 没覆盖的）

- `mount`：相机键或 topic 名里含 `wrist`、`hand`、`gripper_cam`、`eye_in_hand` → `wrist`；含 `exterior`、`external`、`front`、`side`、`top`、`third`、`overhead`、`table` → `fixed_external`；
  含 `head`、`chest`、`body` → 缺省 `moving`（人形与移动机器人的头部相机会动，没有逐帧头部位姿就不能默认成第三视角）；认不出 → 问。
  `moving` 的相机用户可以在声明里改成 `fixed_external`（「这路相机实际不动」，标 `model_assumed`、假设代码 `mount_declared_fixed`），改了就按第三视角做；它真动起来时画面运动分项会报，报告注明「按声明视为固定」。
  内置 UMI 模版：`/robotN/sensor/cameraN` → `wrist`，属于 `robotN`。每条规则记一个假设代码（`mount_from_keyword` 等）。
- 腕部相机的归属：机械臂一条臂时唯一；双臂按键名里的 `left` / `right` 或 `robotN` 配对，配不上就问。
- 内参：mcap 的 `CameraCalibration` / `CameraInfo` topic 直接读并记来源；LeRobot 的 `meta/` 下有 `calibration*.json` / `umi_calibration.json` 就读，没有留空、标「要上传」。
- 外参：逐帧列（DROID 的 `camera_extrinsics.<cam>`）按分量名认；mcap 的 `/tf` / `FrameTransform` 里有相机帧也认；都没有留空。
- 工具模型：只预填、不当常数。`robot_type` 认得出机型就按该机型官方夹爪预填（Franka：位姿点 `panda_link8`，TCP 沿 z 0.1034 m，手指沿 y，开口 0.08 m），假设代码 `tool_from_robot_type`；
  位姿列的分量名或列名含 `tcp` / `ee` 时预填「位姿列已是 TCP、偏移 0」；手持夹爪按型号预填今天的 DEMO 值。每个数据集在声明里确认或改。
- 语义：沿用 `record_draft.py`，位姿列的 `frame_id` 不起草为 null 而是按机型给缺省并标假设（机械臂的 `cartesian_position` 多数是法兰或 TCP，表单里改）。

### 3.4 确认时的校验

到货即校验，失败逐条定位到字段：刚性检查（外参、`T_camera_tcp`、`body_to_optical`）；内参换到视频尺寸后 fx / fy 偏差 > 2% 报可疑；开口范围超出 [0, 0.2] m 报可疑；
位姿流与视频的配对成功率 < 90% 报可疑（有时间戳时）；关节列与机型的自由度数一致；再抽三帧把 TCP、三轴与两指连线投影出来给人看一眼落点（不解码整段，只取三帧）。
可疑项不拦确认，写进声明的 `suspects`，报告沿用「按假设值 / 可疑」注记。

### 3.5 版本、冻结与任务参数

- 声明随数据集保存，带版本号与更新时间（沿用 `Dataset.viz_mapping` 的做法）；预检缓存按声明版本失效；任务开跑时整份冻结进 `run.json`（D62 的做法），
  运行目录与报告引用冻结版本；改声明不改已跑任务。
- 留在任务参数里的只有运行时的选择：`trajectory_json`（覆盖）、`observation_seeds` / `gripper_template`（夹爪参考）、`use_vlm`（开关）、`threshold_profile`、
  `camera_mounts`（参与的相机子集）、窗口与搜索范围。`gripper_calibration` 与 `record_mapping` 两个任务参数并入声明后停用（旧任务按旧注册表读）。

## 4. 预检：轨迹来源与可画性

### 4.1 轨迹来源的顺序（设计 24 §1 改写）

1. 任务参数 `trajectory_json`（覆盖）；
2. **按声明从数据集生成**：机械臂 LeRobot / mcap / Lance 由 `adapters/lerobot_mapping.py` 的同一套生成（它今天就是「列 + 映射 → 轨迹包」，只是没接进平台；mcap / Lance 的位姿来源按 topic / 列读）；
   手持夹爪 LeRobot 走设计 24 §1 第 2 条；原始 UMI 会话走设计 24 §6；原始手持夹爪 mcap 走设计 22 §5.4；
3. 数据集根目录自带的 `trajectory.json`；
4. 都没有 → 按 §2.1 轴一报「缺声明」或「缺位姿」。

生成结果是形态 B（位姿 + 标定，投影由平台重算），所以不存在「提供投影与重算不一致」；上传覆盖时照旧做输入自洽。

### 4.2 能力表（设计 12 §5.1 的扩展）

模块条目多三样：

- `trajectory_source`：`upload` / `generate` / `dataset_file` / `session` / `mcap_derive` / `missing_declaration` / `missing_pose`；`missing_*` 时 `needs_input` 的 `input_hint` 指向声明页的哪一节或 `trajectory_json`；
- `cameras[]`：每路相机的 `mount`、归属、`drawable`（算得出投影）、算不出的原因码、可做的分项（第三视角：位置 / 朝向 / 时间 / 画面运动；腕部：自运动）；
- `applicable_params`：这个数据集上第二屏该出现的参数（有第三视角相机才有 `observation_seeds` / `gripper_template`）。

模块可用的条件不变：任一分项可用即可用。预检不解码视频，`drawable` 只说明「算得出投影」，证明不了「画得对」，后者是模块要查的。

### 4.3 第二屏（设计 07 §4.3 的 EEF 一节改写）

| 项 | 规则 |
|---|---|
| trajectory.json | `trajectory_source` 是 `missing_pose` → 必填；`missing_declaration` → 先出一句「到数据集页补声明」并链过去，上传框折叠在下面的「改为上传 trajectory.json」里（上传即绕过声明）；其余可选，说明「上传即覆盖平台生成的轨迹，并额外比对上传件与数据集记录」 |
| 夹爪参考 | 只在 `cameras[]` 有第三视角相机时出现；二选一分组不必填；说明「给了就按画面测量，不给只有模型意见」 |
| 使用 VLM 辅助 | 开关，缺省开。没有可用的 VLM 后端时照常允许建任务，预检的 EEF 条目照常 `available`，`notes` 里一条 `vlm_backend_missing` 作提醒，运行时模型渠道按「缺」处理（§7.1 的 `single_source` 写明原因）；关掉时不提醒、不要求后端。两种情况下都只跑 CPU 块里的那一半（D84） |
| 其余 | 阈值 profile、参与的相机、窗口与搜索范围照旧放高级设置 |

## 5. 叠加：数据集级与任务级

### 5.1 完整版「可视化」（数据集级，D85）

- 新接口 `GET /datasets/{id}/episodes/{index}/eef-overlay`：按数据集的声明生成这一条的轨迹包（§4.1 第 2–3 条，不含上传覆盖），算图层，返回与任务级同一个 `EefOverlay` 结构；
  缓存键 = 数据集 id + 声明版本 + episode；生成结果落本地盘缓存（与转码缓存同一处，`CURATOR_VIZ_*`）。
- 页面：预检说这路相机 `drawable` 就出现「叠加」菜单，图层与缺省开关同迷你版；算不出的相机在菜单里置灰并给原因（缺内参、缺外参、会动的相机）。
- 它展示的是「从原始数据与声明直接得到的几何」，没有观测点、残差、模型意见——那些是任务的产物，只在迷你版。
- 首次打开要读位姿列（LeRobot parquet / mcap 小文件），TOS 上的大数据集有几秒延迟；缺省按需生成，数据集预检成功后由 Daemon 在后台预生成前 3 条（§10 第一行；
  预检是 CLI 子进程，缓存是 Daemon 的，CLI 不写 Daemon 的缓存）。

### 5.2 迷你版（任务级，现状）

读任务冻结的 `inputs/eef/trajectory.json`（上传的或生成的），加任务产物图层（观测点、观测轨迹、残差、模型意见的证据帧）。

### 5.3 上传覆盖时的「记录」图层组（D85）

上传件覆盖平台生成的轨迹时，迷你版多一个图层组 `record`：由数据集记录算出的 TCP 轨迹与三轴，**虚线**，各走各的标定——上传件用自己带的，记录用声明里的，
屏幕上两条线的距离就是两边的总差异。它取代今天记录比对的静态证据图（设计 12 §8.7 末段），数据来源不变（`record.py`）。

### 5.4 腕部相机的叠加说明

本手在画面里不动，叠加只画本手的中心、两指连线与开口、三轴、过去 / 未来轨迹（轨迹随相机运动重投影，能看出世界系轨迹的形状）；页面与报告都注明「腕部相机的标记检验不了本手的位姿，看自运动一致性」。

## 6. 渠道

每个渠道对它能看的分项、在每路相机上给：`verdict`（`issue` / `ok` / `cannot_tell`）、`confidence`（0–1，对该结论的把握）、读数与证据。统一换算成「不一致置信度」p：
`issue` → p = 低档 + (1 − 低档) × confidence；`ok` → p = 低档 × (1 − confidence)，confidence 为 0 的 `ok` 无 p；`cannot_tell` → 无 p。
低档取任务 profile 的（demo 0.4），合并时按它重算。这样有证据的「有问题」至少是「可能不一致」，「没发现」永远落在「一致」里，证据只决定离边界多远
（F5.22 定；v1.0 写的是 `issue` → confidence、`ok` → 1 − confidence，按它 CPU 刚过开阈值的可疑是「一致」；开工时先改成以 0.5 为中点，
dataset2 实测证据少的「没发现」仍落进「可能不一致」，见 §11 第三轮）。

| 渠道 | 相机 | 分项 | 结论怎么来 | 置信度怎么来 | 前提 |
|---|---|---|---|---|---|
| CPU 测量 | 第三视角 | 位置、朝向、时间对齐、画面运动 | 设计 12 §8 的残差与迟滞分段 | 读数超过阈值的幅度（0 在开阈值、1 在 3 倍开阈值，线性）× 覆盖率；覆盖不足 → `cannot_tell` | 夹爪参考（种子 / 模板） |
| 自运动 | 腕部 | 自运动 | 设计 22 §5.3 | 同上，用旋转差与相对误差 | 标定、逐帧相机位姿 |
| VLM 复核 | 第三视角，CPU 渠道在 | 位置、朝向 | 窗口投票（设计 12 §10、D-E14）：反对多 → `issue`，支持多 → `ok`，平票或一票没有 → `cannot_tell` | \|反对票 − 支持票\| / 有答复窗口数 × 有答复窗口占比 | 夹爪参考 + VLM 开 |
| VLM 意见 | 第三视角无夹爪参考；腕部 | 位置 / 朝向（第三视角）、朝向（腕部，`umi-action-prompt/9`） | 整段标记视频的不匹配片段（设计 12 §10.5、设计 20） | 片段里最高的不匹配置信度 × 问过的片段占比；没有片段 → `ok`，置信度按问过的占比 | VLM 开 |
| 记录比对 | 不看视频 | 记录一致性（独立于上面） | 设计 12 §8.7 | 不进合并，单独报告 | 见 §6.1 |

额外的两个信号只做修正、不独立成渠道：VLM 复核的 `tracking_target_correct` 多数反对 → 这路相机的 CPU 渠道作废（§7.1）；输入自洽 `input_inconsistent` 只提示。

### 6.1 记录比对的两条

- **数据集内部**：位姿列与关节角正解都在时互比（恒定差、随时间的差、时间差），不需要任何上传，有两份记录就跑；细码 `record_mismatch` 加读数 `source: internal`。
- **上传件对记录**：只在上传覆盖时做，比的是「客户导出的轨迹 vs 原始记录」；`source: upload`。
- 轨迹由平台从唯一的一份记录生成时不比（循环论证），报告里写「轨迹来自数据集记录，无需比对」。

## 7. 合并与最终输出

### 7.1 规则（D82）

按**分项 × 相机**合并，得到 p 与标记：

| 情形 | p | 标记 |
|---|---|---|
| 两个渠道都有 p | max | 若一个 ≥ 高档下限、另一个 < 低档上限（与 §7.2 的「一致」同一条边界）→ `conflict` |
| 只有一个渠道有 p | 该渠道的 p，封顶 `single_source_cap`（缺省 0.8） | `single_source`（写明缺的是哪个渠道、为什么：没给夹爪参考 / 没开 VLM / 没有模型后端 / 没问模型这一项 / 模型没答 / 模型拿不准（答了但没表态、平票） / 分项模型看不了 / CPU 不测 / CPU 判断不了） |
| VLM 多数窗口认为跟错目标 | CPU 的 p 作废；若 VLM 自己有 p 则按单渠道，否则无 p | `tracking_invalid`（提示重新点种子或重建模板） |
| 两个渠道都 `cannot_tell` | 无 | 该分项「判断不了」，进覆盖率统计 |

episode 级：取所有分项、所有相机里 **p 最大**的那个，不平均；它的分项、相机、渠道组成与标记就是「依据」，写在 `details.merged.episode`。
**发现的粒度**（第三轮定）：每个达到「可能不一致」的分项 × 相机出一条 `inconsistent`（读数里是这一格的 p、渠道与标记；腕部相机的自运动格出
`ego_motion_suspect`），冲突的格只出一条 `conflict`（不再另出 `inconsistent`）；条目级的标签不单独出发现。

### 7.2 标签与置信度

| p | 标签 |
|---|---|
| ≥ 高档下限（demo 0.7） | 不一致 |
| 中档（demo 0.4–0.7） | 可能不一致 |
| < 低档上限（demo 0.4） | 一致 |
| 无 p | 判断不了 |

档位写在阈值 profile 里，demo profile 标 `calibrated: false`；报告与卡片都注「置信度未校准，是序不是概率」。回归样本集（设计 16）有真值后按渠道、按档校准命中率，再换正式 profile；
取大的规则不随校准变。

最终输出三样：**标签、不一致置信度 p、一句话依据**（分项、相机、渠道、读数、标记）。例：「不一致 · 0.90 · 冲突：位置（相机 exterior_1），CPU认为不一致（0.90），模型复核认为一致（0.10），诊断支持：extrinsics_error」。

### 7.3 发现与缺省策略（D81，设计 17 的表增改）

| 细码 | 分类表 | 严重度 | 缺省级别 | 裁决线 | 读数 |
|---|---|---|---|---|---|
| `inconsistent` | MV-4 | 按标签：不一致 high、可能不一致 medium | **info** | — | `item`、`camera`、`p`、`sources`、`cpu`、`vlm`、`flags` |
| `conflict`（新） | MV-4 | medium | **review** | `eef_check` | 同上，两个渠道的证据都带 |
| `ego_motion_suspect` | MV-4，兼 AV-1 | 随档 | info | — | 现状；它是腕部相机自运动分项的 `inconsistent`，细码保留以维持 AV-1 覆盖 |
| `record_mismatch` | MV-4 | low | info | — | 加 `source: internal / upload` |
| `calibration_suspect`（新，数据集级） | MV-4，`scope: dataset` | medium | info | — | 相机、条目数、恒定偏差的方向与量级、PnP 修正量（D86） |
| `unsettled`、`opinion_mismatch` | — | — | 停用 | — | 旧记录照读，新任务不产生 |

- 「可能不一致」是否也提到 review、「不一致」要不要先请人确认，由任务的策略改，模块不管（P18 的精神）。
- `eef_check` 卡片只在 `conflict` 时出现；内容是两个渠道的证据并列（CPU 证据帧与残差曲线、模型的窗口答复与标记图）；答「一致」→ 该发现作废，「不一致」→ blocking（kind human），「拿不准」→ 留着。
- 条目不因 EEF 进 drop；held 的规则不变（模块出错或没记录）。
- 旧任务不管：被旧版 EEF 判废的条目不自动复议，旧任务的报告按旧注册表读、顶部提示「按旧口径（注册表 4.x）」。
  **缺省级别要冻结**（第三轮核实）：今天 `run.json` 只冻结策略的预设与规则，细码的缺省级别每次 aggregate 取当前注册表——旧任务以后再出结果版本
  （裁决、补跑、继续）时，旧版判废的 EEF 条目会按新的 info 被放回。所以 5.0 起开跑时把所选模块各细码的缺省级别与可复议的细码一并冻结进
  `run.json` 的 `policy`（`defaults`、`appealable`）；没有这份冻结的旧运行按 `run.json` 的 `registry_version` 用旧表（4.x：EEF `inconsistent` =
  blocking、可复议），停用的细码留在注册表里、标 `retired`。

### 7.4 数据集级标定可疑（D86）

诊断层今天对位置分项已算 PnP 外参修正，并有「同一相机跨 episode 一致 → 升数据集级」的条件（设计 12 §8.6）。落地为：同一路相机上 ≥ 60% 且 ≥ 5 条条目的位置分项恒定偏差方向一致（角度中位差 < 30°）、
量级相近（变异系数 < 0.5）→ 一条 `calibration_suspect`，写明「多半是外参、TCP 偏移或假设值，不是逐条的数据问题」；这些条目的 `inconsistent` 加 `flags: calibration_suspect`，报告的 EEF 小节单列。
手持夹爪按 DEMO 假设值跑时常会触发，正是它的用途。

### 7.5 和今天的判决怎么对

| 今天（D-E12） | 本篇 |
|---|---|
| CPU 可疑 + 模型反对多 → 判废确认 | 不一致，p 高，无冲突；info |
| CPU 可疑 + 模型支持多 → 转人工（冲突） | 不一致 / 可能不一致，`conflict`；review |
| CPU 可疑 + 模型一票没有 → 转人工 | 单渠道封顶；info |
| CPU 正常 + 模型反对多 → 转人工（冲突） | p 取模型的，`conflict`；review |
| 时间 / 记录抖动 / 画面运动 CPU 可疑 → 转人工 | 单渠道封顶；info |
| 跟错目标 → 转人工 | CPU 作废，`tracking_invalid`；info |
| 文件里没有 / 位置到处无法评估 → 转人工 | 判断不了；不出卡 |
| 意见模式 `opinion_mismatch` | VLM 单渠道的 `inconsistent` |

## 8. 改动清单

| 组件 | 改动 |
|---|---|
| 契约 C1（注册表 → 5.0） | `inconsistent` 级别 blocking → info、严重度按档；新 `conflict`（review，`eef_check`）、`calibration_suspect`（数据集级）；`unsettled`、`opinion_mismatch` 标 retired（留在目录里，旧记录取级别用）；EEF 的 `needs` 去掉 `vlm`；参数：新 `use_vlm`；`gripper_calibration`、`record_mapping` 停用；`param_schema` 的夹爪参考分组加 `x-applies-when: third_person_camera`；模块声明两段（CPU 块 `vlm_prep` 与 VLM 块 `vlm`，设计 23 §4.1），`use_vlm=false` 时计划只排前一段 |
| 契约 C2 | EEF 记录 Schema：`details.channels{cpu,vlm,ego,…}`、`details.merged{per_item_camera[], episode{label,p,reason,flags}}`、`details.record{sources, internal, upload}`；预检条目加 `trajectory_source`、`cameras[]`、`applicable_params` |
| 契约 C4（→ 5.0.0） | `Dataset.declaration`（含版本）取代 `viz_mapping` 字段名，`GET / PUT /datasets/{id}/declaration`（`/mapping` 保留一版作别名）；新 `GET /datasets/{id}/episodes/{index}/eef-overlay`；`EefOverlayLayer.group` 加 `record`；上传种类去掉 `eef_gripper_calibration`、`eef_record_mapping`，加 `dataset_declaration`；客户可见变更记录中英两版 |
| 契约 C7（改名 `dataset-declaration/1.0`） | §3.2 的三层，一份文档一个版本号（§3.1）；`viz-mapping/1.x` 文件仍有效；示例与不合法示例；`docs/contracts/SUMMARY.md`、`README.md` 的 C7 一行改名 |
| 契约 C6 / 回归工具 | `taxonomy.json` 平台注记重生成；`finding_map.json` 不动（只管旧格式）；`score.py` 的 MV-4 期望值按「标签 + p」改（F5.22 验收 ⑤） |
| 内核 `extensions/eef_consistency/` | 跑在两段里：CPU 半段（解码、观测、测量、自运动、标记视频与请求包）与模型半段（请求、答复、合并），`runner.py` 按段拆；新 `channels.py`（各渠道的 verdict / confidence 换算）、`combine.py`（§7.1–7.2，取代 `decide.py`）；`capability.py` 加 `trajectory_source`、`cameras[]`；`derive.py` 接 `lerobot_mapping` 生成；`record.py` 的两条模式；`diagnosis.py` 数据集级聚合；`report.py` 按新口径；`profile.py` 加档位与封顶 |
| 策略（设计 17） | 开跑时把所选模块各细码的缺省级别冻结进 `run.json` 的 `policy.defaults`；`Policy.level` 先查冻结的缺省，没有时按 `registry_version` 查旧缺省表（§7.3） |
| CLI / planner | `preflight` 读声明、给 §4.2 的字段；`check` 的 `EefJudge` 改调 `combine`；`aggregate` 的数据集级聚合写 `calibration_suspect`；EEF 按设计 23 §2.1、§4 拆两半（D76：CPU 块加 `vlm_prep` 段跑解码、观测、测量、自运动、标记视频编码与请求包暂存 D77；VLM 块的 `vlm` 段只发请求收答案、合并、出发现），`use_vlm=false` 或没有后端时计划里没有 VLM 半段、合并在 CPU 半段末尾做。设计 23 的 S3 中 EEF 这一半并入本篇 F5.24，任务成败判定那一半仍按设计 23 走 |
| Daemon | 声明的存取与版本（沿 `viz_mapping` 的仓储）、起草接口；数据集级轨迹生成与缓存；`eef-overlay` 的数据集级路由与 `record` 图层组；预检缓存按声明版本失效 |
| 前端 | 数据集页「声明」表格（三层分节、假设逐条列、可疑项、另存为模版）；完整版播放器「叠加」菜单；第二屏按 `applicable_params` 与 `trajectory_source`；`use_vlm` 开关；EEF 结果组件改成「标签 · 置信度 · 依据」+ 渠道分栏 + 冲突卡；报告 EEF 小节一套统计（标签分布、置信度分布、冲突数、判断不了的原因、标定可疑） |
| 文档 | 本篇；00 §7 登记 D81–D86 并注明 D-E11–D-E13 作废；设计 12 §0.4 / §0.5 / §9 / §10.5 / §11.4 指到本篇；设计 17 的 EEF 行；设计 18 §6 改为声明；设计 07 §4.3 / §4.5；设计 22 §2.2；设计 24 §2；extension README、frontend README、Daemon README 的手动验证 |

## 9. 分期与验收

| Feature | 内容 | 验收（dataset2 = d2，`umi_das` = das，cups 原始会话 = cups） |
|---|---|---|
| F5.22 输出口径 | §6 渠道换算、§7 合并、细码与缺省策略、结果组件与报告小节；输入侧不动 | ① d2 种子模式 + 假模型：ep0 「一致」、ep5 时间对齐单渠道封顶 0.8、ep6 位置「不一致」且 `extrinsics_error` 支持；人为让假模型在 ep6 支持 → `conflict` 出卡、答「一致」后发现作废、答「不一致」后 blocking ② d2 不给夹爪参考：VLM 单渠道，全部 ≤ 0.8，无卡 ③ das：自运动单渠道，`00001` 一致、robot1 晚 0.5 s → 不一致 ④ 不勾 EEF 的任务判决逐位不变（对账回放）；旧运行目录按旧注册表读出 `unsettled` 不报错 ⑤ 回归样本工具 `score.py` 的 MV-4 按「标签 + p」打分，对照表与注册表一致性检查过；样本集里 EEF 注入条目不够的地方记成缺口，不在本 feature 里造样本 |
| F5.23 数据集声明 | C7 扩、起草、表格、校验、冻结；机械臂轨迹生成接通（`lerobot_mapping` 今天只读本地 LeRobot 目录，要补 TOS 流式读与 mcap / Lance 的位姿来源） | ① d2 登记后起草出两路 `fixed_external`、位姿列 + 关节列 + 外参列，内参留空「要上传」，工具按 `robot_type` 预填并标假设（dataset2 是 DROID：Robotiq 2F-85，不是 Franka Hand）；补内参、把工具改成 dataset2 的值（TCP 沿 z 0.16 m、开口 0.085 m）后 `trajectory_source=generate`，生成的轨迹与 `dataset2/trajectory.json` 逐帧投影差 < 0.05 px ② das 起草出两路 `wrist` 归 `robot0/1`，手持夹爪缺省值全 `model_assumed` ③ 改声明后旧任务不变，新任务冻结新版本 ④ `viz-mapping/1.1` 旧文件照读 |
| F5.24a 第二屏两根轴与 VLM 开关 | §4.2–4.3；`use_vlm`；EEF 的 `needs` 去掉 `vlm` | ① d2 无内参时第二屏显示「到数据集页补声明」，上传框折叠在「改为上传 trajectory.json」里；补齐后轨迹可选、夹爪参考出现 ② das 无夹爪参考项、轨迹可选 ③ 只有图片的数据集 EEF 不支持 ④ `use_vlm=false` 的任务不要求 VLM 后端、输出形状同；没配后端但开着 VLM 的任务能建、预检 `available` 带提醒、记录的 `single_source` 写「没有模型后端」 ⑤ 声明里把一路 `moving` 相机改成「视为固定」后，它按第三视角参与、报告带注记 |
| F5.24b EEF 拆两半 | EEF 的 CPU 半段进 CPU 块 `vlm_prep` 段（设计 23 D76、D77 中 EEF 的部分；先补设计 23 的段基础设施：C1 的段、planner、Daemon worker、跨段续跑） | ① `use_vlm=false` 时 EEF 全在 CPU 块跑完 ② 开着 VLM 时 CPU 半段的产物（观测、曲线、请求包）在 CPU 块落盘，VLM 半段只读它们 ③ 暂停 / 续跑跨两段正确 ④ 不勾 EEF 的任务判决逐位不变 |
| F5.25 数据集级叠加 | §5.1、§5.3 | ① d2 完整版「可视化」有「叠加」菜单，两路画 P、B、三轴、过去轨迹，与迷你版同帧同位 ② 上传覆盖的任务迷你版多 `record` 虚线组，ep2 上两条线错开 30° ③ TOS 上的 das 首次打开 < 5 s，第二次命中缓存 ④ 腕部相机页面有「检验不了本手位姿」说明 |
| F5.26 标定可疑 | §7.4 | ① d2 把 `exterior_1` 的外参整体写错 3 cm 跑 7 条 → 一条 `calibration_suspect`，7 条 `inconsistent` 带 flag，报告单列 ② das 按 DEMO 标定若触发则注明「假设值」 |

### 9.1 F5.22 落地记录（2026-10-09）

- **代码**：`channels.py`（各渠道的结论与置信度）与 `combine.py`（合并与输出）取代 `decide.py`。记录里的 `details.merged`：`cells[]`
  （每格 `subitem`、`camera`、`p`、`label`、`flags`、`sources`；单渠道带 `missing`，判断不了带 `why`）、`episode`（`label`、`p`、`subitem`、
  `camera`、`flags`、`reason`、`conflicts`、`cells_rated`、`cells`）、`bands`。§8 写的 `details.channels` 没有单列：每个渠道的结论、置信度、p
  与读数就在格子的 `sources` 里（`cpu` 或 `ego`，`vlm_review` 或 `vlm_opinion`）。
- **置信度的算法**（§6 表的细化）：CPU 的比值 r = 读数 / 开阈值（位置取 `median_px` 与段峰值对 `on_px` 的大者，朝向同理；时间对齐
  `lag_frames / min_lag_frames`；画面运动对 `hf_on_px`；记录抖动取高频与尖峰两个比值的大者），强度 s = clip((r − 1) / (full_at − 1))，
  full_at = 3（profile 的 `merge.full_at`）；`issue` 的置信度 = s × 覆盖率，`ok` 的 = 覆盖率。复核：可疑的分项只看它的候选窗口，
  其余分项看非候选窗口。意见：m = 该分项片段里最高的不匹配置信度（没有片段为 0），答复占比 share，e = 0.5 + (m − 0.5) × share，
  m > 0.5 为 `issue`，置信度 = |2e − 1|。自运动：最差一段的时间差对 `lag_min_s`、旋转差对 `rotation_on_deg`，乘覆盖率。
- **版本**：注册表 5.0；C4 4.7.0（不是 §8 写的 5.0.0：这一步只给 `FindingCode` 加了 `retired`，向后兼容；声明带来的不兼容改动随 F5.23 再升）。
- **策略冻结**：开跑时写 `run.json` 的 `policy.defaults` 与 `policy.appealable`；没有这两项的旧运行按 `registry_version` 查旧表
  （`policy.LEGACY_DEFAULTS`、`LEGACY_APPEALABLE`：4.x 的 EEF `inconsistent` = blocking、可复议）。
- **报告**：EEF 小节 5.0 起给标签分布、p 分布、冲突（条目与格）、单渠道条目与缺的原因、判断不了的原因、按分项的不一致、跟错目标，明细表
  `eef_opinions`；回归工具 `score.py --eef-min-p`（缺省 0.7）。
- **顺带修的**：mcap 话题转成本地视频时按 30 fps 编号，复核与意见片段用了转出视频的时间戳——非 30 fps 的 mcap 数据集上，片段的播放速度和
  「对应 episode 时间」都错；改为按视图自己的帧率计时（`observations.media_frames`）。是「mcap 与 LeRobot 读出相同」的用例在 5.0 下比 p 时发现的。
- **验收**（本机 Daemon + 假模型端点，与对账、回归工具的套件）：① d2 种子模式：ep0「一致」0.32；ep1（轨迹漂移）两路位置冲突 0.78 / 0.77，出卡；
  ep2（夹爪转 30°）「可能不一致」0.63；ep3（抖动）记录抖动 0.8；ep4（画面晃动）画面运动 0.8；ep5（时间差 5 帧）时间对齐单渠道封顶 0.8，另有一格位置冲突；
  ep6（外参错 3 cm）位置 0.8、诊断支持 `extrinsics_error`。冲突卡答「一致」作废、答「不一致」blocking 由 `tests/orchestr` 与 `tests/results` 的用例覆盖。
  ② d2 不给夹爪参考：全部是模型意见单渠道 0.76，无卡。③ das 内置标定：`00001`「一致」0.03（自运动单渠道），另一条推不出轨迹、判断不了；
  robot1 晚 0.5 s：自运动 0.96、封顶 0.8「不一致」，出 `ego_motion_suspect`。④ 对账黄金基线回放全过；注册表 4.0 / 4.2 / 4.4 的旧任务照常读出，
  旧的 `unsettled` 卡照旧待裁决。⑤ `score.py` 按「标签 + p」计 MV-4，对照表与注册表的一致性检查过。
  理想观察者假模型下的 d2 七条（`tests/cli/test_eef_dataset2.py`，需本机 DEMO 数据）：原样「一致」，六种故障都至少「可能不一致」，依据点名分项与相机。

### 9.2 F5.23 落地记录（2026-10-10）

- **契约**：C7 改为 `docs/contracts/dataset-declaration.schema.json`（`dataset-declaration/1.0`，示例 5 个合法、16 个不合法）；Schema 自包含，第一层的
  `$defs` 抄自 `viz-mapping.schema.json`（后者留一版，下次改契约时删）。语义层是 `semantics.{pose, joints, gripper, frames}`（位姿列或 topic、布局、单位、
  `frame_id`、参考系，与 `eef-mapping/1.1` 的 `eef` / `record` 块同名同义）；标定层是 `calibration.cameras.<来源>`（键是 LeRobot 的视频键或 mcap 的 topic）、
  `calibration.tool`、`calibration.handheld`（一份 `umi-calibration/2`）；外参三种：`static`（`xyz_rpy` 或 4×4）、`column`（逐行的位姿列）、`camera_tcp`（腕部）。
  C4 升 5.0.0：`GET` / `PUT /datasets/{id}/declaration`、`DatasetItem.declaration`；模版可以存整份声明。C2 预检的 EEF 条目加 `trajectory_source`（可选字段，仍是 1.0）。
  C1 升 5.1：`gripper_calibration`、`record_mapping` 标 `deprecated`（界面不再给，旧任务照用）。
- **存取**：声明就存在映射原来的列里（`Dataset.viz_mapping`，一份文档、一个版本号）；旧的 `viz-mapping/1.x` 读出来就是只有第一层的声明，
  mcap 读取器拿的是第一层的视图（`curation.declaration.mapping_of`）。`PUT /mapping` 只换第一层，其余两层不动。改声明后 Daemon 只重做预检
  （`refresh_preflight`，格式可读性变了才整套 `repreflight`），不重新列举文件。任务开跑时冻结进 `run.json["declaration"]` 与 `inputs/declaration.json`，
  各阶段命令带 `--declaration`。
- **起草**（`curation/declaration/draft.py`）：LeRobot / Lance 用登记时预检里的 `dataset.features`（不再读 info.json）；mcap 用探测，探测新增标定 topic 首条消息的内参
  （`TopicProbe.calibration`）。`GET` 的 `draft` 是草稿叠在已确认版本下面，任务从不用草稿；没有确认过的声明时，CLI 预检自己起草一份，只用来说缺什么
  （`declaration_unconfirmed` / `intrinsics_missing` …），从不拿它生成轨迹。
- **校验**（`curation/declaration/checks.py`）：硬错误定位到字段；可疑项记在声明的 `suspects`。三帧落点预览没有单做：由数据集级叠加（§5.1，F5.25）承担；
  位姿流与视频的配对率要读数据，确认时不算。
- **生成**（`extensions/eef_consistency/declared.py`）：LeRobot 走 `eef-mapping` 的导出（`Generated`，TOS 上把 `meta/` 与该条的数据文件拷到暂存目录，视频不读），
  mcap 机械臂读位姿 topic 与相机 topic（`GeneratedMcap`，以第一路能画的相机为帧，位姿与其他相机按日志时间就近配对）；都是逐条生成，落在
  `checks/eef_video_consistency/trajectory/episode_N.json`，叠加照读。Lance 不生成（EEF 读不了 Lance 的画面），数据集级叠加另说（F5.25）。
  导出的视频时间改按帧号算（`frame_index / fps`）：掉帧的数据集时间戳会跳，画面仍按帧号解码。生成的样本由平台命名，人给的种子按 sample_id 末尾的
  episode 号重排到运行目录（`declared.remap_seeds`），相机编号以声明里的 `camera_id` 为准。
- **记录比对**（§6.1）：轨迹由平台从记录生成时只比数据集自己的两份记录（位姿列对关节角正解，`source: internal`）；只有一份记录时写
  `record_single_source`「轨迹来自数据集记录，无需比对」；上传或数据集自带的轨迹照旧比上传件对记录（`source: upload`）。
- **界面**：数据集详情加「数据集声明」卡片与抽屉（来源与用途 / 记录的含义 / 相机标定 / 工具 / 手持夹爪 / 时间六页，每项「声明 / 按假设值」与起草时的假设，
  按表格内容即时说「平台按声明生成轨迹」或还缺什么），从 JSON 导入、导出、套用 / 另存模版；列表在格式下写「声明：第 N 版」。上传种类
  `dataset_declaration` 没有加：导入在浏览器里读 JSON，`PUT` 直接收文档；`eef_gripper_calibration`、`eef_record_mapping` 两个上传种类停用但仍收一版。
- **验收**：① dataset2 起草出两路 `fixed_external`、位姿 / 关节 / 外参列，内参留空、工具按 Franka 预填并标假设；补内参、把工具改成 0.16 m / 0.085 m 后
  `trajectory_source=generate`，生成的轨迹与 `dataset2/trajectory.json` 逐帧投影最大差 0.005 px（28126 个点，`tests/eef/test_declaration.py` 的 DEMO 用例）；
  按声明跑 `check`：ep0「一致」0.32，ep6 位置 0.8、诊断支持 `extrinsics_error`，与上传轨迹的结果相同。② das 起草出两路 `wrist`、归 `robot0` / `robot1`，
  手持夹爪用内置 DEMO 标定、各项 `model_assumed`。③ 改声明后旧任务读冻结的版本（`tests/viz/test_declaration.py`）。④ `viz-mapping/1.1` 文件照读、照存。

### 9.3 F5.24a 落地记录（2026-10-10）

- **契约**：C1 升 5.2：EEF 的 `needs` 去掉 `vlm`，新参数 `use_vlm`（布尔，缺省开），模块条目多 `vlm_switch: "use_vlm"`（`ModuleSpec.asks_model(params)`：
  要模型的模块恒为真，有开关的看开关）；夹爪参考两项加 `x-applies-when: third_person_camera`；几段参数说明去掉了已失效的「参与判决」与内部编号。
  C2 预检的模块条目加 `cameras[]`（`source`、`camera_id`、`mount`、`owner`、`drawable`、`reason`）与 `applicable_params`（仍是 1.0）。C4 升 5.1.0
  （`vlm_switch`、中英变更记录）。C7 只在假设代码的说明里补了 `mount_declared_fixed`。
- **预检**（`cli/preflight.py::_eef_entry`）：`cameras[]` 按来源取——按声明判断的（生成或缺项）用 `declared.readiness` 的相机状态，原始手持 mcap
  是逐只手的腕部相机（归属按名字），其余读轨迹包（`eef_preflight.bundle_cameras`）；读不到时为空，空表示「不知道」，这时夹爪参考照常给。
  没选后端时 EEF 条目照常 `available`，`notes` 末尾一条 `vlm_backend_missing: …`；`use_vlm=false` 不提醒；关掉 VLM 又没给夹爪参考、有第三视角相机时
  另加一条「第三视角相机不测」。顺手修了预检里一个变量遮蔽：前一个模块缺输入时，循环里的 `args` 被改成了一个字典，后面 EEF 读不到任务参数与声明
  （mcap 数据集常见：缺状态列的模块排在前面）。
- **CLI**（`check`）：新开关 `--no-vlm`——这次调用没有模型后端（Daemon 在任务没选模型时传）；带上它而所选模块里有 `task_success` 时是用法错误。
  EEF 在 `use_vlm=false` 或 `--no-vlm` 时不开 VLM 会话、不发请求：复核与整段意见都不做，意见块 `status: not_asked` 带原因；合并时模型一路记
  `vlm_off` / `no_vlm_backend`，记录的形状与开着时相同（`merged` 的 `tracking` 只在模型复核过时才有）。`planner` 把有 `vlm_switch` 的模块所在段仍定为
  `vlm` 段（闸门照给）；Daemon 起这一段时按 `Run.stage_asks_model` 决定传 `--vlm-*` 还是 `--no-vlm`（三处：常驻 worker、批次、整段）。
  `prechecks.needs_vlm` / `task_needs_vlm` 与建任务时的检查按「需要模型，或开关开着且任务选了模型」算，没选模型的 EEF 任务能建能跑。
- **「视为固定」**（§3.3，验收 ⑤）：声明抽屉里「会动」的相机多一个「视为固定」按钮，改成 `fixed_external`、`model_assumed`、假设 `mount_declared_fixed`；
  也可以直接在下拉里改，假设跟着加减。生成的轨迹来源带 `declared_fixed`（相机编号），报告 EEF 小节的 `declared_fixed_cameras` 列出它们，
  控制台小节末尾写「按声明视为固定的相机：…（它真动起来时画面运动分项会报）」，Episode 明细的 EEF 区块也写。数据集级的标定可疑（F5.26）对这类相机不另作处理。
- **界面**：第二屏按 `applicable_params` 给参数，trajectory.json 按 `trajectory_source` 分三种写法（必填 / 先补声明、上传折叠 / 可选并说轨迹从哪来），
  「补完后重新预检」按钮直接重跑预检；模型配置在只有「可用模型辅助」的模块时选填，可清空；EEF 卡片标「可用模型辅助」。Episode 明细的 EEF 区块
  对按声明生成的轨迹写「轨迹由平台按数据集声明（第 N 版）生成」与按假设值的字段，模型没问时写「没有问模型（原因）：这一条只有 CPU 的测量」。
- **验收**（本机 `curator-daemon-eef`，dataset2 = `eef_ds2_lr3`，das = `umi_das`）：① 把 d2 声明的两路内参清空（第 2 版）后第二屏出「数据集声明还缺：
  相机内参（exterior_1_left）、相机内参（exterior_2_left）、至少一路能画的相机」与「到数据集页补声明」，上传框折叠；恢复（第 3 版）后点「补完后重新预检」，
  轨迹一项写「轨迹由平台按数据集声明生成，不用上传」，夹爪参考在。② das 的预检 `mcap_derive`、两路腕部相机归 `robot0` / `robot1`，`applicable_params`
  里没有夹爪参考。③ 只有图片的 LeRobot 数据集 EEF `unsupported: missing_input`（`video_cause: none_declared`，`tests/cli/test_preflight.py`）。
  ④ 第一屏清空后端建的 d2 任务（没给种子）7 条全「判断不了：没给夹爪参考，模型也没问（没有模型后端）」，没有模型请求；给种子不选模型：
  62 格单渠道 `no_vlm_backend`，ep0「一致 0.32」与 F5.23 有模型时的读数相同；开着 `task_success`、EEF 关掉 VLM：EEF 0 次请求、`vlm_off`，任务成败照常 14 次。
  ⑤ d2 的 `exterior_2_left` 记 `mount_declared_fixed` 跑 ep0、ep6：两条都按第三视角测，报告小节写「按声明视为固定的相机：28221883_left」。
  不勾 EEF 的任务判决不变：本 feature 没有动判决与非 EEF 模块的代码路径（`--no-vlm` 只在有 `vlm_switch` 的段上传）。

## 10. 开工时的缺省选择（实测后可改）

| 项 | 缺省 | 为什么 |
|---|---|---|
| 数据集级生成的时机 | 按需生成、按声明版本缓存；数据集预检成功后 Daemon 在后台预生成前 3 条 | 预检刚读过元数据，热；全量预生成对 TOS 上的大数据集太重；CLI 不写 Daemon 的缓存 |
| 回归样本集 | F5.22 里把 `score.py` 的 MV-4 改成按「标签 + p」打分；EEF 注入条目不够的记成缺口 | 打分口径随输出口径走；造样本是设计 16 的事 |
| 记录比对的去留 | 继续挂在 EEF 下：内部一致性（位姿列 vs 关节角正解）作为 EEF 的一个只报告的分项 | 少动一处；独立成模块等有第二个用户再说 |
| 术语 | 界面上「夹爪参考」暂不改名；每条的输出叫「结论 · 置信度 · 依据」，模块级的统称仍叫「意见」（报告小节标题「EEF–视频一致性（意见）」）；「判断不了」= 有输入但没有渠道给出 p，「不支持」= 预检就知道做不了 | 两个词分别对应运行时与预检，不混用 |
| VLM 意见的封顶 | 与其他单渠道同为 0.8 | 先不给模型单独的折扣，F5.22 实测后再定 |
| 双臂 / 两手共视 | 不在本篇；声明里腕部相机的归属字段为它留位 | 要扩 EEF 格式的多工具表达，另立设计 |

## 11. 评审记录

### 第一轮（2026-10-09 晚，Yichen）

| 问 | 答 | 落到哪 |
|---|---|---|
| 档位与封顶的 demo 值 | 高 0.7、低 0.4、单渠道封顶 0.8、冲突 = 一边到高档一边在低档以下 | §7.1、§7.2，进 demo profile |
| 旧任务 | 不管；被旧版判废的条目不自动复议，报告提示「按旧口径」 | §7.3 |
| 声明的载体 | 就是今天的 mcap 配置（C7）：第一层原样，加语义与标定两层，并推广到 LeRobot / Lance；一份文档一个版本 | §3.1 |
| 内置机型的缺省值 | 不是常数：工具模型按「机器人 + 夹爪」随数据集声明，与相机无关；`robot_type` 只用来预填并标假设 | §3.2、§3.3 |
| 夹爪参考的构建工具 | 暂不做；后果接受：第三视角机械臂数据没有参考时只有 VLM 单渠道、最高 0.8 | 不在本篇；将来另立 |
| VLM 开关缺省与没有后端 | 缺省开；没有后端允许建任务，预检报缺输入，运行时模型渠道按「缺」处理 | D84、§4.3 |
| 头部 / 会动的相机 | 缺省 `moving` 不支持；用户可在声明里改成「视为固定」并标假设 | §3.3 |
| 与设计 23 的关系 | 合成一步：EEF 的拆两半（D76、D77 中 EEF 的部分）并入 F5.24，CPU 的活都在 CPU 块 | D84、§8、§9 |

### 第二轮（2026-10-09 晚，Yichen）

| 问 | 答 | 落到哪 |
|---|---|---|
| 声明用一份文档还是两份 | 一份，简单 | §3.1：C7 改名 `dataset-declaration/1.0`，`Dataset.declaration` |
| 文档定稿 | v1.0 开工稿，没问题就按它开工 | §10 改为开工时的缺省选择 |

### 第三轮（2026-10-09 夜，对照代码核实）

| 发现 | 定为 | 落到哪 |
|---|---|---|
| 「旧任务不管」做不到：`run.json` 只冻结策略的预设与规则，缺省级别每次 aggregate 取当前注册表，旧任务再出结果版本时旧的 EEF 判废会被放回 | 5.0 起冻结所选模块的缺省级别；旧运行按 `registry_version` 用旧缺省表；停用细码留在注册表标 `retired` | §7.3、§8，F5.22 |
| 「没后端照常建任务、预检报 needs_input 作提醒」与现有语义冲突（Daemon 对 vlm 的 needs_input 直接拒；注册表 `needs` 含 `vlm`） | EEF 的 `needs` 去掉 `vlm`；提醒用 `available` + `notes` | D84、§4.3，F5.24a |
| 发现的粒度没写 | 每个达到「可能不一致」的分项 × 相机一条 `inconsistent`，冲突的格只出 `conflict`；条目级标签写 `details.merged.episode` | §7.1 |
| （开工后）「`issue` → p = 置信度、`ok` → p = 1 − 置信度」：CPU 刚过开阈值的可疑得 p = 0（「一致」），只问到一半片段的「没发现」得 0.5（「可能不一致」） | 与档位对齐：`issue` → 低档 + (1 − 低档)c，`ok` → 低档 × (1 − c)，按任务的低档算；取大、封顶、冲突规则不变。先试过以 0.5 为中点（`issue` → 0.5 + 0.5c，`ok` → 0.5 − 0.5c），dataset2 上覆盖不全的「没发现」（c ≈ 0.2）还是 0.4 多、被标成「可能不一致」 | §6，F5.22 |
| （开工后）可复议也取当前注册表：旧任务的 EEF 判废冻结了级别却不能复议了 | 可复议的细码一并冻结（`policy.appealable`），旧运行按旧表 | §7.3，F5.22 |
| VLM 复核在支持多或平票时怎么算 | 按票差符号定 `issue` / `ok`，平票或无票 `cannot_tell`；置信度 = \|票差\| / 有答复窗口 × 有答复占比 | §6 |
| 冲突与「一致」的边界不一致（≤ 与 <） | 冲突 = 一个 ≥ 高档下限、另一个 < 低档上限 | §7.1 |
| 「缺声明不出上传框、也允许上传绕过」自相矛盾 | 提示 + 链接，上传框折叠在「改为上传 trajectory.json」 | §4.3 |
| 预检里预生成叠加会让 CLI 写 Daemon 的缓存 | 预检成功后 Daemon 后台预生成 | §5.1、§10 |
| F5.23 验收①按 Franka Hand 预填过不了（dataset2 是 DROID 的 Robotiq 2F-85） | 验收写成「改成 dataset2 的工具值后」；`robot_type` 认不出夹爪时预填标假设 | §9 |
| F5.24 的拆两半依赖设计 23 还没落地的段基础设施 | 拆成 F5.24a（第二屏、开关）与 F5.24b（拆两半） | §0、§9 |
| 机械臂轨迹生成器只读本地 LeRobot；跨 episode 升数据集级的诊断条件没实现 | 算进 F5.23、F5.26 的工作量 | §9 |
| 核实无误 | `eef_check` 的「不一致 → blocking（kind human）」已在 `verdicts.py`；对账黄金基线无 EEF 运行；`run.json` 有 `registry_version`；dataset2 有 `camera_extrinsics.*` 列 | — |

## 附录 A · 术语对照

| 本篇 | 今天的叫法 | 含义 |
|---|---|---|
| 声明 | 字段映射（C7）、`mapping.yaml`、`record_mapping`、`gripper_calibration` | 数据集级、版本化的「来源→角色 + 语义 + 标定」 |
| 轨迹来源 | `trajectory_source`（F5.20 起） | 上传 / 生成 / 自带 / 会话 / mcap 推导 / 缺声明 / 缺位姿 |
| 夹爪参考 | 观测种子 / 夹爪外观模板 | CPU 跟踪器的锚点来源，只对第三视角相机有意义 |
| 渠道 | CPU 测量、模型复核、模型意见、自运动 | 各自出「结论 + 置信度」的来源 |
| 不一致置信度 p | — | 渠道结论的统一尺度，合并取大 |
| 冲突 | 转人工（部分） | 两个渠道结论相反、都不低；唯一出裁决卡的情形 |
| 判断不了 | 弃权 / 无法评估 | 没有任何渠道给出 p；进覆盖率，不出卡 |
