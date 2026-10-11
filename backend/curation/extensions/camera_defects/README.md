# camera_defects · 镜头画面缺陷（花屏 / 抖动 / 镜头污染）

设计：`docs/design/13-video-native-vlm.md`「逐机位画面缺陷」。注册表：C1 1.14 起，
`ModuleSpec(id="camera_defects", rides_on="task_success")`；2.0（设计 17）起有三个细码 `glitch`（IMG-5）、`shake`（IMG-6）、
`contamination`（IMG-7），默认级别都是 info：只出结果，不影响判决。

## 它是什么

`task_success` 每条 episode 只发一次请求（D71），多机位连续视频一起看。答复里每路相机一项（`cameras`），每项带一个 `camera_check` 字段，
报告这路相机自己的三项画面缺陷；没有任务标注的条目不判成败，只发一次只问画面缺陷的请求（`CAMERAS_ONLY_PROMPT`，D73）。
这个模块把各路答案汇成每条 episode 一条记录。

- **不增加模型调用**：请求次数和发送的视频都不变，只是答复里每路相机多一个字段。
- **每次都问**：没有开关，界面上也不勾选。它是 `task_success` 的随附模块，宿主在跑它就出结果。
- **只出结果**：答成 `minor` / `severe` 的项各是一条 info 级发现（严重度 low / medium，范围是那路相机，带模型给的时间段），
  不影响 keep / drop / held，不提复核问题；没有一路相机答出某一项时，这一项记为评估不了（`model_no_answer`）。
- **不拖累宿主**：请求带 `response_format: {"type": "json_object"}` 让服务端约束出合法 JSON；万一 `camera_check` 还是写坏，
  解析时把它整块剪掉、保住五个核心字段，三项记 unknown 并在 `problems` 里写明。

三项：`glitch`（花屏：撕裂、块状、拖影、糊掉，或带伪影的画面冻结）、`shake`（相机本体晃动；腕部
相机随手臂运动属正常，只报超出的抖动）、`contamination`（镜头脏污、油污、水渍、遮挡物）。
每项四档 `none` / `minor` / `severe` / `unknown`，附时间段（口径同 `evidence`：episode 相对秒）和一句说明；
污染另给 `kind`（`dirt` / `smudge` / `water` / `obstruction` / `other`）。

## 代码在哪

| 位置 | 做什么 |
|---|---|
| `adapters/video_vlm.py` | `CAMERAS_PROMPT`（判定请求里要求每路相机一项，含 `camera_check`）、`CAMERAS_ONLY_PROMPT`（无标注条目只问画面缺陷）、三项的定义 `CAMERA_CHECK_DEFS`；`parse_cameras` / `parse_camera_check` 永不抛错，畸形只记 `problems`；键集合校验是「去掉 `cameras` 后等于五个核心字段」 |
| 本目录 `__init__.py` | `struct_from_task`（从 `task_success` 的 `detail["cameras"]` 读回，汇成一条记录）、`summary` / `table_rows`（报告小节与明细表） |
| `pipeline/check_stage.py` | `_vlm_modules` 在 `task_success` 之后补这条记录（无标注、内部出错的条目也有一条，三项 unknown）；`--resume` 时 `details.protocol` 不是 `camera-check/2` 的记录算过期、重做；熔断器不看随附模块 |
| `pipeline/findings.py` | `_camera_defects`：把记录换成发现（minor → low、severe → medium） |
| `contracts/modules.py` | 注册表条目与 `riders_of` / `with_riders`；`cli/check.py`、planner、`cli/preflight.py`、`daemon/`、`pipeline/reporting.py` 都经 `with_riders` 展开清单 |

记录的 `details`：`protocol`（`camera-check/2`）、`source="task_success.cameras"`、`cams`、`known`、`clean_ratio`
（已答项里判为 `none` 的比例）、`reason`（为什么是 unknown：成败判定没产生结果 / 没有任务标注且画面缺陷请求没有结果 / 判定请求没有按相机作答 /
模型没答 `camera_check`）、`items`（三项各一个跨机位最坏档，就是一个字符串）、`per_camera`（逐机位 `answered` / `error` / `problems`
与三项原答案，含时间段与污染种类）。逐机位明细只存这一份，报告与控制台都从它读。

## 手动验证步骤

```bash
# 1. 单测：解析器、汇总、报告
cd backend && ../.venv/bin/python -m pytest -q curation/tests/test_video_vlm.py curation/tests/test_camera_defects.py

# 2. 端到端（假模型）：不列出它也每条一条记录，请求次数不变，单独跑报参数错误，报告里有小节和表
cd backend && ../.venv/bin/python -m pytest -q tests/cli/test_camera_defects.py

# 3. 真数据：本机 Daemon 建任务，正常勾选（不必也不能勾它），跑完看
#    runs/<task>/checks/camera_defects/results.jsonl 每条三项都有值，
#    报告页有「镜头画面缺陷」小节，usage.jsonl 的请求数与不带它时相同
```
