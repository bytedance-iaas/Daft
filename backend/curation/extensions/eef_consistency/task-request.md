# 用对外的 Task API 跑 EEF–视频一致性

接口都在 `{base}/api/v1` 下（生产 `{base}` 是 `/curation`），契约是 `docs/contracts/openapi.yaml`（C4）。
开了鉴权的站点用 HTTP Basic（`-u 用户名:密码`）；本机 `CURATOR_AUTH_MODE=none` 时不用。下面的 `$B` 是
`https://<站点>/curation/api/v1`，本机是 `http://127.0.0.1:8080/curation/api/v1`。

一共三步：**预检 → 建任务（`POST /tasks`）→ 查进度和结果**。只给数据集地址就行，EEF 需要的轨迹由平台自己生成（见文末「轨迹从哪里来」），不用上传任何文件。

## 1. 预检

```bash
curl -s -u "$USER:$PASS" -X POST "$B/preflight" -H "Content-Type: application/json" -d '{
  "input": {"source": "tos", "uri": "tos://dongmao-test/umi-datasets/trossen-example-cube-into-box/lerobot",
            "region": "cn-beijing", "credential": "dongmao-test"},
  "vlm_backend": "umi-ark"
}'
# → {"preflight_id": "pf-…", "expires_at": …, "result": {...格式、episode 数、相机...}}
```

- `input.source`：`tos`（私有 TOS，`credential` 是在控制台登记的密钥名）、`local`（站点开放的本地挂载路径，`uri` 写路径）等。
- `preflight_id` 有有效期，建任务时要带上。
- 结果里 `modules` 每个模块一项：EEF 是 `available` 就能跑（`notes` 里写着「trajectory generated from the dataset's state and camera
  calibration」）；数据集里没有末端位姿与相机标定时是 `unsupported`，原因写在 `reason`，这时不勾它就行。

## 2. 建任务：`POST /tasks`

```bash
curl -s -u "$USER:$PASS" -X POST "$B/tasks" -H "Content-Type: application/json" -d '{
  "name": "Trossen cube-into-box · EEF",
  "input": {"source": "tos", "uri": "tos://dongmao-test/umi-datasets/trossen-example-cube-into-box/lerobot",
            "region": "cn-beijing", "credential": "dongmao-test"},
  "output": {"uri": "tos://umi-local/trossen-tos", "credential": "umi-local-output"},
  "preflight_id": "pf-…",
  "episodes": {"mode": "all"},
  "modules": [
    "data_integrity", "timestamp_check", "motion_quality", "visual_quality", "video_action_sync",
    "task_success", "dedup", "eef_video_consistency"
  ],
  "vlm": {"backend": "umi-ark", "model": "doubao-seed-2-1-lite-260915", "reasoning_effort": "minimal"},
  "params": {"start_now": true, "vlm_hedge": false}
}'
# → {"id": "task-…", "state": "queued", "warnings": [], "links": {...}}
```

| 字段 | 说明 |
|---|---|
| `name`、`input`、`output`、`preflight_id`、`episodes`、`modules` | 必填 |
| `output` | 交付位置（`tos://…`）和它的密钥名 |
| `episodes` | `{"mode": "all"}`；也可以按范围或清单选 |
| `modules` | 模块 id 的列表；带参数的写成 `{"id": …, "params": {…}}`。只跑 EEF 就只写 `"eef_video_consistency"` |
| `data_integrity.params` | `full_read`（整读校验，缺省关）、`decode_test`（逐帧解码，缺省关） |
| `vlm` | 后端名、模型名、推理强度；不写用站点缺省 |
| `params.start_now` | 建完就开跑（缺省 true），开跑前先做三项检查：输入可读、交付可写、模型可用，不过返回 `precheck_failed` |
| `params.policy` | `{"preset": "default"}`（缺省，按细码的默认级别判）或 `"report_only"`（只报不拒） |
| 请求头 `Idempotency-Key` | 可选；同一个键重复提交只建一个任务 |

