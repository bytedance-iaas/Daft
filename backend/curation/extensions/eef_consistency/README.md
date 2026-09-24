# EEF–视频一致性（DEMO 模块，阶段 5）

接入 v2（D49，F5.9）：一个参与判决的模块 `eef_video_consistency`（vlm 档、一票否决、只跑前面没被判废的条目）：每条先由 CPU 测量，
再请模型复核（一点一轴的请求包），`decide.py` 按设计 12 附录 C.9 给出判过、判废或转人工；命令行里它和任务成败判定同在 vlm 档，
由 `pipeline/check_stage.py` 的 `StageRun` 逐条调用 `backend/curation/cli/eef_check.py` 的 `EefJudge`（流水线模式下同样逐条交接），
模块参数 `--param` 在 `backend/curation/cli/modparams.py`；`aggregate` 在调用边界把它作为一票否决项加进 v1 的判决配置（A 类不动）。
（F5.4–F5.8 期间是两个建议性模块 `eef_video_consistency` / `eef_video_review`，不影响判决。）

设计：[docs/design/12-eef-video-consistency.md](../../../../docs/design/12-eef-video-consistency.md)；
输入契约：[docs/contracts/eef/](../../../../docs/contracts/eef/)（`eef-video/1.0.0` 四段 + `trajectory-bundle/1.0` 单文件容器）。
本目录是 B 类新代码，不碰 A 类目录；检测器与观测 provider 只读白名单输入，真值只在仓库外的离线评估器里读。

