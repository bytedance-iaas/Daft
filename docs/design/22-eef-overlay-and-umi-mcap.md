# 22 · 轨迹叠加进迷你播放器；UMI（DAS 手持夹爪 mcap）的意见路线

> 状态：**实施稿 v1.1（2026-10-08，开工前评审后）**——需求方 2026-10-08 定：叠加走前端叠加层，先用现有的 Franka 数据（dataset2，`eef_ds2_lr3`）做；
> 画什么由用户勾选；UMI 不新加模块、放在「EEF–视频一致性」里、现在不判废只出意见；UMI 缺的数据按 §7 的假设做 DEMO——需求方同意先按假设做、
> 标明是假设，驻场时当面与客户确认，F5.18 / F5.19 不再等确认；生产输入按 §7 向客户要。决策 D74、D75（00 §7）。需求账本：阶段 5 的 F5.17–F5.19。
> 直接在 `feat/curator-v2` 上开发。v1.1 相对 v1.0 的改动见 §9「开工前评审」：对时改按画面帧的时间、标签改成前端开关、A 默认关并换色、
> 「与送模型的一致」预设、EEF 发现的迷你布局、缺测放宽与插值、两手各自世界系的校验面、内参走 `H_media_from_calibration`、新细码 `ego_motion_suspect`。
> 依据：需求方 2026-10-08 两点诉求与答复、客户截图（第三视角鱼眼、两只手）、`tos://galbot/mcap/` 七个客户 mcap 与样本集 GenRobot 子集的实测、
> 分支现状（设计 12 §10.5、设计 20、`eef-overlay` 接口）。

## 0. 开工指引（先读这一节）

读的顺序：§1 现状与缺口 → §3 叠加的设计（F5.17，先做）→ §4 UMI 数据实测 → §5 UMI 路线（F5.18、F5.19）→ **§7 假设与生产输入**（F5.18 起的前提）→ §8 验收。

落地顺序：**F5.17**（叠加并入迷你播放器，dataset2 立刻能验，先 LeRobot、再它的 mcap 孪生）→ F5.18（mcap 适配器与每种夹爪一份的标定文件）
→ F5.19（自运动一致性与意见输出）。§7 按假设做（`model_assumed`，报告与意见注明「按假设值」），驻场时当面与客户确认。
契约：F5.17 升 C4 4.3.0（`EefOverlay` 只加字段）；F5.18 EEF 输入格式升 1.1（只加可选字段，旧文件不变）、新增 `umi-calibration/2` 的 Schema；
F5.19 C1 加细码 `ego_motion_suspect` 与参数、C2 的 EEF 记录加 `ego_motion` 分项。

要动的地方：

| 组件 | 位置 | 要做的 |
|---|---|---|
| 契约 | `docs/contracts/openapi.yaml`（C4 4.3.0）；`docs/contracts/eef/sample.schema.json`（EEF 1.1）、新 `umi_calibration.schema.json`；C1 `backend/curation/contracts/modules.py`；C2 的 EEF 记录 Schema；各自的 `examples/` | §3.2 的 `times_s`、`hands`、图层声明；§5.2 的 `umi.world_frames`、标定文件；§5.3 的细码与 `ego_motion`；`CONTRACTS.lock`、`npm run gen:api`；C4 的客户可见变更记录中英两版 |
| Daemon | `daemon/viz/eef_overlay.py`、`daemon/routes/viz.py`、`daemon/viz/mcap.py` | §3.2：`times_s`（按配对的视频帧从可视化读出）、观测图层（读运行目录的观测文件）、图层声明；缓存键加观测文件的 size / mtime；§5.2 的插值间隔参数（F5.18） |
| 内核 | `curation/extensions/eef_consistency/overlay.py`、`history.py`、`umi.py`、`load.py`、新 `egomotion.py`、`adapters/` 新 `umi_mcap.py`、`__main__.py`；`curation/viz/mcap_episode.py` | §3.2 坐标轴与未来轨迹图层；§3.4 mcap 扫描记下每条图像消息的时间；§5.2 `export-umi-mcap`、缺测与两手各自世界系的校验；§5.3 自运动分项与意见 |
| 前端 | `features/visualizer/`（`Player.tsx`、新 `cells/OverlayCanvas.tsx`、三种相机格子、工具条「叠加」菜单、`SidePanel.tsx`、`MiniPlayerModal.tsx`）、`lib/eefOverlay.ts`、`lib/vizLayout.ts`、`features/eef/EefRecord.tsx`（删 `EefOverlayVideo.tsx`）、`pages/report/EpisodesTab.tsx`、`mocks/eef.ts`、`locales/zh.ts` | §3.3 |
| 数据 | 新 `backend/scripts/make_mcap_twin.py`（LeRobot 数据集 → mcap 孪生） | F5.17 验收④（下表） |
| 文档 | 本篇；07 §4.5（播放器叠加层）；12 §10.5 与 §11.2（指到本篇）；18 §4.6（迷你版叠加与 EEF 发现的布局）；20（报告里的叠加改由本篇承担）；extension README、frontend README 的手动验证步骤 | 各 F 落地时改 |
| 测试 | `backend/tests/eef/`（overlay、umi_mcap、egomotion）、`backend/tests/daemon/`（eef-overlay 接口）、`backend/tests/viz/`（扫描的消息时间）、前端 Vitest（图层选择与预设、按时间找帧与陈旧上限、contain 映射、EEF 发现的布局、MSW 处理器） | §8 |

不动的：质检读取器（`ingest/` A 类）、判决与策略（D56–D59）、设计 20 的模型意见请求包、`eef-overlay` 的现有字段与错误码。

验收数据：

| 用途 | 数据 | 说明 |
|---|---|---|
| F5.17 | `~/ws/ws_general/galbot/dataset2`（`eef_ds2_lr3`，Franka + 两路外部相机）+ `trajectory.json` + `seeds.jsonl` 或 `gripper_template.json` | 判决模式，有观测图层可验；TOS 上 `tos://galbot/eef_ds2_lr3/` 是同一份 |
| F5.17 意见模式 | 同上，不传夹爪参考 | 只有声明图层 |
| F5.17 mcap | dataset2 的 mcap 孪生：`backend/scripts/make_mcap_twin.py` 把 `eef_ds2_lr3` 写成每条 episode 一个 `.mcap`（两路相机 H.264，码流故意从 GOP 中间开始、开头 3 帧解不出；其余 topic 与相机同时开始），轨迹用 `tools/eef_convert.py to-mcap` 转 | 复现 DAS 的「首个关键帧前解不出」，验 §3.4 的对时；质检时钟 `offset_s ≠ 0` 的情况放在 `tests/daemon` 里（孪生的轨迹时间以相机第一帧为零，action 晚开始会让发现的色块整体偏移，验收时容易误读） |
| F5.18 / F5.19 | `tos://galbot/mcap/00001(1).mcap`（两手、带 `camera_info`、robot1 有抓放）、`umi_sample.mcap`（倒水，**没有** `camera_info`） | 或样本集 `tos://curation-robo-anchor/anchor/v1/mcap/genrobot_fold_and_store_clothes/`（公开，CC BY-SA 4.0） |

