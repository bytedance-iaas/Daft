> 本文件是 EEF–视频一致性模块的输入格式规范，随 12 篇设计文档进仓库（2026-09-22），F5.1（2026-09-23）冻结在 `docs/contracts/eef/`，五份 Schema 与本文件同目录、受 `CONTRACTS.lock` 保护；合法与不合法的最小示例在 `docs/contracts/examples/eef-*.json`。原件与六组可打开的样例、参考校验器在 `~/ws/ws_general/galbot/eef_video_consistency/`；DEMO 上传件的实例是 `~/ws/ws_general/galbot/dataset2/trajectory.json`。平台的读取与校验实现在 `backend/curation/extensions/eef_consistency/load.py`。

# EEF–视频一致性统一格式 1.0

版本：`eef-video/1.0.0`。本文件中的“必须”描述新格式的接入要求；不表示源数据已经满足，也不代表现有平台已经实现。

## 1. 范围和文件边界

数据组织以 dataset2 的 `eef / gripper / cameras` 为骨架，吸收 dataset1 的明确坐标与时间定义，以客户 `sample.json` 的样本入口为兼容形态。兼容是通过适配器进行语义转换，不承诺客户原 JSON 可被新读取器原样读取。

```text
dataset/
├── lerobot_v2/                   保留原生数据；版本读取 meta/info.json
├── eef_consistency/
│   └── samples/<sample_id>/
│       ├── sample.json
│       ├── frames.jsonl
│       ├── calibration.json      可空；每样本解析完毕的有效标定
│       └── raw_pose_sequence.json 可选，未解释的源序列
└── evaluation/                  仅离线评估器可见，输入清单不引用
```

本交付的样例为方便阅读放在 `examples/`。文件名由 `sample.json` 引用决定；批量产品可改成 `annotations/episode_XXXXXX.jsonl`，语义相同。

Schema 使用 [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12)。四份 Schema 均自包含，不需要联网解析外部 `$ref`。对象默认不接受额外字段，避免拼写错误被静默忽略。增加字段需修改 Schema 与版本；破坏已有语义时提升主版本。迁移器保留旧版本，不覆盖源文件。

## 2. 两种可检查输入和两种保留形态

| 形态 | 画面 | eef | 投影／标定 | 可用能力 |
|---|---|---|---|---|
| 二维投影输入 | 视频 | 可空 | 提供投影；标定可空 | 相同物理点的二维位置、可观测方向、局部时序 |
| 三维几何输入 | 视频 | 明确的绝对位姿，或可恢复绝对位姿 | 相机标定；投影可由平台产生 | 上述能力＋数值轨迹检查＋投影自洽检查 |
| 未知语义序列 | 图片或视频可缺失 | `null` | 原始序列存旁路文件 | 保留数据，待补足定义，不进行绝对投影 |
| 纯图像 | 图片 | `null` | 可空 | 其他图像质检可使用，EEF 一致性 `unsupported` |

是否具备某项能力由 preflight 从真实输入推导，不接受由客户填写一个 `can_check=true` 来跳过验证。单张图最多支持该帧的空间比较，不能产生时序或轨迹抖动结论。

## 3. sample.json：样本入口

Schema：[sample.schema.json](sample.schema.json)。完整实例见参考设计的 `examples/dataset2_000006/sample.json`（仓库外）。

| 字段 | 说明 |
|---|---|
| `schema_version` | 固定 `eef-video/1.0.0` |
| `sample_id` | 稳定、不包含故障类别的 ID；跨运行不可按遍历顺序重新分配 |
| `source` | 数据集名、源 episode ID、可空的任务文字；仅作溯源 |
| `frame_count` | JSONL 行数，指统一样本时序索引；不等于每个视图实际媒体帧数 |
| `timebase` | `video_pts`：有视频局部时间；`index_only`：仅有序列次序，不知道真实时间 |
| `annotations_path` | 逐帧文件路径，相对于本 `sample.json` |
| `calibration_path` | 标定文件路径或 `null` |
| `eef_frame` / `reference_frame` | 如 `panda_link8` / `robot_base`；未知则 `null` |
| `views` | 显式列出所有相机、普通图片、位姿可视化及其媒体 |
| `point_definitions` | 按点 ID 声明物理意义、几何模型与来源 |
| `axis_definitions` | 按方向 ID 声明起点、终点、物理意义、有向性和轴长 |
| `raw_pose_sequence` | `{path, semantics:"unresolved", unresolved_fields}` 或 `null` |
| `notes` | 接入说明；不传给视觉定位或 VLM 判定提示词 |