| 文件 | 内容 |
|---|---|
| `contracts.py` | 枚举、分项、状态、原因码、`Issue`（样本 / 帧 / 相机 / 点 / JSON 路径级定位） |
| `load.py` | `trajectory.json` 读取与校验：严格 JSON → 真值键拒绝 → 容器 Schema → 四段 Schema → 跨段语义 → 媒体存在 → 提供投影与重算投影之差（超 0.05 px 记 `input_inconsistent` 警告，两份都保留、不覆盖） |
| `geometry.py` | SE(3)、四元数、工具点（`fixed` / `linear_gripper`）、针孔 / Brown(5) / 鱼眼(4)、H 变换、投影链 |
| `timeline.py` | 主时间线、线性插值与 SLERP、零阶保持、按缺口分段、按 lag 平移轨迹 |
| `capability.py` | 逐分项能力预检（相机级、episode 级、数据集级）；文件里没有的 episode 报 `unsupported: projection_missing` |
| `motion.py` | L0 数值轨迹：折叠重采样重复帧、按机器人时钟的真实 Δt、线 / 角速度、稳健尖峰、2 Hz 以上高频能量与 1 秒滚动 RMS |
| `video.py` | 按真实 PTS 流式解码一路视图的片段：v3 拼接视频从 `clip_start_s` 起 seek，帧号从片段第一帧记 0，不留整段 |
| `mcap_media.py` | mcap 数据集（F5.13）：`media.topic` 指定的图像 topic 按 log_time 顺序转成本地 mp4（JPEG 封装、H.264 重封装，不重编码；复用 `ingest/mcap_reader` 的做法），帧号就是消息序号；一条 episode 判完即删。`observations.media_frames` 是两种媒体共用的取帧入口 |
| `observations.py` | 独立观测：`ObservationProvider` 协议、provider 输入白名单（只有媒体位置、点 id、种子，没有投影 / 位姿 / 标定）、种子文件读取校验、观测文件（observation Schema） |
| `template.py` | 夹爪外观模板（`gripper-template/1.0`，F5.8）：读取与校验（Schema、PNG 小图与掩膜解码、每条目 ORB 特征）；`Redetector` 在整幅画面里按相机匹配条目（比率检验 + RANSAC 相似变换、内点下限与比例、尺度范围、歧义放弃、固定随机种子）；`rigid_member_mask` 用跟踪器的成员规则从两个锚点间的帧算夹爪掩膜；`make_entry` / `make_template` / `check` |
| `tracking.py` | P-A provider：种子锚点 + 夹爪刚体特征簇的金字塔 LK（逐步前后向校验）；成员由前后两个锚点决定（受种子约束的 RANSAC），逐帧相似变换带种子走，远端锚点的已知误差线性校正，前后向一致才采纳；遮挡、失跟、分歧一律 `uncertain`，不插值冒充观测 |
| `metrics.py` | 位置残差（像素与毫米等效）、方向夹角（有向 0–180°、无向 0–90°，投影过短为 not_observable）、全局与滑窗 lag（`u_visual(t) ≈ u_declared(t+lag)`，同一掩码、去常量偏移、亚帧抛物线）、残差高频占比 |
| `motion.py`（续） | 背景 / 相机运动：半分辨率逐帧背景特征 + RANSAC 相似变换累积成画面轨迹（遮掉独立观测到的夹爪，不用投影），1 Hz 以上滚动 RMS |
| `segments.py` | 迟滞分段（开 / 关阈值、最短持续、允许短缺口）、滚动中位数、证据帧挑选 |
| `assess.py` | 分项状态：`ok / suspect / unknown / unsupported / error`；无 profile 只出曲线（`threshold_uncalibrated`），覆盖不足 `unknown`；episode 级只做「任一相机 suspect 即候选」汇总 |
| `diagnosis.py` | 诊断假设（只在对应分项已 suspect 时算，不改状态）：PnP 外参修正、时间偏移、恒定朝向错（三维拟合 EEF 本体系恒定旋转）、TCP 轴向偏移（一维拟合）、位姿漂移、抖动来源 |
| `profile.py`、`profiles/demo.yaml` | 阈值 profile；`demo` 标 `calibrated: false`，每个数都注明来自哪条基准的噪声底、乘了多少倍 |
| `runner.py` | 单 episode 的流式执行：解码 → 观测 → 测量 → 判定 → 诊断 → 产物（`observations/`、`curves/*.parquet`、`evidence/` 叠加图），输出 §11.3 的 `detail` |
| `decide.py` | 逐条判决（D-E12 / 附录 C.9）：模型能看的分项（位置、朝向）按分项多数意见与 CPU 比对，模型看不了的分项（时间对齐、状态运动、相机运动）CPU 可疑即转人工，文件里没有或位置到处无法评估的转人工；有确认的判废即判废，否则有转人工理由即转人工，否则判过 |
| `preflight.py` | `curation preflight` 里的模块条目：文件校验、逐分项能力表、按 episode 计数；没给文件报 `needs_input: trajectory_missing`（`input_hint.field = trajectory_json`，F5.5 起控制台第二屏上传）；复核模块跟随它复核的模块（不可用报 `eef_base_unavailable`、缺文件同样要上传），再要 VLM 后端 |
| `review.py` | VLM 复核：窗口（同分项、时间重叠的 CPU 位置 / 朝向候选段合并成候选窗口，加均匀抽查窗口，每路相机各至多 N 个、超出记 `truncated`）；**每个窗口只问一个点 P 和至多一根轴 A**（候选窗口问 CPU 偏得最厉害的点 / 轴，抽查窗口问覆盖最好的点；轴在窗口里投影不足 20 px 就换最长的一根，都不够就不问朝向）；请求包：缩小的整帧、每帧原始裁剪与标记裁剪（声明的 P 红圈、跟踪到的 P 绿十字、声明的 A 红箭头，都标名字），prompt 只给这一点一轴的定义；答复校验（`eef/review_output.schema.json` 1.1、帧号必须来自请求、解释里不许有测量值，不合格给一次修复）、按发送内容缓存、`votes` 把答复变成分项投票 |
| `report.py` | 报告小节摘要：判过 / 判废 / 转人工条数、转人工的原因、判废来自哪些分项、模型与 CPU 的一致率，以及候选、各分项可疑 / 无法评估的条数、被支持的诊断、覆盖率、复核窗口（有答复 / 失败、冲突）；四张表 `eef_camera_metrics` / `eef_segments` / `eef_diagnosis` / `eef_review_windows` |
| `adapters/` | `unified_sample`（三文件目录 ↔ 单文件包条目）、`world_policy`（客户 World_Policy 参考样本 → 形态 C / 纯图像）、`lerobot_mapping`（按显式的 `eef-mapping/1.0` 映射从 LeRobot 列生成 `trajectory.json`，形态 B，设计 §3.3；不是平台入口） |
| `__main__.py` | 离线命令：`validate`、`run`（`--seeds` 或 `--template`）、`export`、`template-build`、`template-check` |