本机看效果：`.claude/launch.json` 的 `curator-daemon-eef`（数据根 dataset2，托管 `frontend/dist`，先 `npm run build`）；控制台 tasks/new → 本地路径 `eef_ds2_lr3` → 勾 EEF 模块 → 第二屏传 `dataset2/trajectory.json`，判决模式再传种子或模板。

## 1. 现状与缺口

### 1.1 分支上已有的（`feat/curator-v2` @ a1c62d38b）

- `GET /tasks/{id}/episodes/{index}/eef-overlay`（C4 2.6.0 `EefOverlay`，`backend/daemon/viz/eef_overlay.py`）：读任务冻结的
  `trajectory.json`（运行目录 `inputs/` 的副本，被清理时先从交付目录取回，再不行用上传件），不解码视频，按样本帧算出矢量图层
  （`point` / `segment` / `arrow` / `polyline`，坐标是源视频像素），内存 LRU 缓存。普通 EEF 画 P 的过去轨迹（青）、B（橙）、A（红箭头）、
  P（红圈）；UMI 画本手过去轨迹、两指连线、接近轴、中心点。`viz_camera` 把上传件的相机对到可视化相机键（LeRobot 按视频文件、mcap 按 topic）。
- 前端 `features/eef/EefOverlayVideo.tsx`：自己的 `<video controls>` 加一层 canvas，用 `video.currentTime − from_ts` 乘 fps 找样本帧
  （`lib/eefOverlay.ts`：`frameMap`、`sampleFrameAt`、`containFit`、`drawFrame`）。只在 Episode 明细的「模型意见」块出现
  （`assessment_mode = vlm_opinion`），裁决卡没有。
- 迷你播放器（设计 18、19、21）：`Player mode="mini"`，三种格子（`VideoCell` 直连、`FramesCell` 帧包、`SamplesCell` 浏览器解码），
  一份时钟 `t`（episode 秒，`clock.ts`），画布按 `t` 推帧号，`<video>` 按 `t − offset + from` 绑定并纠漂；发现以色块与片段带显示；
  格子上方只有深度叠加一种画面层（`DepthCell` 的 `canvas.vz-depth.over`，透明背景、可调不透明度），是本篇叠加层的样板。
  `.vz-cell.kind-video canvas` 的样式会给格子里所有 canvas 上底色，新叠加层要像 `.vz-depth.over` 那样覆盖成透明。
- 意见模式（没有种子也没有模板）已经只出 info 级发现 `opinion_mismatch`（带 `time_s` 与相机），不影响判决；判决模式才出 blocking 的 `inconsistent`。
- 判决模式的运行目录里有逐帧观测：`checks/eef_video_consistency/observations/<ep>/<cam>.jsonl`（`points{pid:{uv_px, visibility, …}}`，
  `pixel_space: media`）和 `curves/<ep>/<cam>.parquet`；没有接口读它们。

### 1.2 缺口

| 缺口 | 影响 |
|---|---|
| 叠加不在迷你播放器里，是单独一个播放器，不受播放器时钟驱动 | 客户要在迷你版里看；两套播放器、两套对时 |
| 用 `fps` 把 `currentTime` 换成帧号，mcap 的 `ep.fps` 为 null | **mcap 数据集画不出标记**，而 UMI DEMO 就是 mcap |
| 图层没有三维坐标轴、没有未来轨迹、没有观测点 | 客户截图里的「三维坐标和轨迹」画不全；判决模式看不到观测 |
| 发现的 `scope.cameras` 用的是上传件里的相机 id（如 `27432424_left`），迷你布局按可视化键找相机，找不到退到第一路 | 焦点落错相机 |
| mcap 的样本包、转封装从第一个关键帧起算，`offset_s` 有偏移；EEF 的 mcap `video_frame_index` 是消息序号 | 浏览器里按帧号对会错位；要由 Daemon 把消息序号换成这条消息在播放器时钟上的时间（§3.4） |
| 上传件的 `timestamp_s` 只保证在自己的时间线上递增，零点与时钟源（log_time / header）都不一定和播放器一致 | 拿它对时，换一份数据就可能整体偏移 |

## 2. 需求还原

### 2.1 客户截图

一路第三视角鱼眼相机（胸前或头部，画面底部是采集者的身体），同时看见左右两只手持夹爪，每只夹爪的指尖中心画了三维坐标轴
（红 / 绿 / 蓝三根，标 L / R），左手还有一小段蓝色轨迹。要点：

- 画的内容：坐标轴 + 轨迹线 + 手的标签；轨迹是过去还是未来、多长，截图看不出，做成可选（§3.2）。
- 画在**能同时看见两只手的那路**相机：两只手的位姿和这路相机的位姿必须在同一坐标系。客户的工具画得出来，说明他们有这套变换；
  对应我们的格式是形态 B（位姿 + 这路相机的标定，同一参考系）或形态 A（他们直接给投影点）。
- 我们手里的 UMI 数据（客户 mcap、样本集 GenRobot）只有每只手自己的腕部相机，两只手的 VIO 世界系各是各的（§4）。
  **截图那种画面用现有数据画不出来**；要问截图出自哪个数据集 / 设备（§7 第 6 项）。腕部相机里能画本手：坐标轴、两指连线、过去 / 未来轨迹。

### 2.2 两种相机在模块里的分工

| 相机 | 位置 / 方向分项 | 轨迹叠加 | 意见 |
|---|---|---|---|
| 第三视角（DROID 的外部相机、截图那路） | 有种子或模板时测；没有则不测 | 画 | 设计 12 §10.5 的模型意见 |
| 腕部（UMI / DAS） | 报不支持（本手在画面里不动） | 画本手 | §5.3 自运动一致性 + 设计 20 的模型意见 |

## 3. 叠加并入迷你播放器（F5.17，D74）

### 3.1 路线取舍（留档）

