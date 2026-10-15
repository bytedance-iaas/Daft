# camera_defects · 镜头画面缺陷（花屏 / 抖动 / 镜头污染 / 视觉遮挡）与机位身份

设计：`docs/design/13-video-native-vlm.md`「逐机位画面缺陷」。注册表：C1 1.14 起，
`ModuleSpec(id="camera_defects", rides_on="task_success")`；2.0（设计 17）起有细码 `glitch`（IMG-5）、`shake`（IMG-6）、
`contamination`（IMG-7），7.2 起加 `occlusion`（IMG-8）与 `identity`（MV-2），默认级别都是 info：只出结果，不影响判决。

## 它是什么

`task_success` 每条 episode 只发一次请求（D71），多机位连续视频一起看。答复里每路相机一项（`cameras`），每项带一个 `camera_check` 字段，
报告这路相机自己的四项画面缺陷和它物理上是什么。记录里另有一个 `arms` 块，说各路相机骑的是不是数据集给它的那条臂——
那一块不问模型，是在 CPU 上量出来的（见下）。
没有任务标注的条目不判成败，只发一次只问画面缺陷的请求（`CAMERAS_ONLY_PROMPT`，D73）。
这个模块把各路答案汇成每条 episode 一条记录。

四项是花屏、抖动、镜头污染、视觉遮挡。**遮挡单独给三个数**：`coverage`（最严重时遮住画面的比例）、`region`（遮挡物在画面里的
位置）、`duration_s`（被挡住的总秒数）；提示词里逐条写明光照变化、机器人自己的手臂与搬运物、只是在场景里的人和物、
对着别处的画面都不算遮挡。**左右臂、机位身份串线**（`arms`）一路相机看不出来，而且模型也看不准：手持夹爪的另一只夹爪在它收到的画面里只有二十来个
像素。改成实测（`pipeline/arm_pairing.py`）：腕部相机拧在手上，那只手一动画面就晃，所以比一比每路相机的画面位移和
数据集记的各臂速度——只取一条臂单独在动的时刻，整条 episode 做一次「两路相机配两条臂」的配对，赢出余量才下结论，否则 `unknown`。
anchor-arms-fastumi 上 14 条对调抓到 12 条、不发一次请求、48 个视频 11 秒；模型版同一批只认出 1 条。

- **不增加模型调用**：请求次数和发送的视频都不变，只是答复里每路相机多一个字段。
- **每次都问**：没有开关，界面上也不勾选。它是 `task_success` 的随附模块，宿主在跑它就出结果。
- **只出结果**：答成 `minor` / `severe` 的项各是一条 info 级发现（严重度 low / medium，范围是那路相机，带模型给的时间段），
  不影响 keep / drop / held，不提复核问题；没有一路相机答出某一项时，这一项记为评估不了（`model_no_answer`）；
  左右臂配对量不出结论时 MV-2 记 `measurement_inconclusive`，数据集本来就没有可比的两条臂时记 `not_applicable`（注册表 8.2）。
- **不拖累宿主**：请求带 `response_format: {"type": "json_object"}` 让服务端约束出合法 JSON；万一 `camera_check` 还是写坏，
  解析时把它整块剪掉、保住五个核心字段，三项记 unknown 并在 `problems` 里写明。

四项：`glitch`（花屏：撕裂、块状、拖影、糊掉，或带伪影的画面冻结）、`shake`（相机本体晃动；腕部
相机随手臂运动属正常，只报超出的抖动）、`contamination`（镜头脏污、油污、水渍、遮挡物贴在镜头上）、
`occlusion`（镜头干净，但操作区被镜头与场景之间的东西挡住：机械臂、机身、人、物件、线缆）。
每项四档 `none` / `minor` / `severe` / `unknown`，附时间段（口径同 `evidence`：episode 相对秒）和一句说明；
污染另给 `kind`（`dirt` / `smudge` / `water` / `obstruction` / `other`），遮挡另给 `source`（`arm` / `body` / `person` /
`object` / `cable` / `other`）和解析时算出的 `share`（时间段合计占这路窗口的比例）。

机位身份 `view`：`kind`（`wrist` / `fixed`，看画面是否随臂摆动）、`arm`（固定机位主要看哪条臂；腕部相机一律 `unknown`，
哪条臂由 CPU 量）、`matches_name`（看到的**安装方式**与数据集声明是否矛盾，只在明显矛盾时答 `no`，数据集没说就 `unknown`）。
整条的 `identity`：有一路 `no`，或量出左右臂串线 → `inconsistent`；有一路 `yes` 且无人反对 → `consistent`；
否则 `unknown`（MV-2 记 unassessable，原因是量不出结论还是数据集没有可比的两条臂，见上）。