## 手动验证

在 `backend/` 下执行（DEMO 数据在仓库外 `~/ws/ws_general/galbot/`，可用 `CURATOR_EEF_DEMO_DATA` 改位置；
没有这份数据时相关测试自动跳过）：

1. 单测：`../.venv/bin/python -m pytest -q tests/eef`，应全部通过（本机有 DEMO 数据时约 15 秒）。
2. 校验 dataset2 的上传件并看能力表：

   ```bash
   ../.venv/bin/python -m curation.extensions.eef_consistency validate \
     --trajectory ~/ws/ws_general/galbot/dataset2/trajectory.json \
     --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 --all-points-observable
   ```

   应看到 `report.valid: true`、`total_frames: 2009`、`total_points_checked: 28126`、
   `max_reprojection_difference_px` 约 0.013；`capability.availability: available`，七条 episode 都是 `available`。
   去掉 `--all-points-observable` 时位置 / 方向两项是 `needs_input: observation_seed_missing`（还没给观测种子）。
   dataset1 换成 `dataset1/trajectory.json` 与 `dataset1/eef_ds1_lr2`：11 条、3344 帧、13376 个投影点、差约 3e-13 px。
3. 真值隔离：把 `trajectory.json` 复制一份，在任意位置加一个 `"truth": {}` 键再跑 `validate`，
   应退出码 1，`report.errors[0].code` 为 `forbidden_key` 并给出 JSON 路径。
4. 自洽不覆盖：把某帧某点的 `uv_px` 改动 3 px，`validate` 仍通过，但 `report.warnings` 里有一条
   `input_inconsistent`，带相机、点与帧号。
5. 独立观测报告（F5.2，约 1 分钟；在仓库根执行）：

   ```bash
   PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.observation_report
   ```

   逐 episode × 相机打印「点=可见比例/对真值 P95 像素」，最后是汇总与独立性实验：25 路的可见比例中位数约 0.66、
   P95 误差中位数约 3.2 px；两条 `observations_identical: true`（投影平移 30 px，观测逐位不变）。
   种子与真值都是 `synthetic_fixture`，这些数只说明跟踪器在 DEMO 数据上的表现，不是精度验收。
6. 离线评估一条 episode（F5.3，在 `backend/` 下）：

   ```bash
   ../.venv/bin/python -m curation.extensions.eef_consistency run \
     --trajectory ~/ws/ws_general/galbot/dataset2/trajectory.json \
     --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 \
     --seeds ~/ws/ws_general/galbot/dataset2/observations_seed --episodes 0 5 6 --out /tmp/eef_run
   ```

   每条打印 `overall` 与分项状态：ep0 全 `ok`（`overall: assessed`）；ep5 `temporal_alignment: suspect`；
   ep6 `position_2d: suspect`。`/tmp/eef_run/details.jsonl` 里 ep6 的 `cameras` 只有 `27432424_left` 的位置是 suspect，
   `diagnosis` 里 `extrinsics_error` 为 `supported: true`、`delta_translation_mm` 约 30、`delta_rotation_deg` 约 2；
   ep5 两路的 `lag_s` 约 +0.33。`evidence/000006/27432424_left/*.jpg` 上红圈是声明投影、绿叉是独立观测。
   `--profile none` 时所有分项都是 `unknown`（`threshold_uncalibrated`），只出曲线。