| | 前端叠加层（采用） | 后台烧进视频 |
|---|---|---|
| 做法 | Daemon 现算图层（已有接口），浏览器 canvas 逐帧画 | 逐帧画、libx264 重编码，播放器放新视频 |
| 开销 | 零解码、零存储；每条 episode 一份 JSON（DROID 287 帧约 100 KB；UMI 1,900 帧带 24 点轨迹约 1 MB） | 1600×1300 30 fps 50 s 一路约 1–2 分钟 CPU、几十到上百 MB；任务百条即小时级与 GB 级；设计 20 已明确「不落盘」 |
| 画质 | 矢量，随显示尺寸清晰 | 固定在源分辨率 |
| 切换 | 想看什么点什么，即时 | 烧死了 |
| 一致性 | 位置与送模型的画面同一函数算出，像素级外观略有差别 | 与送模型画面完全一致 |
| 分享 | 页面内看 | mp4 可下载 |

「导出带轨迹的视频」留作按需的后台渲染（同一份图层数据光栅化，经 `/media/sign` 像审计片段 `details/audit_clips/` 那样签发），不在本轮。

### 3.2 接口：C4 4.3.0，`EefOverlay` 只加字段

`EefOverlayCamera` 新增：

| 字段 | 含义 |
|---|---|
| `times_s: (number \| null)[]` | 每个样本帧所配的那一帧画面（`media_frames[f]`）在**播放器时钟**（episode 秒）上的显示时刻，由 Daemon 从同一份可视化数据读出（§3.4）：LeRobot 是相机的 `offset_s` + 帧号 / fps，mcap 是该 topic 第 k 条图像消息在扫描里的时间。前端直接和时钟 `t` 比，不经 `check_clock`。没有配对帧、或那一帧放不出来（首个关键帧之前）为 null；`viz_camera` 为 null 时整列为 null。`media_frames` / `fps` 保留 |
| `hands: {id, title, color, opening_m}[]` | 这路相机里画的手（普通 EEF 一个，名字取工具名；UMI 是本手），清单按手分节；`opening_m` 是每个样本帧的开口读数（米，没有为 null，整列没有为 null），画在两指连线旁 |

`EefOverlayLayer` 新增：

| 字段 | 含义 |
|---|---|
| `id` | 稳定标识，按角色取：`point`、`finger_axis`、`axis`、`axis_x` / `axis_y` / `axis_z`、`trail_past`、`trail_future`、`observed_point`、`observed_trail`、`residual`；同一个 id 在各路相机、各只手上是同一种图层，勾选按 id 记 |
| `group` | `declared` / `axes` / `trail_past` / `trail_future` / `observed` / `residual` |
| `title` | 清单里显示的名字，带真实的点名 / 轴名（如「指尖中心 P（tcp）」，P、A 由 `opinion.select` 挑，未必是 TCP 与接近轴）；组名是固定的几种，放 `locales` |
| `default_on: boolean` | 默认是否画 |
| `hand` | 属于哪只手（`hands[].id`） |
| `in_model: boolean` | 送模型的标注片段里画了这一层（「与送模型的一致」预设只开这些） |
| `model_color` | 送模型的片段里这一层的颜色，与 `color` 不同时才给（A：显示换了色，模型看到的是红色），否则 null |

文字不单独成组：各图层的 `label`（手名、点名、轴名）、开口读数与角上的样本帧号由前端的「标签」开关统一管（帧号与模型意见引用的帧号同一口径）。

图层与默认（颜色指普通 EEF；UMI 的声明组与轨迹沿用每只手的固定配色）：

| 组 | 图层 | 默认 | 来源 |
|---|---|---|---|
| `declared` | 指尖中心 P（红圈）、两指连线 B（橙，旁边是开口读数）、接近轴 A（箭头） | P、B 开；**A 关**，显示色换成紫色（与三轴不冲突），`model_color` 红 | 上传件投影（`review.marks_for`、`umi.project`）；P、A、B 与送模型的同一套挑法 |
| `axes` | 从 P 沿工具系 x / y / z 各 6 cm 的三条 `arrow`，红 / 绿 / 蓝（长度同现有的接近轴） | 开 | 同上，三根轴端点各投影一次 |
| `trail_past` | 过去的 P，青色 `polyline`，**用当前相机重投影**，缺测断线 | 开 | `history.project_eef` / `umi.project(indices=…)`；时长取送模型时的值（普通 EEF `history.HORIZON_S`，UMI 轨迹包的 `umi.horizon_s`），不做成模块参数 |
| `trail_future` | 未来同样时长的 P，浅青 | 关 | `history.indices` 加一个向未来的版本 |
| `observed` | 判决模式的观测点（绿十字）与观测轨迹（过去同一时长，绿） | 有观测时开 | 运行目录 `observations/<ep>/<cam>.jsonl`，`pixel_space: media` 直接用；只取与 P 同名的点；观测轨迹只画固定相机（移动相机过去的像素不能重投影）；意见模式没有这组 |
| `residual` | P 的声明点到观测点的 `segment`（黄） | 关 | 上两者相减 |

只有二维投影、没有标定的输入（`external_2d`，或 `projection.source = provided` 而缺标定）没有坐标轴与轨迹两组，只画声明点。

观测文件从运行目录读，被清理时先恢复（与 `inputs/uploads.json` 同一套 `store.revision`）；读不到就没有这组，不报错。
缓存键加观测文件的 size / mtime。`viz_camera` 不变。

### 3.3 前端

- `Player` 新增 `overlays` 输入：按可视化相机键给图层与 `times_s`；相机格子里加一层 `OverlayCanvas`（绝对定位、透明背景、
  `pointer-events: none`，像 `.vz-depth.over` 那样盖掉 `.vz-cell.kind-video canvas` 的底色），三种格子共用。
  - 按时钟 `t` 二分找「显示时刻不晚于 t 的最近样本帧」；离 `t` 超过 1.5 个样本间隔就不画，缺测段、低频段不把旧标记画到新画面上。
  - 直连 `<video>` 在浏览器支持时用 `requestVideoFrameCallback` 取实际显示那一帧的媒体时间，换成 episode 时间再找，播放中也逐帧对准；不支持时按时钟。
  - 按 `object-fit: contain` 的拟合把源像素映射到格子（`containFit`），`devicePixelRatio` 的处理与 `FramesCell` 相同；`drawFrame` 的画法沿用，按选中的图层过滤。
- `MiniPlayerModal`：一律请求 `eef-overlay`（react-query，`staleTime: Infinity`；episode 读到之后再发，mcap 这时已扫描完）。
  404 `no_eef_module` / `no_episode` 静默：没有叠加、不出菜单；其他错误只提示一行，不影响播放。焦点相机改用 `viz_camera` 定位（`cameraOfScope` 找不到时的兜底）。
- EEF 发现的迷你布局：有叠加时放所有带叠加的相机（至多 3 路，发现的那路在前），不再是「一路相机 + 一组曲线」；没有叠加时照旧。
  不选发现时本来就是全部相机（至多 3 路）。