## 3. 查进度和结果

```bash
curl -s -u "$USER:$PASS" "$B/tasks/task-…"                      # state: queued → running → succeeded / failed …；summary 是通过 / 拒绝 / 待定条数
curl -s -u "$USER:$PASS" "$B/tasks/task-…/episodes/0"           # 一条 episode 各模块的记录：modules.eef_video_consistency
curl -s -u "$USER:$PASS" "$B/tasks/task-…/report"               # 整份质检报告
curl -s -u "$USER:$PASS" "$B/tasks/task-…/episodes/0/eef-overlay"   # 投影叠加的图层（播放器在原视频上画）
```

进度也可以订阅 SSE（`{base}/events`）。EEF 的结论在 `modules.eef_video_consistency` 的 `findings` 和 `details.opinion`
（每路相机的模型总结与不符的段），UMI 手持夹爪只出意见，不参与判决。

## 用 Python

```python
import json, urllib.request

B = "http://127.0.0.1:8080/curation/api/v1"

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data, {"Content-Type": "application/json"}, method=method)
    return json.load(urllib.request.urlopen(req, timeout=300))

inp = {"source": "tos", "uri": "tos://dongmao-test/umi-datasets/trossen-example-cube-into-box/lerobot",
       "region": "cn-beijing", "credential": "dongmao-test"}
pf = call("POST", "/preflight", {"input": inp, "vlm_backend": "umi-ark"})
task = call("POST", "/tasks", {
    "name": "Trossen · EEF", "input": inp,
    "output": {"uri": "tos://umi-local/trossen-tos", "credential": "umi-local-output"},
    "preflight_id": pf["preflight_id"], "episodes": {"mode": "all"},
    "modules": ["eef_video_consistency"],
    "vlm": {"backend": "umi-ark", "model": "doubao-seed-2-1-lite-260915", "reasoning_effort": "minimal"}})
print(task["id"])
```

（开了鉴权的站点给请求加 `Authorization: Basic …` 头。）

## 轨迹从哪里来

EEF 要把每帧末端（每只手）的位置和朝向投影到视频上，需要两样东西：每帧的末端位姿，和相机标定。平台在任务跑到 EEF 时从数据集里自己算出来
（`extensions/eef_consistency/derive.py`），写进任务的运行目录 `inputs/eef/trajectory.json`，模型请求和报告里的投影叠加都用这一份：

- **手持夹爪数据集**（UMI / TRUMI 导出的 LeRobot，`robot_type` 为 `umi_*`）：`observation.state` 里每只手有 `robotN_pos_x/y/z`（TCP 位置，米）、
  `robotN_rot6d_0..5`（姿态，旋转矩阵的前两列）、`robotN_gripper_width`（开口）；`meta/umi_calibration.json` 里每路相机有内参 `K`、
  鱼眼畸变、标定图尺寸和 `T_camera_tcp`（相机到 TCP 的固定变换）。每帧的手位姿由位置和 rot6d 还原成 4×4 矩阵；腕部相机的位姿 =
  手位姿 × `T_camera_tcp` 的逆；视频尺寸与标定图尺寸的比例写成媒体变换。
- **UMI / TRUMI 原始会话**（根目录 `dataset_plan.pkl` + `demos/`，SLAM 流水线跑完的样子）：数据集地址直接填会话目录。平台不转码，
  手位姿取自 plan，标定从会话里取（`tx_slam_tag.json`、SLAM 日志或内参文件里的内参、plan 与相机轨迹 CSV 反推的相机到 TCP 变换），
  视频直接读 demo 的 `raw_video.mp4`。这种数据集只能勾 `eef_video_consistency`。
- **数据集自带 `trajectory.json`**（离线导出工具写在数据集根目录的）：没有上面那些数据时直接用它。
- 两样都没有：EEF 对这个数据集不可用，预检写明原因。

`params.trajectory_json` 仍然可以传（换一份轨迹时），一般不用。
