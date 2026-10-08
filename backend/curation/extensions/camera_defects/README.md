# camera_defects · 镜头画面缺陷（花屏 / 抖动 / 镜头污染 / 视觉遮挡）与机位身份

设计：`docs/design/13-video-native-vlm.md`「逐机位画面缺陷」。注册表：C1 1.14 起，
`ModuleSpec(id="camera_defects", rides_on="task_success")`；2.0（设计 17）给它细码 `glitch`（IMG-5）、`shake`（IMG-6）、
`contamination`（IMG-7），4.1 加 `occlusion`（IMG-8）与 `identity`（MV-2），默认级别都是 info——只出结果，不影响判决
（1.x 的 `affects_dataset_verdict=False`）。

## 它是什么

`task_success` 判成败的那一次请求里，每路相机各有一份回答（D71）。这个模块让模型在每路回答里多给一个
`camera_check` 字段，报告这路相机自己的四项画面缺陷和它物理上是什么，然后把各路答案汇成每条 episode 一条记录。
没有任务标注的条目发的是只问画面缺陷的同一个问题（D73），记录由此照样产生。

- **不增加模型调用**：请求次数、媒体内容都不变，只多约 250 个输出 token。
- **每次都问**：没有开关，界面上也不勾选。它是 `task_success` 的随附模块——宿主在跑它就出结果。
- **只出结果**：记录永远是 `abstain`（`passed=score=null`），不进 keep / drop / held，不提复核问题。
- **不拖累宿主**：请求带 `response_format: {"type": "json_object"}` 让服务端约束出合法 JSON；万一还是写坏，
  解析时把 `camera_check` 整块剪掉、保住五个核心字段，各项记 unknown 并在 `problems` 里写明。

四项：`glitch`（花屏：撕裂、块状、拖影、糊掉，或带伪影的画面冻结）、`shake`（相机本体晃动；腕部
相机随手臂运动属正常，只报超出的抖动）、`contamination`（镜头脏污、油污、水渍、遮挡物贴在镜头上）、
`occlusion`（镜头干净，但操作区被镜头与场景之间的东西挡住：机械臂、机身、人、物件、线缆）。
每项四档 `none` / `minor` / `severe` / `unknown`，附时间段（口径同 `evidence`：episode 相对秒）和一句说明；
污染另给 `kind`（`dirt` / `smudge` / `water` / `obstruction` / `other`），遮挡另给 `source`（`arm` / `body` / `person` /
`object` / `cable` / `other`）和解析时算出的 `share`（时间段合计占这路窗口的比例）。

机位身份 `view`：`kind`（`wrist` / `fixed`）、`arm`（`left` / `right` / `single`）、`matches_name`（画面是否与相机命名相符，
只在明显矛盾时答 `no`；单臂或双目命名里的 left / right 答 `unknown`）、`same_as`（与另一路是同一份录像时写它的名字，
解析时核对必须是本次送的另一路）。整条的 `identity`：有一路 `no` 或 `same_as` → `inconsistent`；有一路 `yes` 且无人反对 →
`consistent`；否则 `unknown`（MV-2 记 unassessable）。

## 代码在哪

| 位置 | 做什么 |
|---|---|
| `adapters/video_vlm.py` | `CAMERA_CHECK_SHAPE` / `CAMERA_CHECK_DEFS`（拼进判定请求与无标注条目的请求）、`parse_camera_check`（永不抛错的解析，畸形只记 `problems`；算 `share`）、`parse_view`；协议号 `CAMERA_CHECK_PROTOCOL` |
| 本目录 `__init__.py` | `struct_from_task`（从 `task_success` 的 `detail["cameras"]` 读回，汇成一条记录；`identity_of`）、`summary` / `table_rows`（报告小节与明细表） |
| `pipeline/findings.py` | `_camera_defects`：minor / severe 各报一条发现（带机位、时间段、种类 / 来源 / 占比），`identity` 按机位报不符或同画面；没答的项记 `model_no_answer` |
| `pipeline/check_stage.py` | `_vlm` 在 `task_success` 之后补这条记录；`_todo` 按 `details.protocol != CAMERA_CHECK_PROTOCOL` 判待补跑；熔断器只盯影响判决的模块 |
| `contracts/modules.py` | 注册表条目与 `riders_of` / `with_riders`；`cli/check.py`、planner、`cli/preflight.py`、`daemon/`、`pipeline/reporting.py` 都经 `with_riders` 展开清单 |
| `tools/parity/fakevlm.py` | 假模型的 `_fake_camera_check`：按相机名与窗口给出每一档、漏一项、整个不答、一路与命名不符 |

记录的 `details`：`protocol`、`source="task_success.cameras"`、`cams`、`known`、`clean_ratio`
（已答项里判为 `none` 的比例）、`reason`（为什么是 unknown：成败判定没结果 / 没跑复核 / 模型没答）、
`items`（四项各一个跨机位最坏档，就是一个字符串）、`identity`、`per_camera`（逐机位 `answered` / `error` / `problems`、
四项原答案含时间段与种类 / 来源 / 占比、`view`）。逐机位明细只存这一份，报告与控制台都从它读。

## 手动验证步骤

```bash
# 1. 单测：解析器、汇总、发现、报告
cd backend && ../.venv/bin/python -m pytest -q curation/tests/test_video_vlm.py curation/tests/test_camera_defects.py tests/cli/test_findings.py

# 2. 端到端（假模型）：不列出它也每条一条记录，请求次数不变，单独跑报参数错误，报告里有小节和表
cd backend && ../.venv/bin/python -m pytest -q tests/cli/test_camera_defects.py

# 3. 真数据：本机 Daemon 建任务，正常勾选（不必也不能勾它），跑完看
#    runs/<task>/checks/camera_defects/results.jsonl 每条四项与 identity 都有值，
#    报告页有「镜头画面缺陷」小节（四项计数加机位身份计数），episode 明细里机位不符的那一路另起一行；
#    usage.jsonl 的请求数与不带它时相同
```