- 工具条加「叠加」菜单（有叠加时才出）：
  - 顶部三选一：原图 / 叠加 / 只看观测；
  - 预设：「默认」（各图层的 `default_on`）、「与送模型的一致」（只开 `in_model` 的图层、按 `model_color` 上色、三轴关）；
  - 按手、按图层的勾选清单，来自接口的 `hands` 与图层声明；外加「标签」开关；
  - 选择记在 `localStorage`（键 `curator.eefOverlay.layers`，按图层 `id`，不分任务），首次按 `default_on`。
- 选中某条发现时，该相机的叠加线加粗、其余相机的变淡（已有的色块、片段带照旧）。
- 右侧「详细信息」：焦点落在带叠加的相机格子上时多一节「叠加」，写轨迹来源、画的手、当前样本帧号与它对应的画面帧；F5.18 起再加插值间隔的设置（§5.2）。
- Episode 明细「模型意见」块里的独立播放器删掉（`EefOverlayVideo.tsx`），证据帧按钮改为驱动迷你播放器：打开迷你播放器、该相机排在前面，
  按 `times_s` 跳到这一帧并停住。裁决卡自然得到同样的叠加。
- 「可视化」完整页（数据集级）没有任务、没有轨迹，本轮不画。

### 3.4 对时与相机对应

- 叠加按**画面帧的显示时刻**对：标记是给第 `media_frames[f]` 帧画面算的，就在播放器显示这一帧时画。上传件的时间线不参与对时，
  它的零点、时钟源（log_time / header）和播放器不一样也不影响。
- 帧的时刻由 Daemon 从同一份可视化数据读出，不解码：
  - LeRobot：第 k 帧 = 相机 `offset_s` + k / fps，与 `<video>` 的绑定 `t − offset + from` 是同一套；
  - mcap：第 k 条图像消息在扫描里的时间。k 按 log_time 从 0 数，与 EEF 的 `video_frame_index` 同一口径，含首个关键帧之前的消息。
    扫描（`curation/viz/mcap_episode.py`）把每路相机全部消息的时间记进 `episode.json`（转封装那条路原来没记，扫描缓存因此升版本）；
    首个关键帧之前的消息放不出来，对应的 `times_s` 为 null。叠加请求在 episode 读到之后才发，这时扫描已完成，只是查表。
- 帧包与浏览器解码按同一份时间摆帧，标记与画面严格同帧；直连 `<video>` 用 `requestVideoFrameCallback` 对准实际显示的帧（§3.3）。
- 发现的色块与片段带仍按质检时钟（`check_clock` + `time_s`）摆。上传件的时间线与质检时钟一致时（dataset2 的 LeRobot 与 mcap 孪生、
  §5.2 的 UMI 导出），两边落在同一处。
- 相机对应沿用 `viz_camera`。

## 4. UMI 数据实测（2026-10-08）

### 4.1 候选数据

| 候选 | 格式 | 相机 | 内外参 | 评价 |
|---|---|---|---|---|
| 客户 `tos://galbot/mcap/`（7 个文件） | mcap | 每手一路腕部鱼眼 | 6 个文件带 `camera_info`；VIO 位姿；开口编码器；无相机→指尖外参 | 客户自己的设备 |
| 样本集 GenRobot RealOmin（`tos://curation-robo-anchor/anchor/v1/mcap/genrobot_*`，15 个子集 94 条） | mcap | 同上 | 同上；`genrobot_p2_zip_clothes/episode_100026.mcap` 实测 topic、标定、分辨率、约定与客户文件**完全一样** | 公开可展示（CC BY-SA 4.0，要署名）；和截图最像的子集是 `genrobot_fold_and_store_clothes`（4 条，00675 两路长时间欠曝）与 `genrobot_p2_fold_and_store_clothes`（3 条） |
| FastUMI-100K（anchor，19 个子集 99 条） | LeRobot v2.1 | 腕部 | 数据集里没有标定 | 画不了投影 |
| DecisionFacts Physical-AI-UMI（设计 20 用的原始 UMI） | 原始会话 → LeRobot | 两手两路 GoPro 2704×2028 | 显式标定文件 | 链路已通，但不是 mcap、设备与客户不同 |

截图那种「一路相机看两只手」的数据，这几处都没有。

### 4.2 客户 mcap 里有什么

文件：`00001(1).mcap`（50.8 s）、`00001.mcap`（57.4 s）、`00011.mcap`（48.7 s）、`00051.mcap`（166.6 s）、`00051(1).mcap`（30.3 s）、
`00111.mcap`（114.7 s）、`umi_sample.mcap`（22.1 s，**没有 `camera_info`**）。每只手（`robot0` / `robot1`）：

| topic | 内容 | 实测 |
|---|---|---|
| `/robotN/sensor/camera0/compressed` | `foxglove.CompressedImage`，h264 | **1600×1300**，30 fps，流从 GOP 中间开始（首个关键帧前解不出） |
| `/robotN/sensor/camera0/camera_info` | `foxglove.CameraCalibration` | 640×480，`equidistant`（= `opencv_fisheye`），K、D[4]，`T_b_c` = [0.013, 0.022, 0.013, 1, 0, 0, 0] |
| `/robotN/vio/eef_pose` | `foxglove.PoseInFrame`，`frame_id: world` | 30 Hz（个别段 15–18 Hz），每只手**各自的** VIO 世界系（首帧都在原点附近） |
| `/robotN/sensor/magnetic_encoder` | 开口 | 米，0–0.103 |
| `/robotN/sim/robot_info` | `das_gripper`，`base_link` = `eef_pose` | `gripper_end_effort_link` 全部为空：**没有指尖的变换** |
| IMU（200 Hz）、`system_info` | — | 自运动检查不用 IMU；SLAM 路线才用（§6） |

### 4.3 三个关键事实

1. **内参按分辨率换算**：640×480 是整幅 1600×1300 非等比缩放后的标定（x 乘 2.5、y 乘 2.708）；换算后 fx ≈ fy ≈ 748，说明换算成立；
   等距模型的 D 不随分辨率变。
2. **坐标系约定**：`eef_pose` 是 VIO 机体系（x 前、y 左、z 上），`T_b_c` 只给平移、旋转是单位阵，指的是机体系对齐的相机系；
   到光学系（x 右、y 下、z 前）还要一个固定旋转 `R_bc = [[0,0,1],[-1,0,0],[0,-1,0]]`（列是光学系三轴在机体系里的方向）。
   这是用画面反推出来的：24 种轴对齐旋转里只有它让 VIO 的相对旋转与画面估计的相对旋转一致（`00001(1).mcap` robot1 十二对帧：
   旋转差中位 1.1°、最大 3.0°，平移方向差中位 8.3°；把 `T_b_c` 当四元数 wxyz 或 xyzw 读都不对，分别 4.3° 与 3.8°）。
   `00011.mcap`、`umi_sample.mcap`、GenRobot `episode_100026.mcap`（1.7°）同样成立。