### 3.1 视图与媒体

每个 view 包含 `view_id`、`kind`、`camera_id`、`mount`、`media`。

- `kind=camera`：独立相机画面，必须有相机 ID。
- `kind=pose_visualization`：由运动记录画出的图，只供展示，禁止作为独立视觉观测。
- `kind=image`：无法确认相机身份的一般图像。
- `mount` 为 `fixed_external/wrist/moving/unknown/not_applicable`。固定外部相机是首版重点；移动相机不能默认静止。

媒体字段：`kind=video|image`、`uri`、`image_size_wh=[宽,高]`、`frame_count`、`fps`、`clip_start_s`、`clip_end_s`。

相对路径均以样本目录为基准。本地工具支持文件路径；后续产品的远程 URI 由已有存储适配器读取。不要把原 Redis／开发机路径直接当作可访问媒体。

视频区间采用半开区间 `[clip_start_s, clip_end_s)`，时间在原媒体解码器的 PTS 时间线上。逐帧记录的 `video_timestamp_s` 为片段局部时间，因此原文件定位时间为 `clip_start_s + video_timestamp_s`。视频帧号也以片段第一帧为 0。

VFR 视频 `fps` 可以为空，有效时间必须来自逐帧 PTS；CFR 只有在验证过重编码时间线后才可用 `i/fps`。`frame_count` 指可解码的片段帧数，不由小数时长直接四舍五入得到。

图片 `frame_count=1`、`fps=null`、`clip_start_s=0`、`clip_end_s=null`。客户 DROID 的 65 步位姿与一张首帧图不能伪装成 65 帧视频：总记录 65 行，但图像 view 仍只有一帧。

**mcap 数据集（2026-09-24 增补，F5.13，可选字段，现有文件不受影响）**：mcap 数据集没有视频文件，一路相机是该 episode 的 `.mcap`
文件里的一个图像 topic。这时 `media` 多一个 `topic`（以 `/` 开头，如 `/observation.images.exterior`），`uri` 指向 `.mcap` 文件
（`media_uri_base=lerobot_root` 时相对数据集根目录，即 `episode_<N>.mcap`），`kind=video`、`clip_start_s=0`、`clip_end_s=null`；
`frame_count` 是该 topic 的帧数，`fps` 可给标称值或留空。帧号按该 topic 的消息 `log_time` 顺序从 0 数：`video_frame_index=i`
就是第 i 条图像消息（H.264 流按解码出的第 i 帧）。读取支持 JPEG（`CompressedImage`）与 H.264 Annex-B（`CompressedVideo`），
与漏斗其他模块读 mcap 的方式相同，不重编码；raw Image 等其它编码不支持。时间仍以逐帧 `timestamp_s` 为准，不看消息时间。

### 3.2 点与朝向

点定义使用以下模型：

| model | 定义 | 用途 |
|---|---|---|
| `fixed` | `position_eef_m`，表达在 `eef_frame` 中 | EEF 原点、固定 TCP、固定轴端点 |
| `linear_gripper` | `position_open_eef_m` 与 `position_closed_eef_m` | 开合引起的位置变化，`p(g)=(1-g)p_open+g p_closed` |
| `external_2d` | 不提供三维点，三个位置字段均 `null` | 客户已经提供二维点；仍须写清物理意义 |

`g` 来自当前帧 `closed_fraction`；未知时不可推算夹指点。复杂非线性工具模型不在 1.0 内，需适配器预计算投影或以后扩展版本，不能在 JSON 中放可执行表达式。

`provenance={source,method,assurance}` 中 `assurance` 为 `declared/model_assumed/independently_calibrated/unknown/synthetic`。厂商／数据集声明与独立验证是两个等级。来源文件即使把字段命名为 GT，也不能自动升级成视觉真值。