## 代码在哪

| 位置 | 做什么 |
|---|---|
| `adapters/video_vlm.py` | `CAMERAS_PROMPT`（判定请求里要求每路相机一项，含 `camera_check`）、`CAMERAS_ONLY_PROMPT`（无标注条目只问画面缺陷）、三项的定义 `CAMERA_CHECK_DEFS`；`parse_cameras` / `parse_camera_check` 永不抛错，畸形只记 `problems`；键集合校验是「去掉 `cameras` 后等于五个核心字段」 |
| 本目录 `__init__.py` | `struct_from_task`（从 `task_success` 的 `detail["cameras"]` 读回，汇成一条记录）、`summary` / `table_rows`（报告小节与明细表） |
| `pipeline/check_stage.py` | `_vlm_modules` 在 `task_success` 之后补这条记录（无标注、内部出错的条目也有一条，各项 unknown），并调 `_arm_pairing` 量出 `arms`；`--resume` 时 `details.protocol` 不是当前协议号的记录算过期、重做；熔断器不看随附模块 |
| `pipeline/arm_pairing.py` | 左右臂、机位身份（MV-2）的实测：画面位移（相位相关）对各臂速度，只取一条臂单独在动的时刻，整条 episode 做一次配对，差距不够答 `unknown` |
| `pipeline/dataset_claim.py` | 数据集自己的说法：几条臂、每条臂占哪几列动作通道、每路相机声明的 `mount` / `owner` |
| `pipeline/findings.py` | `_camera_defects`：把记录换成发现（minor → low、severe → medium） |
| `contracts/modules.py` | 注册表条目与 `riders_of` / `with_riders`；`cli/check.py`、planner、`cli/preflight.py`、`daemon/`、`pipeline/reporting.py` 都经 `with_riders` 展开清单 |
| `tools/parity/fakevlm.py` | 假模型的 `_fake_camera_check`：按相机名与窗口给出每一档、漏一项、整个不答、一路与数据集的说明不符 |

记录的 `details`：`protocol`（`camera-check/10`）、`source="task_success.cameras"`、`cams`、`known`、`clean_ratio`
（已答项里判为 `none` 的比例）、`reason`（为什么是 unknown：成败判定没产生结果 / 没有任务标注且画面缺陷请求没有结果 / 判定请求没有按相机作答 /
模型没答 `camera_check`）、`items`（四项各一个跨机位最坏档，就是一个字符串）、`identity`（整条的机位身份）、
`arms`（量出来的左右臂配对：`identity`、`crossed`、`basis="motion_pairing"`、`readings`——各臂单独在动的时刻数、
每路相机对每条臂的读数、赢出的余量）、`per_camera`（逐机位 `answered` / `error` / `problems` 与四项原答案，
含时间段、污染种类、遮挡的比例 / 位置 / 时长，以及 `view`）。逐机位明细只存这一份，报告与控制台都从它读。

## 手动验证步骤

```bash
# 1. 单测：解析器、汇总、左右臂配对、发现、报告
cd backend && ../.venv/bin/python -m pytest -q curation/tests/test_video_vlm.py curation/tests/test_camera_defects.py \
    curation/tests/test_arm_pairing.py tests/cli/test_findings.py

# 2. 端到端（假模型）：不列出它也每条一条记录，请求次数不变，单独跑报参数错误，报告里有小节和表
cd backend && ../.venv/bin/python -m pytest -q tests/cli/test_camera_defects.py

# 3. 真数据：本机 Daemon 建任务，正常勾选（不必也不能勾它），跑完看
#    runs/<task>/checks/camera_defects/results.jsonl 每条四项与 identity 都有值，
#    报告页有「镜头画面缺陷」小节（四项计数加机位身份计数），episode 明细里机位不符的那一路另起一行；
#    usage.jsonl 的请求数与不带它时相同
# 4. 左右臂配对：对双臂数据集（tos://curation-clip-check/anchor-arms-fastumi，ep0–13 两路相机文件对调、ep14–23 正常）
#    建任务，看 camera_defects 记录里的 arms：对调的条目 identity=crossed 并点名相机，正常的 consistent，
#    量不出来的 unknown 带原因；单臂数据集（DROID）这一块恒为 unknown，不报错
```