3. **相机→指尖中心没有**：DEMO 用估计值 `(0, 0.086, 0.17) m`（光学系；由指尖在画面里的位置与最大开口反推），画出的中心点落在两指之间；
   两手世界系不共享，另一只手画不进本手画面，只画本手（与设计 20 一致）。

效果图：`~/ws/ws_general/galbot/dataset2/preview/umi/`；脚本：`~/ws/ws_general/galbot/tools/umi/`（探测、解帧、叠加、自运动比对）。

## 5. UMI 路线：适配器、格式、意见输出（F5.18、F5.19，D75）

### 5.1 原则

不新加模块：整条路线跑在 EEF 模块的意见模式下（没有种子和模板），只出 info 级发现，不参与 keep / drop。
每种夹爪一份标定文件，所有数据集复用；缺什么按 §7 处理。

### 5.2 `export-umi-mcap`：mcap → `trajectory.json`（F5.18）

设计 20 的 `export-umi` 吃原始 UMI 会话（`dataset_plan.pkl` + CSV）；客户数据是 mcap，加一个适配器
`python -m curation.extensions.eef_consistency export-umi-mcap --mcap-root DIR --calibration gripper.json --out trajectory.json`
（后续可像 F5.16 那样由预检起草、表单里确认，本轮先离线）：

- 输入：mcap 的位姿 / 编码器 / `camera_info` topic（内置 UMI 模版已认 topic 名），加每种夹爪一份的标定 JSON（`umi-calibration/2`，§7 表里
  标「生产输入」的项都在这里）：

  ```json
  {"schema_version": "umi-calibration/2", "gripper": "das_gripper_v3",
   "pose_frame": "vio_body_flu",
   "body_to_optical": [[0,0,1],[-1,0,0],[0,-1,0]],
   "T_camera_tcp": [[1,0,0,0],[0,1,0,0.086],[0,0,1,0.17],[0,0,0,1]],
   "finger_axis": "camera_x", "opening": {"unit": "m", "scale": 1.0},
   "intrinsics_fallback": {"robot0": {"K": [...], "D": [...], "image_size_wh": [640, 480]}},
   "intrinsics_scaling": "stretch_to_video",
   "pairing_tolerance_s": 0.02,
   "provenance": {"source": "...", "assurance": "model_assumed | declared"}}
  ```

- 换算：`T_world_cam = T_world_body · T(T_b_c 平移) · R(body_to_optical)`，`T_world_tcp = T_world_cam · T_camera_tcp`。
- 内参：标定保持 `camera_info` 的原值（640×480 的 K、D），用格式现成的逐帧 `H_media_from_calibration` 把标定图像坐标换到媒体像素。
  `stretch_to_video` 时 `H = diag(视频宽 / 标定宽, 视频高 / 标定高, 1)`，DAS 是 2.5、2.708；现有 UMI 导出就是这样缩放的。不改写 K，标定能原样追溯。
- 时间线与配对：
  - 每只手的相机帧各自配本手的位姿，按 `header.timestamp` 取最近的（实测差 1–7 ms），超过 `pairing_tolerance_s`（20 ms）记缺测（`null`）；
  - 样本时间线取 robot0 的相机帧，robot1 的相机帧按时间就近对上；
  - `timestamp_s` = 这一帧消息的 log_time − 质检时钟的锚点（内置 UMI 模版下是 `/robot0/vio/eef_pose` 第一条消息的 log_time，
    `check_mapping` 取 action 的第一个来源）。这样发现的色块与片段带落在画面的对应处；叠加本身按 §3.4 对时，不依赖它。
    锚点之前的帧本来也配不上位姿，不进时间线（格式要求 `timestamp_s ≥ 0`）；
  - 首个关键帧之前的消息不配对：它们解不出来，送模型的片段从首个关键帧开始。
- 缺测：有数据的帧才画、才查，整条不再因为缺一帧而校验失败。`load_hands` 放宽为：相机位姿或手的位姿缺测的帧不画、轨迹在那儿断开，
  模型意见与自运动检查跳过这些帧。
- 插值：VIO 个别段只有 15–18 Hz，视频是 30 fps，按 20 ms 配对会隔帧缺测，轨迹碎成点。trajectory.json 不预先插值，缺的就是缺；
  读的一方在前后两个有效位姿相隔不超过「插值最大间隔」时在中间插（位置线性、姿态球面插值）：
  - 缺省 3 个样本间隔（30 fps 即 100 ms），参考范围 2–5 个间隔，按这条 episode 的帧率换算成毫秒显示；
  - 质检（送模型的标记、自运动检查）用缺省值，记录里写明用了多少、插了几帧；
  - 播放器右侧「详细信息」的「叠加」一节可以改，只影响画面（`eef-overlay` 加查询参数，缓存键带上它）；「与送模型的一致」预设回到缺省值。
- 媒体引用：`media.topic`（F5.13 的 mcap 路径）；`image_size_wh` 取视频实际解码尺寸；`media.fps` 写标称帧率 30（送模型的片段要用帧率）；
  `frame_count` 写该 topic 的消息数。
- 格式：两手各在自己的世界系。EEF 1.1 加可选字段 `umi.world_frames: "shared" | "per_hand"`（缺省 `shared`，旧文件不变）。`per_hand` 时：
  - 每只手的位姿写在各自的 `<hand>_vio_world` 里，本手相机的标定 `reference_frame` 与逐帧 `T_reference_camera` 也在这个系里；
  - 样本级 `reference_frame` 写 `per_hand`，逐帧 `eef` 为 null（手的位姿在 `hands` 里）；
  - 要放宽三处校验：`load.py` 的「相机标定与样本同一参考系」「逐帧位姿与样本同一参考系」，以及 `umi.load_hands` 的同一参考系；
    跨相机的几何（画另一只手、`declared_track` 之类）不用；
  - 截图那种公共坐标系的数据来了，仍走 `shared`。
- 标定文件 `umi-calibration/2` 是交给客户填的生产输入，Schema 放进契约目录（`docs/contracts/eef/umi_calibration.schema.json`，配合法与不合法的示例）；
  `/1`（设计 20）照旧只在代码里校验。
