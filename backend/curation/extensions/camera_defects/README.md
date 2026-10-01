# camera_defects · 镜头画面缺陷（花屏 / 抖动 / 镜头污染）

设计：`docs/design/13-video-native-vlm.md`「逐机位画面缺陷」。注册表：C1 1.14 起，
`ModuleSpec(id="camera_defects", rides_on="task_success")`；2.0（设计 17）给它三个细码 `glitch`（IMG-5）、`shake`（IMG-6）、
`contamination`（IMG-7），默认级别都是 info——只出结果，不影响判决（1.x 的 `affects_dataset_verdict=False`）。

## 它是什么

`task_success` 判成败时，每路相机本来就有一次「只看这路相机」的复核请求（设计 13）。这个模块
在那段 prompt 后面追加一问，让模型在同一份 JSON 里多给一个 `camera_check` 字段，报告这路相机
自己的三项画面缺陷，然后把各路答案汇成每条 episode 一条记录。

- **不增加模型调用**：请求次数、媒体内容都不变，只多约 150 个输出 token。
- **每次都问**：没有开关，界面上也不勾选。它是 `task_success` 的随附模块——宿主在跑它就出结果。
- **只出结果**：记录永远是 `abstain`（`passed=score=null`），不进 keep / drop / held，不提复核问题。
- **不拖累宿主**：请求带 `response_format: {"type": "json_object"}` 让服务端约束出合法 JSON；万一还是写坏，
  解析时把 `camera_check` 整块剪掉、保住五个核心字段，三项记 unknown 并在 `problems` 里写明。

三项：`glitch`（花屏：撕裂、块状、拖影、糊掉，或带伪影的画面冻结）、`shake`（相机本体晃动；腕部
相机随手臂运动属正常，只报超出的抖动）、`contamination`（镜头脏污、油污、水渍、遮挡物）。
每项四档 `none` / `minor` / `severe` / `unknown`，附时间段（口径同 `evidence`：episode 相对秒）和一句说明；
污染另给 `kind`（`dirt` / `smudge` / `water` / `obstruction` / `other`）。

## 代码在哪

| 位置 | 做什么 |
|---|---|
| `adapters/video_vlm.py` | `CAMERA_CHECK_PROMPT`（追加在每次逐机位复核的 prompt 后）、`parse_camera_check`（永不抛错的解析，畸形只记 `problems`）；键集合校验放宽为「去掉可选的 `camera_check` 后等于五个核心字段」 |
| 本目录 `__init__.py` | `struct_from_task`（从 `task_success` 的 `detail["video_reviews"]` 读回，汇成一条记录）、`summary` / `table_rows`（报告小节与明细表） |
| `pipeline/check_stage.py` | `_vlm` 在 `task_success` 之后补这条记录；`_todo` 按 `details.protocol != "camera-check/1"` 判待补跑；熔断器只盯影响判决的模块 |
| `contracts/modules.py` | 注册表条目与 `riders_of` / `with_riders`；`cli/check.py`、planner、`cli/preflight.py`、`daemon/`、`pipeline/reporting.py` 都经 `with_riders` 展开清单 |

记录的 `details`：`protocol`、`source="task_success.video_reviews"`、`cams`、`known`、`clean_ratio`
（已答项里判为 `none` 的比例）、`reason`（为什么是 unknown：成败判定没结果 / 没跑复核 / 模型没答）、
`items`（三项各一个跨机位最坏档，就是一个字符串）、`per_camera`（逐机位 `answered` / `error` / `problems`
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