7. 受控异常矩阵（F5.3 验收，约 2 分钟；在仓库根执行）：
   `PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.matrix`，应打印 18 行 `PASS`、`"cells_ok": 114`
   与 4 行轻重档 `PASS`；说明见 [tools/eef_eval/README.md](../../../../tools/eef_eval/README.md)。
8. 在 v2 命令行链路上跑（D49，在 `backend/` 下；需要一个 VLM 后端，下面用方舟预设 `ark`，环境变量 `ARK_API_KEY` 里放密钥）：

   ```bash
   G=~/ws/ws_general/galbot/dataset2; R=/tmp/eef_cli; mkdir -p $R
   P="--param eef_video_consistency.trajectory_json=$G/trajectory.json"
   ../.venv/bin/python -m curation.cli preflight --json --input $G/eef_ds2_lr3 --vlm-backend ark --modules timestamp_check,eef_video_consistency $P > $R/preflight.json
   ../.venv/bin/python -m curation.cli plan --preflight $R/preflight.json --modules timestamp_check,eef_video_consistency --episodes 0-6 --out $R/plan.json
   ../.venv/bin/python -m curation.cli check --modules timestamp_check --input $G/eef_ds2_lr3 --run-dir $R --episodes 0-6 --survivors-out $R/numeric.txt
   ../.venv/bin/python -m curation.cli check --modules eef_video_consistency --input $G/eef_ds2_lr3 --run-dir $R --episodes @$R/numeric.txt $P --vlm-backend ark
   ../.venv/bin/python -m curation.cli aggregate --run-dir $R --phase funnel --revision 1 --episodes 0-6
   ../.venv/bin/python -m curation.cli aggregate --run-dir $R --phase final --revision 1 --episodes 0-6 --input $G/eef_ds2_lr3
   ../.venv/bin/python -m curation.cli report --run-dir $R --revision 1
   ```

   `preflight.json` 里 `eef_video_consistency` 是 `available`，带 `subitems` 与 `episode_counts: {available: 7}`（不给 `--param` 时是
   `needs_input: trajectory_missing`，给了文件但不给 `--vlm-backend` 时是 `needs_input: vlm_backend_missing`）；`plan.json` 的 `vlm`
   阶段是 `eef_video_consistency`、`episodes: survivors:numeric`、`hard_gates` 里有它；EEF 的 `check` 按条打印通过 / 判废 / 弃权
   （弃权就是转人工），每条记录的 `details.decision` 写着结论和理由，`details.review` 是每个复核窗口（问的点与轴、模型答复）；
   `revisions/r0001/verdicts.jsonl` 里被它判废的条目是 `drop`，理由以「未通过「EEF–视频一致性」」开头；`report.md` 的
   「EEF–视频一致性」一节写判过 / 判废 / 转人工条数、转人工的原因、CPU 可疑分项和模型与 CPU 的一致率；`tables/` 下有
   `eef_camera_metrics`、`eef_segments`、`eef_diagnosis`、`eef_review_windows` 四张表。没有 VLM 后端时，第 10 步的测试用假模型走同一条路。