- 产出同时写一份 `umi-export-report.json`：每路相机的配对成功率、缺测段与低频段、内参来源（文件内 / 回退）、`T_camera_tcp` 的 assurance，
  以及 §7 第 2、3、7、8 行的平台校验（`calibration_suspect`、`timing_suspect`）。这些在导出时算：数据都在手边，预检不解码视频，不放在预检里。

### 5.3 自运动一致性与意见输出（F5.19）

输入（一个任务）：

| 输入 | 来源 |
|---|---|
| 数据集 | mcap（每只手的腕部相机 topic；第三视角相机等 §7 第 6 项，本轮不做——`load_hands` 现在要求每路相机都是某只手的腕部相机） |
| `trajectory.json` | §5.2 的适配器产出 |
| 模块参数 | 自运动检查的窗口（默认 0.5 s）、阈值档（`demo` profile，标未校准）；插值用 §5.2 的缺省间隔；模型意见照设计 20 |

输出（每条 episode）：

| 输出 | 内容 |
|---|---|
| 叠加图层 | §3.2 的各组 |
| 每路腕部相机的「自运动一致性」分项 `ego_motion` | 状态 ok / suspect / unknown / unsupported；指标：旋转差中位与 P95（度）、平移方向差中位（度）、估计的位姿时间差（秒，带置信）、覆盖率（有足够匹配的窗口占比） |
| 不好的片段 | `[起, 止]`（帧与秒）、原因（旋转对不上 / 平移方向对不上 / 时间差 / 画面匹配不足）、幅度（度或秒）、幅度档（轻 / 中 / 重）、证据帧号 |
| 整条的意见 | 好 / 不好；不好时列片段，按幅度排序；一句中文解释 |
| 发现 | 新的 info 级细码 `ego_motion_suspect`（下文），不影响 keep / drop；模型意见照旧出 `opinion_mismatch` |
| 模型意见 | 设计 20 现有的抓放时机、开口饱和等，不变 |

做法：相邻帧（0.5 s）ORB 匹配，按鱼眼模型去畸变，本质矩阵求相机相对旋转与平移方向，对 VIO 轨迹推出的相机相对运动；
画面底部的夹爪本体用掩膜去掉（掩膜由 `T_camera_tcp` 投影的夹爪区域加固定底边得到）。纯 CPU，一对帧几十毫秒。
时间差：在 ±0.5 s 内扫位姿偏移，取旋转差最小处，与设计 12 §8.3 的 lag 同号约定。客户数据上的读数：旋转差中位 1.1°、平移方向差 8°（正确约定）；
约定错 ≥ 3.8°；位姿流整体晚 0.2 s 时旋转差中位 1.5°、最大 4.2°，晚 0.5 s 时 3.2°、最大 10°。白墙、少纹理的段落匹配不足，报 unknown 不硬判。
腕部相机的「位置」「方向」分项报不支持（`wrist_camera_spatial_only` 已有）。

为什么要它：腕部相机跟着夹爪动，本手的标记在画面里永远在同一处，叠加和模型意见都检验不了这只手的位姿轨迹；
自运动是独立的一路证据——相机的运动从记录的位姿推一遍、从画面推一遍，两边对得上说明位姿可信，对不上多半是位姿流早了 / 晚了、
坐标约定或安装标定写错、VIO 跳变或漂移、视频与位姿不是同一只手。

细码 `ego_motion_suspect`：info 级，一条 episode 至多一条；`time_s` 指最严重的片段，`scope` 是那路相机；读数带状态、
旋转差中位 / P95、平移方向差、估计的时间差与置信、覆盖率与片段列表；文案如「腕部相机的运动与记录的位姿不一致：位姿约晚 0.48 s
（第 300–420 帧，重）」。分类表挂 MV-4（相机标定、末端投影与画面不符），时间差那一类同时覆盖 AV-1（整体时间错位）。
与 `opinion_mismatch` 的区别：那条是模型看画面给的意见，这条是 CPU 算出来的数，可复现。

契约与展示：C1 加细码与模块参数（窗口、阈值档），注册表升版本、`findings.py` 派生、`taxonomy.json` 的平台注记重新生成
（`coverage_from_registry`）、回归工具的 `finding_map.json` 同步；C2 的 EEF 记录加每路相机的 `ego_motion` 分项与片段；
报告的 EEF 小节与 Episode 明细的「模型意见」块展示分项、片段、幅度档、证据帧与「按假设值」注记。

## 6. `T_camera_tcp`、`eef_pose` 的定义与 ORB-SLAM3

- **每种夹爪问一次，不是每个数据集问一次。** 原版 UMI（GoPro 手持夹爪）的流水线用 ORB-SLAM3 跑视频 + IMU 得到**相机**轨迹，
  再乘一个来自夹爪 CAD 的固定变换得到指尖中心（设计 20 的 DecisionFacts 标定里 `T_camera_tcp` 的 (0, 0.086, 0.220) 就是它）；
  原版 UMI 发布的数据集里 `eef_pos` 已经是指尖中心。DAS 是另一种夹爪，常数不同，而且 mcap 给的是 VIO 机体位姿、没替使用者换到 TCP。
  问不到也有办法：一次性标定小工具——在一帧里点两个指尖垫，结合编码器的开口（米）与鱼眼内参反推指尖中心的深度和位置，精度约 1 cm。
- **ORB-SLAM3 对这两个问题没用**：它产出相机轨迹，不产出相机到指尖的变换；客户的 DAS 自带 VIO。以后可能有用的两处（都不是本轮）：
  作为独立的相机轨迹复算 VIO（视频 + 200 Hz IMU 都在 mcap 里，有尺度、有回环，比 §5.3 的逐对帧检查强；代价是 C++ 依赖链、
  相机-IMU 标定与噪声参数、1600×1300 要降采样，平台是 Python 镜像）；多地图合并，把两只手或第三视角相机的世界系接到一起。

## 7. 假设（只为 DEMO）与生产输入

**先看结论：DEMO 真缺的只有第 1 项（相机→指尖中心），而且有可用的估计值，现在就能开工。** 表长是因为它还列了生产要客户**声明**什么，
才能把结果从「按假设值」变成「已声明」。需求方 2026-10-08：第 1、2、6 项现在拿不到客户的数据，先按表里的假设做、标明是假设，
驻场时当面与客户确认；F5.18 / F5.19 不等确认。按性质分：

