# 24 · EEF 的轨迹由平台生成，用户不上传 trajectory.json

> 状态：**已落地（2026-10-09）**。来源：需求方 2026-10-09——「我是用户，我不管 trajectory.json」「就是 EEF 任务的时候生成这个文件，
> 并且前端也不能要求有这个文件」。注册表 4.3。
>
> **修订（2026-10-09 晚，Yichen Wang）**：轨迹**能算就可选、算不了就必选**。原先「都没有」时报 `unsupported`，模块在新建任务里根本勾选不了，
> 改为 `needs_input`：模块照常可勾选，第二屏要求上传 trajectory.json（§1 末段、§3）。原始手持夹爪的 mcap 也能算（设计 22 §5.4，D80）。

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
3. 数据集根目录自带的 `trajectory.json`；
4. 原始会话（§6）从会话算；原始手持夹爪的 mcap（内置 UMI 模版认出的布局）由设计 22 §5.4 逐条从录制推出（D80）。

都没有（或生成出错）：trajectory.json 就是必选——预检里 EEF 是 `needs_input`（`trajectory_missing`，`input_hint.field = trajectory_json`，
原因写明平台算不出轨迹、要上传），模块照常可勾选，控制台第二屏把 trajectory.json 标为必填，不传时 Daemon 也拒绝建任务；命令行直接跑时
模块失败并写明要传 `--param …trajectory_json=PATH`。（修订前这里是 `unsupported`，模块勾选不了。）

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
| CLI `preflight` | 不传轨迹时按 §1 判断：能生成或有自带文件 → 生成到临时文件校验，`notes` 写明来源；原始手持夹爪 mcap → 设计 22 §5.4；都没有或生成出错 → `needs_input`（要上传；修订前是 `unsupported`） |
| CLI `check`（`eef_check`） | 不传轨迹时生成到运行目录再读 |
| Daemon `eef-overlay` | 任务没有上传件时读运行目录里生成的那份（工作目录被清过就先从交付恢复） |
| 前端 | 上传框随参数 Schema 变为可选；预检要求上传时（`needs_input` 指名 `trajectory_json`）第二屏把它标为必填（`lib/preflight.moduleParamFields`）；模拟数据里手持夹爪数据集 `available`，其余 `needs_input` |

## 4. 原始会话的标定从会话里取（2026-10-09）

`export-umi` 不再要求手写的 `calibration.json`（`--calibration` 可省；`adapters/umi.derive_calibration`）。`dataset_plan.pkl`
里没有标定，三样东西分别取自会话：

| 要的 | 取自 |
|---|---|
| `T_world_slam` | `demos/mapping_*/tx_slam_tag.json` 的逆 |
| 内参与鱼眼畸变 | 依次：会话里的 `*intrinsics*.json`（UMI / TRUMI 流水线的输入，按图像尺寸匹配）→ demo 目录 `slam_stdout.txt` 里 ORB-SLAM3 打印的 Kannala-Brandt 参数 → 按图像尺寸的内置标定（`adapters/defaults/`，目前只有 TRUMI 的 GoPro 13 2.7K 4:3） |
| 图像尺寸 | 原视频 |
| `T_camera_tcp` | 逐帧 inverse(相机位姿，CSV 经 `tx_slam_tag` 换到标签系) × plan 的 TCP 位姿；plan 的 TCP 本来就是相机位姿乘固定安装变换算出来的，所以整段应是一个常数，偏差超过 1 mm / 0.05° 就报错 |

实测 cups（内参取自 SLAM 日志）与 Trossen（会话里没有日志，用内置 GoPro 13 内参）推导出的标定与手写的逐项一致（`T_camera_tcp` 误差 0）。
任务文本（`--instruction`）会话里没有，不给时为空，任务成败判定照 D72 不判这些条目。

## 5. ~~原始会话转成 LeRobot~~（2026-10-09 改做 §6）

`curation stage-umi`（把原始会话转码成 LeRobot 放在本地，C2 `stage-umi.schema.json`）留作离线工具；需求方算过 100 GB 的账（整读一遍、
全量转码十几个小时、多存一成）后定：平台**直接读原始会话**，只把轨迹算出来（§6）。

## 6. 平台直接读原始 UMI 会话（只做 EEF）

- **格式**：预检认出 `umi_session`（根目录 `dataset_plan.pkl` + `demos/`），`supported: true`；数据集摘要来自 plan（条数、相机数、帧率、
  `umi_*` 机型）；只有「EEF–视频一致性」可用，其余模块 `unsupported`（`format_unsupported_by_module`，「原始会话没有 LeRobot 的数据列与任务文本」）。
- **轨迹**（`adapters/umi.session_bundle`、`derive.session`）：标定按 §4 从会话取；手位姿与开口取自 plan；相机位姿 = 手位姿 × inverse(`T_camera_tcp`)；
  **视频不转码**，每路相机的媒体就是 demo 自己的 `raw_video.mp4`（2.7K 原图，`H` 为单位阵），片段是 plan 的帧区间、按 plan 的抽帧步长取帧——
  片段起点比第一帧晚 (步长 − 1) / 2 个原始帧，解码器按 `round(t × fps)` 编号、每个编号取第一帧，正好取到 plan 用的那些帧（cups 实测与导出时
  用的帧逐一对上）。远端会话只拷小文件（plan、CSV、SLAM 日志、标签与开口标定、内参文件），视频只用数据集自己的密钥按范围读头部。
- **读视频**：EEF 跑在 `vlm` 段，经 S1 的块缓存只读这条 episode 用到的块（Trossen 实测每条约 18–21 MB）。
- **其他配合**：`snapshot` 记下 plan 与所用 demo 的视频、CSV、日志、标定（源守卫据此核对）；`meta_fingerprint` 只看 `dataset_plan.pkl`
  （CLI 与 Daemon 同一规则）；开跑前的「输入可读」检查认 `dataset_plan.pkl`；Daemon 的可跑格式加 `umi_session`；`aggregate` 读任务文本时
  原始会话返回空。
- **实测**：TOS 上 Trossen 原始会话，只给地址、只勾 EEF，经 Task API 跑完：2 条通过，模型 2 次请求，报告与投影叠加接口正常。
- **可视化**（`daemon/viz/umi_session.py`，C4 4.5.0）：相机是各 demo 的原视频按 episode 的时间窗截取（GoPro HEVC 浏览器放不了，看的时候
  由 Daemon 现转 H.264，只转这条的时间窗，缓存在本地盘，D60）；曲线是每只手的 TCP 位置与开口（取自 plan）；迷你播放器的投影叠加按原视频文件
  对上可视化的相机。
- **还不支持**：EEF 以外的模块。