9. 控制台上传与 Daemon 执行（F5.5 / F5.9）：先跑 `../.venv/bin/python -m pytest -q tests/orchestr/test_eef_tasks.py -m "slow or not slow"`
   （约 45 秒，含一个真跑 CLI 的端到端任务），应全部通过。再真起 Daemon（仓库根的 `.claude/launch.json` 里的
   `curator-daemon-eef`：开发用主密钥、不鉴权、本地数据根是 `~/ws/ws_general/galbot/dataset2`），在 `backend/` 下：

   ```bash
   G=~/ws/ws_general/galbot/dataset2; U='http://localhost:8080/curation/api/v1/uploads'
   curl -s -X POST "$U?kind=eef_trajectory&name=trajectory.json" -H 'Content-Type: application/json' \
     --data-binary @$G/trajectory.json | python3 -m json.tool | head -30
   sed 's/"status": "valid"/"status": "valid", "truth": 1/' $G/trajectory.json > /tmp/bad.json
   curl -s -X POST "$U?kind=eef_trajectory&name=bad.json" -H 'Content-Type: application/json' --data-binary @/tmp/bad.json \
     | python3 -m json.tool | head -20
   cat $G/observations_seed/*/*.jsonl > /tmp/seeds.jsonl
   ```

   第一次上传返回 201，`handle` 是 `upload:upl-…`（9 位小写字母，D45），`validation.summary` 里 `samples: 7`、`frames: 2009`、
   `max_reprojection_difference_px` 约 0.013；第二次返回 400 `validation_failed`，`details.errors` 每条带
   `field`（JSON 路径）、`sample_id`、`frame_index`、`camera_id`、`point_id`，`code: forbidden_key`。
   然后在浏览器打开 <http://localhost:8080/curation/tasks/new>：数据来源选本地路径 `eef_ds2_lr3`，「快速质检」不会勾上
   「EEF–视频一致性」（第一屏的卡片上不再提示要上传的文件，2026-09-24 第四轮），手动勾上后模型配置出现（它要用模型复核）；第二屏的
   trajectory.json 选 `$G/trajectory.json`（按钮先写「上传中…」、文件发完写「校验中…」，通过后显示文件名、sha256 前 12 位与摘要），
   「夹爪参考」下拉默认是「观测种子」，选 `/tmp/seeds.jsonl`（`.jsonl` 由控制台转成 JSON 数组），另有复核窗口数、每窗口帧数两项；
   先选 `/tmp/bad.json` 能看到逐条定位的错误。
   观测种子与夹爪外观模板必须给一个（注册表 1.10 的 `x-choice-group`，表单里合成「夹爪参考」一项）：都不给时第二屏报
   「请上传夹爪参考（观测种子或夹爪外观模板，二选一）」，Daemon 也拒收
   （预检 `needs_input: observation_seed_missing`）——没有它们就找不到画面里的夹爪，每一条都只能转人工。两个文件可以同时上传，
   后传完的不会把先传完的冲掉（2026-09-24 在 galbot 上遇到过：trajectory.json 大、后传完，种子的句柄丢了）。
   创建并开始后：任务的运行目录有 `inputs/uploads.json` 与两份文件副本，`plan.json` 的 `vlm` 阶段有这个模块，报告里有
   「EEF–视频一致性」一节；被它判废的条目进拒绝清单。直接在 `modules[].params` 里填服务器路径会被 400 拒收（Daemon 只认 `upload:` 句柄）。
10. 判决口径与复核（F5.9 / F5.10）：`../.venv/bin/python -m pytest -q tests/eef/test_decide.py tests/eef/test_review.py tests/cli/test_eef_review.py`
    （约 30 秒），应全部通过——第一个逐条覆盖附录 C.9 的表；第二个检查每个请求只画一点一轴、prompt 只定义画出来的东西；
    第三个在迷你数据集上用脚本化的答复把 CPU 正常被模型否定（转人工）、候选段被模型确认（判废，先经过一次格式修复）、
    可疑但模型没给意见（超时、引用不存在的帧、解释里写了「约 2 cm」，转人工）、模型不反对（判过）、文件里没有（转人工）
    都走一遍，录进 tape 后在新的运行目录离线回放，记录逐项相同，`aggregate` 只判废那一条。模型对照真值的准确率用
    `PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.review_eval --dataset dataset2 --stand-in`（假模型，只查流程）
    或带 `--endpoint/--model/--api-key-env` 的真实后端跑（见 `tools/eef_eval/README.md`）。
    转人工的条每个复核窗口都有标记图（窗口的 `evidence`），判过的条只有模型反驳或冲突的窗口有图。