| 性质 | 行 | 对 DEMO 的影响 |
|---|---|---|
| 真缺的数 | 1 | 唯一推不出来的；估计值偏差约 1–2 cm，只影响标记画在哪，不影响自运动检查和轨迹形状 |
| 有，但没声明 | 2、3、7、8 | 都从数据里推出并验证过（旋转差 1.1°、fx ≈ fy、配对 1–7 ms、开口 0–0.103 m）；生产要客户声明，只是为了不靠我们猜 |
| 个别文件缺 | 4 | 七个文件里只有 `umi_sample.mcap` 缺，借同设备的值 |
| 结构限制，不阻塞 | 5 | 只画本手，DEMO 本来就这么画 |
| 不在 DEMO 范围 | 6 | 只有要复现客户截图那种画面才需要 |
| 我们自己的参数 | 9、10、11 | 不用客户给 |

每一项：现有数据里有没有、DEMO 怎么假设、影响哪些输出、生产要客户给什么、进哪里、平台怎么校验。
**assurance 规则**：假设值一律标 `model_assumed`，报告与意见里注明「按假设值」；`declared` 才能去掉这个注记。

| # | 项 | 现有数据 | DEMO 假设（纯为演示） | 影响 | 生产输入（谁给、格式、进哪里） | 平台校验 |
|---|---|---|---|---|---|---|
| 1 | 相机→指尖中心 `T_camera_tcp` | 没有（`gripper_end_effort_link` 空） | `(0, 0.086, 0.17) m`，光学系，由一帧里指尖位置与最大开口目测反推；`model_assumed` | 中心点、坐标轴、两指连线、轨迹末端画在哪（偏 2 cm 约 15 px）；**不影响**自运动检查 | 客户或夹爪厂商给 CAD 值，或用我们的一次性标定工具（§6）得到 `declared`；写进每种夹爪一份的标定 JSON `T_camera_tcp` | 刚性检查；抽几帧投影的两指连线落在指尖垫上（人看一次，记入 provenance） |
| 2 | `eef_pose` 指哪个点、机体→光学旋转 | 没声明；`T_b_c` 只有平移 | 机体系 = VIO / IMU 的 FLU 系，位置在 IMU；旋转 `[[0,0,1],[-1,0,0],[0,-1,0]]`，由画面反推（1.1°） | 轨迹的形状与方向；自运动检查的基准 | 客户确认 `pose_frame`（`vio_body_flu` / `camera_optical` / `tcp`）与 `body_to_optical`，写进标定 JSON | 导出时抽 10 对帧做 §5.3 的检查（写进 `umi-export-report.json`），旋转差中位 > 3° 报 `calibration_suspect`、仍出意见 |
| 3 | 鱼眼内参的分辨率 | 640×480 标定，视频 1600×1300 | 按 x、y 各自比例拉伸（`stretch_to_video`），由 fx ≈ fy 验证 | 所有投影 | 录制分辨率的 `camera_info`，或在标定 JSON 里声明换算规则 | 换算后 fx / fy 偏离 > 2% 报 `calibration_suspect` |
| 4 | 没有 `camera_info` 的文件（`umi_sample.mcap`） | 缺 | 借同设备其他文件的值（各相机 K 差 1–3%，主点差到 ±35 px） | 该条 episode 的投影偏到几十像素 | 每个文件都带 `camera_info`，或标定 JSON 按相机序列号给 `intrinsics_fallback` | 没有内参、也没有回退 → 该相机 `unsupported` |
| 5 | 两手的世界系 | 各自的 VIO 世界系 | `world_frames: per_hand`，只画本手、只查本手 | 画不了另一只手；没有跨手一致性 | 要在一路画面里画两只手：客户给两手世界系的注册变换，或直接给公共坐标系下的位姿（`shared`） | `shared` 时抽帧检查另一只手的投影是否落在画面内 |
| 6 | 第三视角相机（截图那路） | 没有 | 本轮不做，只画腕部相机 | 截图那种画面出不来 | 该相机的 topic、鱼眼标定、每帧位姿（或固定外参）与两手同一坐标系（形态 B），或直接给逐帧投影点（形态 A）；并告诉我们截图出自哪个数据集、给一条样例 | 形态 A 走输入自洽；形态 B 走现有投影校验 |
| 7 | 位姿与视频的时间配对 | 两边都有 `header.timestamp` | 最近配对，容差 20 ms；前后有效位姿相隔不超过插值间隔时插值（§5.2） | 缺测段、轨迹断线 | 声明两条流的时钟（`source_timing`）与容差 | 配对成功率 < 90% 报 `timing_suspect` |
| 8 | 开口 | 编码器，米 | `opening_m = value`，手指沿光学系 x 开合 | 两指连线长度与方向；模型意见里的开口读数 | 标定 JSON 的 `finger_axis`、`opening.unit / scale`，有开口标定时给 `gripper_range`（设计 20） | 开口超出 [0, 0.2] m 报 `calibration_suspect` |
| 9 | 轨迹时长 | — | 过去 1 s、未来 1 s | 只影响画面 | 模块参数 | — |
| 10 | 自运动一致性阈值 | — | `demo` profile 未校准：旋转差 P95 > 5° 或时间差 > 0.15 s 判「不好」，幅度档按 5 / 10 / 20° | 意见的松紧 | 不是客户输入；用回归样本集校准后换正式 profile | 报告标「阈值未校准」 |
| 11 | 模型意见的提示词 | 设计 20 的 `umi-action-prompt/7` | DAS 夹爪沿用，没有开口标定就不加饱和那段 | 意见文字 | 若 DAS 有开口标定视频，按设计 20 写 `gripper_range` | — |

DEMO 的验收因此只能验「链路通、画得对、意见合理」，不能验精度；§7 第 1、2 项任一变成 `declared`，相应的注记自动去掉。

## 8. 测试与验收

**F5.17 叠加并入迷你播放器**

1. dataset2 判决模式任务：报告 Episode 明细与裁决卡的迷你播放器里（不选发现，或选中 EEF 发现，两路相机都在），两路外部相机按默认画出
   指尖中心、两指连线（带开口读数）、三根坐标轴、过去轨迹，接近轴 A 默认不画；观测默认开，绿十字与观测轨迹出现；打开残差线后
   ep6 相机 1 可见、相机 2 几乎重合；ep2 的两指连线与坐标轴明显转开，ep5 的标记整体落后画面约 5 帧；关掉叠加只剩原图。
2. 意见模式任务（不传夹爪参考）：同样的声明图层，没有观测组；「与送模型的一致」预设下只剩 P、A（红）、B 与过去轨迹，与送模型的片段是同一套标记。
3. 拖动、播放、逐帧、按发现跳转时标记跟帧：直连视频与帧包 / 浏览器解码各试一次，暂停与逐帧时和画面同帧；选中发现时该相机叠加加粗、其余变淡。
4. mcap：dataset2 的 mcap 孪生（码流从 GOP 中间开始）上，同一条 episode 的标记与 LeRobot 版落在同一画面帧上；首个关键帧之前没有标记。
5. 刷新页面后勾选保持；没勾 EEF 模块的任务不出叠加菜单、不提示；接口出错只提示一行，视频照放。
6. `EefOverlayVideo` 删除后，证据帧按钮打开迷你播放器、该相机在前并停在该帧；右侧「详细信息」有「叠加」一节；frontend README 的手动验证步骤改写；
   设计 07、12、18、20 的指向更新。