坐标轴由 `start_point_id` 与 `end_point_id` 确定。`length_m` 指三维定义中的轴长，未知时为空。dataset1 从 EEF 原点画 8 cm，dataset2 从模型 TCP 画 6 cm，必须分别保存。

`directed=true` 表示可区分正负方向，比较范围 0–180°；`directed=false` 表示没有端点身份的线方向，比较范围 0–90°。两指连线通常先按无向线处理。只有独立识别出手指身份时，才允许把它当有向线。RGB 颜色不能确定哪个轴是接近方向。

## 4. frames.jsonl：逐帧数据

Schema：[frame.schema.json](frame.schema.json)。一行一个完整 JSON，不允许 NaN／Infinity；不能用零位置代替缺失值。

| 字段 | 必须表达的内容 |
|---|---|
| `sample_id` / `frame_index` | 与样本对应；行号为连续的 `0..frame_count-1` |
| `timestamp_s` | 主样本的局部视频时间；`index_only` 时必须 `null` |
| `source_state_index` | 可追溯的源状态行号；未知或无法无泄漏确定时 `null` |
| `source_timing` | 独立保留源机器人／各相机的原始时间值与时钟语义 |
| `eef` | 已解释的实际 EEF 位姿，或 `null` |
| `gripper` | 已解释的夹爪状态，或 `null` |
| `cameras` | 相机 ID → 本帧媒体映射、标定引用和待检投影 |

这是主时间线上的配对记录，不强制原机器人采样率与所有相机采样率相同。允许各路映射到不同相机帧，或者缺少一路。禁止按 JSONL 行号去跨数据集对齐。

### 4.1 位姿与相对轨迹

`eef` 字段包含 `pose_type`、`frame_id`、`reference_frame`、`position_m`、`quaternion_xyzw`、`relative_to` 和 `provenance`。