11. 转人工进裁决（F5.11，C1 1.9 的复核种类 `eef_check`）：
    `../.venv/bin/python -m pytest -q tests/cli/test_eef_adjudication.py tests/results/test_eef_queue.py`（约 15 秒）与
    `../.venv/bin/python -m pytest -q tests/orchestr/test_eef_tasks.py -k settles -m "slow or not slow"`（约 30 秒），应全部通过：
    转人工的条留在 passed、进 review 并计入待裁；判「一致」保留，判「不一致」进拒绝（理由「人工裁决判为 EEF 与视频不一致」），
    「拿不准」照旧待裁；只被本模块判废的条能复议恢复，和别的硬门一起判废的复议被拒；执行裁决后出新结果版本、交付标为过期，
    重新导出后交付里没有判废的那条。界面上：第 9 步的任务跑完后打开「人工裁决」，转人工的条是一张卡片，写着「来源：EEF–视频一致性 ·
    EEF 与画面核对」、为什么转人工、CPU 分项读数（可疑的格子标橙）、模型逐窗口答复（位置 / 朝向 / 绿十字跟对了三张票、偏移、
    模型原话、与 CPU 冲突的窗口带橙框）和每个窗口的标记图，按钮是「一致，判过」「不一致，判废」「拿不准」；视频下面不再重复这些图。
    被本模块判废的条在「被拒复议」里同样带这一块。
    报告页（F5.12）：「EEF–视频一致性」一节的「转人工」下面写着人工裁决里待裁几条（转人工又被别的检查判废、或已经裁过的不在其中），
    「判废」下面写可复议几条，卡片右上有「去裁决」「可复议」按钮；「Episode 明细」里这个模块的一块和裁决卡片是同一组内容
    （结论与理由、CPU 分项、逐窗口答复与标记图、CPU 证据帧），页面上方的证据帧不再重复这些图；`report.md` 的这一节多一行
    「人工裁决:待裁 N 条…可复议 M 条」。测试：`../.venv/bin/python -m pytest -q tests/cli/test_eef_check.py`（报告一节）与前端
    `npx vitest run src/pages/report`（「the EEF block」一条）。
12. 从 LeRobot 列生成 trajectory.json（设计 §3.3）：`../.venv/bin/python -m pytest -q tests/eef/test_mapping.py`（约 15 秒），应全部通过；再手动：

    ```bash
    ../.venv/bin/python -m curation.extensions.eef_consistency export --mapping tests/eef/mappings/dataset2.yaml \
      --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 --out /tmp/ds2_mapped.json
    ../.venv/bin/python -m curation.extensions.eef_consistency validate --trajectory /tmp/ds2_mapped.json \
      --lerobot-root ~/ws/ws_general/galbot/dataset2/eef_ds2_lr3 --all-points-observable | head -20
    ```

    第一条打印 `samples: 7`、`frames: 2009`；第二条 `report.valid: true`、七条 episode 都 `available`（投影由平台按位姿与标定重算，
    包里 `projection` 为 `null`）。把映射里的 `layout` 删掉或写成 `auto`，`export` 以退出码 2 报「eef.layout」——映射必须写明，不猜列宽。
13. 留出集验收（F5.7，在仓库根执行，约 2 分钟）：`PYTHONPATH=backend:tools .venv/bin/python -m eef_eval.acceptance --dataset dataset3 --jobs 4`，
    需要仓库外的 `galbot/dataset3`（构建方法见 `tools/eef_eval/README.md`）。末尾打印的汇总应为 `detected: 19`、`false_alarm_episodes: 0`；
    分组报告写在 `tools/eef_eval/reports/acceptance.md`。