7. 测试：
   - 后端 `tests/eef/test_overlay.py` 覆盖坐标轴、未来轨迹、观测组、缺测断线、图层声明；
   - `tests/daemon` 覆盖 `times_s`（LeRobot 与 mcap，首个关键帧之前为 null，质检时钟 `offset_s ≠ 0` 不影响）、观测文件恢复、缓存键；`tests/viz` 覆盖扫描记下的消息时间；
   - 前端 Vitest：图层选择、默认与预设、按时间找帧与陈旧上限、contain 映射、EEF 发现的布局、MSW 处理器按契约校验。

**F5.18 `export-umi-mcap`**

1. `00001(1).mcap` 导出两手两路的 `trajectory.json`，平台校验通过（Schema、语义、媒体 topic 存在、投影重算）；
   `umi-export-report.json` 写出配对率、缺测段与低频段、内参来源、assurance；按缺省间隔插值后有位姿的帧 > 95%。
2. `umi_sample.mcap` 没有 `camera_info`：有 `intrinsics_fallback` 时导出并标回退，没有时该相机 `unsupported` 且说明原因。
3. 迷你播放器里每路画本手的中心点、两指连线、坐标轴、过去 / 未来轨迹；robot1 在 `00001(1).mcap` 第 900 帧附近轨迹从货架上方落到当前位置；
   缺测的帧不画、整条不报错；在右侧「详细信息」里改插值间隔，低频段的轨迹随之连上或断开。
4. `per_hand` 文件在旧版校验下被拒、新版通过；`shared` 旧文件行为不变（`tests/eef/test_umi.py` 回归）；`umi_calibration.schema.json` 的示例过 Schema。

**F5.19 自运动一致性与意见**

1. 正确标定：`00001(1).mcap` 两路 `ego_motion` ok，旋转差中位 < 2°，整条意见「好」。
2. 注入：位姿流晚 0.5 s → suspect、报出时间差约 0.5 s 与片段；`body_to_optical` 写错 → suspect、幅度档「重」；
   白墙段（`umi_sample.mcap` 开头）→ unknown、不报片段。
3. 自运动的问题出 info 级 `ego_motion_suspect`（模型意见仍是 `opinion_mismatch`），条目 keep / drop 不受影响；报告 EEF 小节列出片段、幅度档、
   证据帧与「按假设值」注记；契约测试与锁、回归工具对照表的一致性检查通过。
4. 回归：`tests/eef/test_egomotion.py` 用合成的旋转 / 平移序列与鱼眼投影造帧对（不依赖真数据），真数据用例按 dataset2 的惯例可跳过。

## 9. 实施记录

### 开工前评审（2026-10-08，v1.1）

对着代码把 v1.0 核了一遍，需求方定了下面几条，正文已改：

| # | 改动 | 原因 |
|---|---|---|
| 1 | 叠加的对时从「上传件 `timestamp_s` + `check_clock.offset_s`」改为「配对画面帧在播放器时钟上的显示时刻」（§3.2、§3.4），D74 措辞随之改 | 上传件时间线的零点与时钟源不保证与播放器一致；按画面帧对，任何数据都严格同帧，也不用解码（LeRobot 是帧号 / fps，mcap 用扫描已有的消息时间） |
| 2 | 去掉 `labels` 组，文字由前端的「标签」开关管 | 图层的 `kind` 里没有文字；保持 C4 只加字段 |
| 3 | 接近轴 A 默认关、显示换成紫色；加「与送模型的一致」预设（`in_model`、`model_color`） | A 与新的三轴 z 重合，红色又和 x 轴冲突；模型的意见文字可能说「红色箭头」 |
| 4 | EEF 发现的迷你布局改为所有带叠加的相机（至多 3 路） | 原布局只放一路相机加曲线，另一路的叠加看不到 |
| 5 | 迷你播放器一律请求叠加，404 静默 | 任务详情的流水线迷你版在没有结果版本时拿不到 `modules` |
| 6 | 显示时找样本帧加陈旧上限（1.5 个样本间隔）；直连视频用 `requestVideoFrameCallback` | 缺测段、低频段不把旧标记画到新画面上；播放中也逐帧对准 |
| 7 | F5.17 的 mcap 验收改用 dataset2 的 mcap 孪生（`make_mcap_twin.py`，码流从 GOP 中间开始）；`offset_s ≠ 0` 放在单测里 | 本机没有 dataset2 的 mcap 版，GenRobot 要等 F5.18 的导出器；孪生若让 action 晚开始，发现的色块会整体偏移，验收时容易误读 |
| 8 | 缺测放宽为「有数据的帧才画、才查」；低频段由读的一方按「插值最大间隔」插值，缺省 3 个样本间隔，可在「详细信息」里改（只影响画面） | `load_hands` 原要求每帧都有相机位姿，缺一帧整条失败；VIO 个别段 15–18 Hz 会隔帧缺测 |
| 9 | `per_hand` 写明样本级 `reference_frame` / `eef` 的填法与要放宽的三处校验 | 原文只提到 `load_hands`，`load.py` 的两处同参考系检查也会拒 |
| 10 | 内参走 `H_media_from_calibration`、不改写 K；导出写 `media.fps`、`frame_count`；首个关键帧前的消息不配对；`timestamp_s` 以质检时钟的锚点为零 | 格式现成的机制、标定可追溯；送模型要帧率；发现的色块落在画面的对应处 |
| 11 | `umi-calibration/2` 的 Schema 进契约目录；`calibration_suspect` / `timing_suspect` 在导出时算 | 它是交给客户填的生产输入；预检不解码视频 |
| 12 | 自运动的问题用新细码 `ego_motion_suspect`（MV-4，时间差同时覆盖 AV-1），写明 C1 / C2 / 前端的改动 | 复用 `opinion_mismatch` 会把 CPU 的结论说成模型意见 |
| 13 | §7 第 1、2、6 项先按假设做，驻场时当面确认；F5.18 / F5.19 不再等 §7 确认 | 需求方现在拿不到客户的数据 |
| 14 | 过去轨迹时长取送模型时的值、未来同长，不做成模块参数；坐标轴 6 cm，与现有接近轴一样长 | 少一次 C1 变更；与模型所见一致 |

（各 F 落地时在下面补：日期、提交、偏离本篇的地方。）