本格式采用右手坐标系、列向量、主动旋转；`T_A_B` 表示将 B 中的坐标变换到 A。绝对位姿是 `T_reference_eef`。四元数存 `(x,y,z,w)`，规范化为单位长度；零四元数是非法输入。处理差分前可选择连续符号，但必须保留原输入供追溯；`q` 与 `−q` 不能判成旋转跳变。[SciPy 四元数约定](https://docs.scipy.org/doc/scipy/reference/generated/scipy.spatial.transform.Rotation.from_quat.html)

绝对位姿：`pose_type=absolute`，`relative_to=null`。

已知相对起点位姿：

```text
pose_type = relative_to_start
reference_frame = eef_at_start
relative_to.frame_index = 0
relative_to.convention = inverse_T0_times_Tt
ΔT_t = inverse(T_reference_eef(0)) × T_reference_eef(t)
T_reference_eef(t) = anchor_pose × ΔT_t
```

1.0 只接受这一种明确的相对定义。`anchor_pose` 存绝对起点的位置、四元数和参考系；可为空，但为空时不能恢复绝对投影。首步 ΔT 应为单位变换。相邻帧增量应先累乘成此约定；世界坐标平移差与本约定不同，必须显式转换。

单位、归一化、参考系或变换顺序未知时，`eef=null`，序列原样放入 `raw_pose_sequence`。客户的 `bbox_half_size=1.0` 不能证明单位就是米；首帧为零也不能证明是 `inverse(T0)×Tt`。

### 4.2 时间

每条 `source_timing` 为 `{channel,timestamp,unit,clock_id,semantics}`。`timestamp` 用十进制字符串保留精度，`unit=s|ms|us|ns`。例如毫秒值保存为 `"1704137533708"`，不要先转成低精度浮点再导出。

`clock_id` 相同表示来源声明同一时间域，不自动证明两设备已经完成硬件同步；`semantics` 区分曝光估计、读取开始、消息接收等。跨时钟必须有经过确认的映射才能插值；未知时只报告 `clock_alignment_unknown`。

保留原视频与导出视频两种时间的意义。反复使用同一源状态的重采样帧允许重复 `source_state_index`，但主视频时间必须严格递增；源时间不能因此被覆盖成 `i/fps`。

静止填充、变速、补帧、跨缺口插值需要单独的数据准备记录。不要通过异常答案中的 `source_step` 来恢复“正确配对”，这会消除要检测的不同步问题。

### 4.3 夹爪

`closed_fraction` 统一为 0 全开、1 全闭，源定义相反时由适配器反转；语义未知则为空。`opening_m` 可空；只有有依据的实际宽度或明确工具模型才填写，并在 provenance 标明是否 `model_assumed`。不能把任意 gripper scalar 直接映射为米。

### 4.4 每路相机

| 字段 | 说明 |
|---|---|
| `video_frame_index` / `video_timestamp_s` | 当前片段的媒体帧号与局部 PTS；无配对帧时均为空 |
| `image_size_wh` | 待比较媒体的实际尺寸，与 view 一致 |
| `calibration_id` | 当前样本 calibration 文件中的 ID；二维输入可空 |
| `T_reference_camera` | 动态相机的逐帧外参；静态相机时为空 |
| `H_media_from_calibration` | 标定图像坐标到当前媒体像素的 3×3 变换，没有变换为单位矩阵 |
| `projection` | `{source:provided|recomputed,pixel_space:media,points:{...}}` 或空 |

`points` 的 key 必须出现在样本 `point_definitions`。每个点独立保存：

```json
{"uv_px":[501.54,339.77],"depth_m":0.6209,"status":"valid","in_frame":true}
```

- `valid`：像素有限，正深度（若已知），且 `0≤u<W, 0≤v<H`。
- `out_of_frame`：保留有限的出画像素；`in_frame=false`，可用于诊断整体偏移。
- `behind_camera`：深度 ≤0，`uv_px=null`、`in_frame=false`，避免无意义除法。
- `unknown`：缺失或尚未确认，`in_frame=null`。

二维输入的深度可以为空。落在图像内不代表可见；遮挡与可见性只能来自独立观测。某个端点出画不应自动抹去其余有效点。

## 5. calibration.json：已解析的声明标定

Schema：[calibration.schema.json](calibration.schema.json)。顶层为 `schema_version` 和按 `calibration_id` 索引的 `calibrations`。

每份标定记录相机 ID、参考系、标定分辨率、`K`、图像空间、畸变模型、外参模式与 provenance。`K` 为普通 3×3 矩阵，不能存为容易误读的 `[fx,cx,fy,cy]`。

| model / image_space | 系数及处理 |
|---|---|
| `pinhole / rectified` | `distortion_coefficients=[]`；视频已校正，不重复去畸变 |
| `opencv_brown / distorted` | 1.0 只支持 5 项 `[k1,k2,p1,p2,k3]` |
| `opencv_fisheye / distorted` | 4 项 `[k1,k2,k3,k4]`，使用对应鱼眼投影函数 |

有更多系数的原模型不应截断后悄悄使用：需先正确校正媒体并提供新 K，或扩展版本。Schema 可表达后两种模型；本次数值校验脚本仅实现 pinhole 交叉检查。

`extrinsics_mode=static` 时 `T_reference_camera` 存于标定文件；`per_frame` 时该值为空，由每帧提供。单个声明矩阵不要同时在静态与动态两处覆盖。

用于固定相机的投影链：

```text
p_reference = T_reference_eef × p_eef
p_camera = inverse(T_reference_camera) × p_reference
uv_calibration = project(K, distortion_model, p_camera)
uv_media = normalize_homogeneous(H_media_from_calibration × [u,v,1])
```

针孔投影与外参方向依据 [OpenCV 标定文档](https://docs.opencv.org/4.13.0/d9/d0c/group__calib3d.html)。缩放、裁剪、补边必须记录到 H；不能一边修改 K，一边再应用同一次缩放。去畸变是非线性处理，不能假装是一个 H。

H 表达已知的数据预处理，不包含从故障答案拿到的画面抖动补偿。客户声明错误的外参仍是待检输入；适配器不能用另一份正确标定替换它。每样本输出“已解析的有效声明”，避免公共配置和 episode override 的优先级不明确。

## 6. 独立视觉观测

Schema：[observation.schema.json](observation.schema.json)。运行后保存到 `observations/<sample_id>/<camera_id>.jsonl`，不回写输入 `frames.jsonl`。

观察记录包含：sample/frame/camera/media 帧对应、`pixel_space=media`、`method`、模型或标注协议版本、原始输入图像 hash、`projection_visible_to_localizer=false`，以及按物理点 ID 索引的点。

每个观测点为 `{uv_px, visibility, confidence, uncertainty_px}`。`visibility` 为 `visible/occluded/out_of_frame/uncertain`；缺少定位时 uv 为空。`confidence` 是方法自身置信度，未经标定不能视为真实正确概率。`uncertainty_px` 未测量时为空。

本协议的独立观测定位不显示待检投影。VLM 异常复核可以同时查看原图与叠加图，但复核意见写到 review 文件，不能混进此观测流。`synthetic_fixture` 只用于测试，禁止用于视觉精度验收。

## 7. Schema 之外必须做的语义校验

JSON Schema 检查类型与枚举，不能证明以下事项，生产读取器还必须检查：

1. sample、相机、点、轴、标定引用一致；行数和时间映射完整；媒体实际可解码。
2. 单位四元数、旋转矩阵正交且行列式 +1、齐次矩阵末行、正焦距、像素变换可逆。
3. 所有数值有限；时间在对应片段内；跨时钟映射未确认时不得计算精确滞后。
4. 声明的图像尺寸、图像校正状态、裁剪变换与实际媒体一致。
5. 提供的投影与几何重算是否一致；不一致时保留两者和差值，报告输入自洽问题，禁止覆盖原投影。
6. source 指令、notes、raw 序列、评估文件不默认加入检测/VLM 输入；采用明确字段白名单。
7. 可评估性逐项判断：未知点定义、不可见、严重模糊、方向退化分别处理，不能合并成零误差。

本次 helper 检查其中能由样例直接验证的结构、几何和路径条件；完整媒体与语义认证属于实施计划。格式通过只说明数据可解释，不证明客户轨迹正确。

## 8. 夹爪外观模板：`gripper-template/1.0`（F5.8）

Schema：[gripper_template.schema.json](gripper_template.schema.json)，示例：`docs/contracts/examples/eef-gripper-template.json`。
它不是逐帧记录，而是**同一夹爪的外观库**：每个条目是从某路相机某一帧裁下来的小图（`patch_png_base64`）、
小图坐标系里标好的物理点（`points_patch_px`，key 必须在 `tool.point_ids` 里）、可选的夹爪掩膜（`mask_png_base64`，
白色 = 夹爪，只在掩膜里提特征）、该条目来自哪路相机（`camera_id`，`null` 表示任意相机）与开合状态（`closed_fraction`）、
来源（`source`）与出处等级（`provenance.method`：`human_click` / `synthetic_fixture` / `tool_export`）。

运行时（跟踪器的 P-A′ 模式）：每隔 `matching.every_frames` 帧，在整幅画面里提 ORB 特征，与本路相机的所有条目做比率检验匹配，
用 RANSAC 拟合二维相似变换（`matching.model`），内点数须不少于 `min_inliers` 且不少于条目特征数的 `min_inlier_ratio`，
尺度在 `scale_range` 内；把条目上标的点变换到当前帧，就是一个锚点，与人点的种子完全同等地进入锚点间的跟踪。
两个条目给出的位置相差超过 `agree_px` 且内点数相近时视为歧义，放弃这一帧。RANSAC 按 `rng_seed` 固定随机数，同一帧同一答案。

构建：`python -m curation.extensions.eef_consistency template-build` 从若干帧的观测行（人点的，或 DEMO 的 `synthetic_fixture`）裁条目；
掩膜来自相邻两个锚点之间的跟踪：只保留跟着标记点刚性运动的特征所在的圆盘，静止的背景、桌面和滑动的物体都被剔除。
两个锚点之间夹爪移动不足 20 像素时分不出夹爪和背景，这样的帧默认不做成条目（`skipped_static`）。

与观测种子的关系：模块参数里两者**二选一**；同一路相机两样都给时以种子为准（人点的锚点等级更高）。
模板描述的是夹爪长什么样，与账本对不对无关，所以从一个数据集的合格 episode 建一次，可以查这个数据集的全部 episode。
条目 `provenance.method = synthetic_fixture` 的模板只用于 DEMO，不能用于视觉精度验收。