14. 夹爪外观模板（F5.8，锚点的第二种来源，与种子二选一）。先从 dataset2 第 0 条的种子建模板（约 20 秒），再检查重检测器，
    再用模板而不是种子跑三条 episode：

    ```bash
    G=~/ws/ws_general/galbot/dataset2
    ../.venv/bin/python -m curation.extensions.eef_consistency template-build --trajectory $G/trajectory.json \
      --lerobot-root $G/eef_ds2_lr3 --seeds $G/observations_seed --episodes 0 --every 15 --dataset dataset2 \
      --tool-name robotiq_2f85 --out $G/gripper_template.json
    ../.venv/bin/python -m curation.extensions.eef_consistency template-check --template $G/gripper_template.json \
      --trajectory $G/trajectory.json --lerobot-root $G/eef_ds2_lr3 --seeds $G/observations_seed --episodes 0 6 --every 15
    ../.venv/bin/python -m curation.extensions.eef_consistency run --trajectory $G/trajectory.json \
      --lerobot-root $G/eef_ds2_lr3 --template $G/gripper_template.json --episodes 0 5 6 --out /tmp/eef_run_template
    ```

    `template-build` 打印 `entries: 27`、`masked_entries: 27`、`skipped_static: 13`（开头静止的一秒多没有条目）、两路相机、
    `methods: ["synthetic_fixture"]`（种子是真值反推的，模板也只供 DEMO）。`template-check` 每路一行：ep0 两路 `detected` 约 10–14 / 20，
    对种子的 `reference_p95_px` ≤ 1；ep6 同样（视频与 ep0 相同，账本不同不影响重检测）。`run --template` 的结论与第 6 步用种子时一致：
    ep0 全 `ok`，ep5 两路 `temporal_alignment: suspect` 且 `time_offset` 被支持，ep6 只有 `27432424_left` 位置 suspect 且 `extrinsics_error`
    被支持；`details.jsonl` 里 `template_sha256` 有值、`seeds_sha256` 为 null，`cameras.<相机>.observation.seed_method` 为 `gripper_template`，
    `redetections` 形如 `10/147`（每 15 帧一次成功，失败的帧逐帧重试）。命令行链路上把模板当参数传（`--param eef_video_consistency.gripper_template=FILE`），
    没有种子目录时预检仍是 `available`，notes 里写着模板的 hash 与条目数；控制台第二屏的「夹爪参考」下拉换成「夹爪外观模板」（`.json`），
    同一个按钮上传，换选项会丢掉已传的另一种（二选一）；上传即校验（`POST /uploads?kind=eef_gripper_template`，返回条目数、可用条目数、
    相机、掩膜条目数与提示，控制台显示「N 个模板条目 · 相机 …」）。
    单测：`../.venv/bin/python -m pytest -q tests/eef/test_template.py`（合成场景 7 条 + DEMO 数据 1 条，约 15 秒）。
15. mcap 数据集（F5.13）：`../.venv/bin/python -m pytest -q tests/cli/test_eef_mcap.py`（约 1 分钟），应全部通过：对账工具的迷你
    mcap 数据集与迷你 LeRobot 数据集是同一批画面，把 trajectory.json 的相机改成 `"uri": "episode_<N>.mcap", "topic":
    "/observation.images.exterior", "clip_start_s": 0, "clip_end_s": null`，逐帧画面与 LeRobot 视频同号同图，预检 `available`，
    `check` 的分项读数与结论和 LeRobot 版逐项相同；假 TOS 上每个 episode 的 `.mcap` 只下载一次（进漏斗的源缓存）；Lance 仍是
    `unsupported`。手动：`cd .. && PYTHONPATH=tools .venv/bin/python -m parity make-fixture --format mcap --out /tmp/mini_mcap`
    做一份 mcap 数据集，把测试里的 trajectory.json 照上面改写后，
    `../.venv/bin/python -m curation.cli preflight --input /tmp/mini_mcap --modules eef_video_consistency --vlm-backend ark --param eef_video_consistency.trajectory_json=<文件>`
    应是 `available`。写 trajectory.json 时 `frame_count` 是该 topic 的帧数，`video_frame_index` 是逐帧对应的第几条图像消息。

## 回退

回退就是新建任务时不勾选这个模块（预设与「全选可用」本来就不勾）：不勾时计划里没有它，判决配置与接入前完全相同，旧流程逐字节不变。
勾了以后它参与判决（D49）：判废的条目进拒绝清单，转人工的出裁决卡片（`eef_check`，人判后按判过 / 判废重算）；阈值仍是未校准的 demo。
