# Curator v2 · 机器人数据质检平台

Curator 是 Physical AI Kit 的数据质检平台，检查机器人操作数据集的质量。它读入 LeRobot（v2 / v3）、mcap 和 Lance 格式的数据集
（UMI 原始会话也能读，但只跑 EEF–视频一致性），对每条 episode 运行一组质检模块，给出「通过 / 拒绝 / 待补跑」的结论，把需要人确认的条目放进人工裁决；报告和结果清单写回你指定的
TOS 交付目录。控制台还内置数据可视化，多路相机和运动曲线在同一条进度条上同步播放。

数据集可以在本地目录或 `tos://` 上（TOS 上的 mcap 按范围直接读，Lance 的表先拉到本地再读），`.rrd` 暂不支持。平台交付的是质检报告和结果清单，
不写交付数据集（D69，见 [06 篇 §4](docs/design/06-delivery-and-report.md)）。

产品是「网页控制台 + REST API + 命令行」三件套，打成一个镜像，作为 rerun 仓库 dataverse Helm Chart 的一个组件部署在火山引擎 VKE 上（D53）。
前半篇写给使用平台的人，后半篇（[开发](#开发)起）写给开发者。

## 平台由哪几部分组成

左侧导航的每一项对应平台的一部分（「质检」「数据集」两组可以折叠）：

| 页面 | 用来做什么 |
|---|---|
| 概览 | 看正在跑的任务，以及一段时间内的质检量、通过率和 Token 消耗 |
| 质检 › 质检任务 | 新建任务，看进度，暂停或恢复，打开报告 |
| 质检 › 人工裁决 | 处理需要人确认的条目，执行裁决后重新出结果 |
| 数据集 › 数据集列表 | 登记数据集，查看预检结果和在它上面跑过的任务 |
| 数据集 › 可视化 | 在浏览器里同步播放多路相机和运动曲线 |
| 系统和资源配置 | 管理 TOS 访问密钥和视觉语言模型（VLM）后端 |
| 帮助 › API 文档 | 在新标签页打开 REST API 文档，把质检接进自己的流程（「使用文档」还没有内容，点了只提示还没配置） |

## 开始前：登记访问密钥和数据集

1. 在「系统和资源配置」添加 TOS 访问密钥（AK / SK），平台加密保存，任务读数据、写报告都用它。要用调用模型的模块（如任务成败判定），
   还要在这里登记 VLM 后端和模型。
2. 在「数据集列表」点「添加数据集」，选来源：私有 TOS 填地址、地域和访问密钥；HuggingFace 缓存桶（站点配了公开目录才有）从列表里选，
   列表里是平台能读的全部格式（LeRobot、Lance、mcap、UMI 原始会话，每项写明格式），匿名只读、不用密钥；站点开放了本地输入时还有「本地挂载路径」。名称、备注可以不填。填好后平台自动预检，识别格式、
   episode 数、相机和帧率。mcap 填放 `.mcap` 文件的那一层目录，Lance 填放 `frames.lance` 等三张表和 `meta/` 的那一层。
3. mcap 数据集多一步：确认字段映射，告诉平台哪些 topic 是相机、哪些是关节状态和动作。映射确认后才能可视化，质检也按这份映射读数据。

数据集里有末端位姿记录时，可以在数据集详情里补一份「数据集声明」（相机内外参、工具偏移等）。补好后，可视化能把末端轨迹画在画面上，
EEF–视频一致性模块也按它生成轨迹。手持夹爪（UMI、DAS）的录制，平台能直接从数据里算出轨迹，不用声明。

## 可视化：先看看数据

从「数据集 › 可视化」进入，或在数据集列表某一行点「可视化」。左栏选数据集和 episode，右边的播放器把这条 episode 的多路相机和运动曲线
放在同一条进度条上。

- 布局：「智能布局」自动摆放相机和曲线；也可以选「仅展示视频」「仅展示运动曲线」，或在格子上点「更换」「放大」自己调。
- 详细信息：右上角打开侧栏，点哪个格子就显示它的来源、编码和帧率；点曲线格可以勾选显示哪些线，并看到当前时刻的数值。
- 轨迹：平台能得到这条 episode 的末端轨迹时（有数据集声明，或是手持夹爪数据），顶栏出现「轨迹」按钮，在画面上叠加末端位置、坐标轴和运动轨迹。
- 操作：空格播放或暂停，← → 逐帧，Shift + ← → 跳 1 秒；进度条右端的图标把播放器铺满浏览器窗口，按 Esc 退出。
- 保存布局：顶栏「布局 → 保存为缺省布局」，之后所有人打开这个数据集都是这个布局；页头的「展示配置」可以调相机顺序、显示名和曲线分组。

## 质检逻辑

### 一条 episode 怎么得出结论

每个模块只报告它发现的问题，平台叫它「发现」。每条发现带一个检测项编号（如 ACT-4）和一个级别：

| 级别 | 含义 | 默认策略下的结果 |
|---|---|---|
| 判废 | 这条数据不能用 | 拒绝 |
| 待人工复核 | 模块拿不准，需要人确认 | 先算通过并交付，同时进入人工裁决 |
| 仅报告 | 值得留意，不影响能不能用 | 只写进报告 |

所有模块跑完后，平台按任务的判决策略汇总：有判废的发现就拒绝；没有判废，但有模块执行出错或没有结果，这条记为「待补跑」，
在任务详情里点「重试」补跑；其余的通过。

判决策略有两档。「默认」按上表执行。「只报不拒」把所有发现都只写进报告、不转人工；只有坏文件照样判废：文件为空、截断、读不出来这类
数据完整性的致命问题，以及解码时才发现损坏的视频（`decode_failed`），因为这样的数据什么模块都用不了。

### 能检出哪些问题

平台把检测项整理成一张分类表，分 9 个维度、共 63 项（另有 5 项对照，标记合理的情形，平台不应报出），报告里会标出本次任务覆盖了其中哪些。分类表也收了平台还不检测的项，例如标注与画面不符、中途失误后又完成，报告里标为未覆盖。

| 维度 | 编号 | 典型问题 |
|---|---|---|
| 文件与结构 | FILE | 文件为空、截断、结构损坏、视频解码失败、数值里有 NaN |
| 传感器流与时间 | STRM | 丢帧跳变、时间戳倒序或重复、缺一路流、过短的残段 |
| 单路画面质量 | IMG | 画面冻结、曝光不良、模糊、花屏、抖动、镜头污染 |
| 多视角一致性 | MV | 各相机滞后不一致、末端投影与画面不符 |
| 动作与本体信号 | ACT | 动作不平滑、尖刺、关节越限、执行器卡死、夹爪抖动 |
| 动作与画面的一致性 | AV | 画面与动作的时间错位 |
| 任务与步骤 | TASK | 任务失败、开头或结尾空转、操作不流畅 |
| 标注与描述 | LABEL | 缺任务标注、标注与画面不符 |
| 数据集级 | SET | 重复的条目 |

### 每个模块查什么

平台有 10 个质检模块，分两块同时运行：CPU 块依次做数据完整性、数值、画面和重复检测，每条 episode 做完一步就进入下一步；VLM 块
调用视觉语言模型。数据完整性判废的条目，其它模块不再评估，只记为「跳过」，报告和任务详情里单独计数；模型检查每条都等数据完整性
出了结论才开始。任务详情里的进度按「数据完整性」「CPU 检查」「模型检查」「生成报告」四组显示。下表的「最高级别」就是新建任务时
模块卡片上的标签，表示这个模块最多能把一条 episode 判到哪一级。解码时发现视频文件损坏，视觉质量、视频-动作同步和任务成败判定
都会判废这一条。

| 模块 | 查什么 | 最高级别 |
|---|---|---|
| 数据完整性 | 文件能不能读：空文件、截断、零填充、结构损坏、CRC 不符；帧数与数据长度、数值、时间戳是否合规；缺动作数据或少一路流。可选整读校验和逐帧解码测试 | 可判废 |
| 时间戳检查 | 时间戳是否单调，有没有丢帧跳变、采样抖动、倒序或重复，是不是短于最短时长的残段；时长离群只报告 | 可判废 |
| 运动学极限 | 对照机器人规格库，检查关节位置和速度、末端工作空间和速度是否越限；需要知道机器人型号 | 可判废 |
| 运动质量 | 动作平滑度、尖刺、执行器饱和或卡死、夹爪抖动、操作流畅度、开头和结尾空转，给出打分 | 只报告 |
| 视觉质量 | 逐路相机检查清晰度、曝光、画面冻结和无信号，给出打分；画面问题只报告，视频解不开（文件损坏）判废 | 可判废 |
| 视频-动作同步 | 比对画面运动和关节速度，找出画面与动作的时间错位；所有可信相机都错位才判废，单路错位只报告 | 可判废 |
| EEF–视频一致性 | 把数据集记录的末端位姿投影到画面上，和画面里实际的夹爪比位置、朝向和时间。CPU 测量和模型复核各给一个结论与置信度，两边结论相反时才转人工。要能得到末端轨迹：数据集声明、手持夹爪数据自带，或上传 trajectory.json | 可转人工 |
| 任务成败判定 | 模型看多机位视频判断任务是否完成：失败判废，拿不准的转人工；没有任务标注的条目不判 | 可判废 |
| 镜头画面缺陷 | 随任务成败判定的同一次模型请求，报告花屏、相机抖动、镜头污染与视觉遮挡；不单独勾选，开了任务成败判定就有 | 只报告 |
| 重复检测 | 找出动作和各路画面完全相同的条目，同一组的每一条都写明和哪几条相同；只报告，不判废 | 只报告 |

## 开启一个质检任务

1. 点「新建任务」：概览和质检任务页右上角都有；从数据集列表某一行点「新建任务」，或在数据集详情、可视化页点「新建质检任务」，会带上这个数据集。
2. 第一屏「基本配置」：填任务名称，选数据集，填交付目录（报告写到哪个 TOS 目录）。平台自动预检，按结果标出每个模块能不能用；
   用不了的模块，点卡片上的「详细信息」看原因。
3. 选要检的 episode：全部、前 N 条，或自选编号。
4. 选质检范围：「完整质检」开启全部可用模块，包括调用模型的；「快速质检」只开不调用模型的模块，出结果最快；也可以自己勾选。
   EEF–视频一致性不在预设里，需要时手动勾选。
5. 选判决策略；勾了调用模型的模块时，再选 VLM 后端和模型。
6. 第二屏「模块设置」：先补齐「需要补充」的输入（如机器人型号、EEF 的轨迹文件），或者跳过那个模块；其余是可选参数，不改就用缺省值。
7. 点「创建并开始」，或者「保存为待启动」，以后再开始。

任务开始后进入任务详情页：

- 「Episode 流水线」列出每条 episode 在各组里走到了哪一步、各组耗时和当前结果，点编号可以直接看这条的画面。
- 「详细进度」按数据完整性、CPU 检查、模型检查、生成报告四组给出完成数和耗时，「Token 消耗」统计模型用量。
- 「模块」一节每个模块一行：评估了几条、检出几条，数据完整性判废后跳过了几条，有待补跑的写明条数。
- 右上角可以暂停、恢复或停止任务，停止或失败的任务可以「继续运行」；有条目待补跑时，「更多」里有「重试（N 条）」。任务跑完后出现「查看报告」。

## 读懂质检报告

报告有三个页签：「报告」看整体，「Episode 明细」看单条，「性能剖析」看耗时和模型调用。页头可以切换结果版本：每次重试或执行裁决都会
生成一个新版本，旧版本保留可查。

- **质检总览**：第一行是一个等式：输入 = 判废 + 交付 + 待补跑。交付里有待人工确认的条目时，卡片下方会注明条数。第二行是通过率、
  耗时、Token 和判决策略。再往下是「判废原因分布（按检测项）」，一条 episode 有几个原因就各记一次。
- **本次质检范围与检测项覆盖**：表格列出每个模块所在的块、检出 / 评估条数和一句结论，点模块名跳到它的小节。下面的「检测项覆盖」
  把分类表的每一项标成覆盖、部分条目评估不了或未覆盖，项目旁的数字是检出条数。
- **数据包完整性**：格式、episode 数、相机、帧率、机器人型号、任务标注等预检结果；缺源文件、没有检查的条目也列在这里。
- **模块小节**：每个模块一节，开头是评估、检出、各级别和跳过的条数，再配一到三张图，例如时间戳的问题分布、视觉质量的逐相机得分、
  同步的滞后分布。报告页只放统计，逐条的结果在「Episode 明细」。
- **Episode 明细**：按编号搜索，或用「通过 / 拒绝 / 待补跑 / 待裁决」、级别和检测项筛选。选中一条后，「判决概要」按判废、
  待人工复核、仅报告列出这条的全部发现，每条写明来自哪个模块、对应哪个检测项、发生在哪一段时间；下面是各模块的详细读数，
  保存了证据帧的条目会附上证据帧，点开看大图。点「可视化」或发现旁的时间段，会弹出迷你播放器：发现标在进度条上，
  点上方的发现标签就跳到那一段。

## 人工裁决

标为「待人工复核」的条目会进入「人工裁决」页，任务详情和报告页头都有入口。「待裁决」页签逐条给出模块的结论、依据和画面，按问题
选择答案，例如判成功或判失败、一致或不一致，拿不准也可以先记下。「被拒复议」页签可以对任务成败判定判废的条目申请改判。裁完点
「执行裁决」，平台重判受影响的条目，生成新的结果版本和报告。

## 常见问题

**结果写到了哪里？** 报告和结果清单写在新建任务时填的 TOS 交付目录下，每次跑批一个 `<run_id>/` 目录，里面的 `revisions/rNNNN/`
是各个结果版本；全部写完后会在 `<run_id>/` 下放一个 `_COMPLETE` 标记文件。交付目录根上的 `latest` 指向最近一次完整成功的跑批
（没有待补跑、没有失败的模块）；有待补跑的跑批要补跑成功后才会被它指到。下游程序可以据此判断交付完成。

**有条目待补跑怎么办？** 待补跑表示某个模块在这条上出错或没有结果。在任务详情点「更多 → 重试」，只补跑这些条目，完成后生成新的
结果版本。

**想先快速看个大概？** 选「快速质检」，只跑不调用模型的模块；或者在 Episode 选择里选「前 N 条」先抽检一部分。

**交付目录里旧的跑批怎么清理？** 单个任务：已结束的任务在「更多 → 清理交付产物」，删掉这个任务写在 TOS 上的跑批目录（`latest` 指着它时一并移除）；
删任务时也可以勾选「同时清理交付产物」。整个交付目录：在 Pod 里用运维命令 `curation prune <交付目录>` 列出历次跑批（时间、条数、占用），
`--keep-latest N --yes` 删掉更早的；`latest` 指的那次不会删。用法见 [CLI README](backend/curation/cli/README.md) 的「运维命令」。

---

## 开发

```
前端控制台   React 18 + Arco，纯静态，所有状态都向 Daemon 要
    │  HTTP/JSON（C4）+ SSE（进度、日志）
API Daemon   FastAPI 单副本：routes → orchestr / planner / exec → repo（SQLite）
    │  子进程：argv 传参，stdout 出一份 --json（C2），stderr 出进度（C3）
命令行       curation 原子命令，命令之间靠运行目录里的文件交接
    │
内核         质检算法、数据集读取、报告生成、VLM 客户端
```

Daemon 用子进程调命令行，不在进程内 import：原生库崩溃只带走子进程，暂停、停止就是给进程组发信号，命令行也因此一直能单独使用
（设计 00 §2.1）。数据可视化是例外：它只读、不碰判决，Daemon 在进程内读数据集，只有转码在子进程里。

- 设计：[docs/design/](docs/design/)（入口 [00-overview.md](docs/design/00-overview.md)，§7 是全部冻结决策）
- 契约：[docs/contracts/](docs/contracts/)（一页导读 [SUMMARY.md](docs/contracts/SUMMARY.md)）；CLI、Daemon、前端之间只通过这些文件对话
- 需求账本与进度：根目录的 `feature_list.md`、`claude-progress.txt`，只在开发机本地，不入库
- v1（Gradio 单体加 Python CLI）在 `release_v1` 分支，本分支不再包含它的代码（设计 27，D89）

### 目录

| 路径 | 内容 |
|---|---|
| `backend/curation/` | 内核：算法（`core/`）、机器人规格库（`registry/`）、数据集读取（`ingest/`）、数据集级检查（`dataset_level/`）、报告生成（`export/`）、编排壳（`pipeline/`）、VLM 客户端与视频输入（`adapters/`）、按块读 TOS（`streams/`）、数据集声明（`declaration/`）、可视化的格式解析（`viz/`）；内核单测在包内 `tests/` |
| `backend/curation/cli/` | 命令行 `curation`：`preflight → plan → snapshot → check → aggregate → report → verify`、`adjudicate-apply`、REST 薄客户端 `curation task …`、把 UMI 原始会话转成 LeRobot 的离线工具 `stage-umi`，以及运维命令 `ls`、`prune`、`fetch`；说明与手动验证见 [其 README](backend/curation/cli/README.md) |
| `backend/curation/planner/` | 执行计划：分段与并发、VLM 闸门、Token 两本账、外层重试；请求合并与自适应降并发的框架在这里，现有模块还没用上；见 [其 README](backend/curation/planner/README.md) |
| `backend/curation/extensions/` | 扩展模块：数据完整性（[README](backend/curation/extensions/integrity/README.md)，设计 14）、EEF–视频一致性（[README](backend/curation/extensions/eef_consistency/README.md)，设计 12、25）、镜头画面缺陷（[README](backend/curation/extensions/camera_defects/README.md)，设计 13） |
| `backend/curation/contracts/` | C1 模块注册表与契约校验工具 |
| `backend/daemon/` | API Daemon：REST 与 SSE、SQLite 仓储、鉴权、任务编排、密钥与 VLM 后端、结果读取、数据可视化；用法与手动验证见 [其 README](backend/daemon/README.md)，编排见 [orchestr/README.md](backend/daemon/orchestr/README.md)，结果读取见 [results/README.md](backend/daemon/results/README.md)，密钥见 [secrets/README.md](backend/daemon/secrets/README.md)，可视化见 [viz/README.md](backend/daemon/viz/README.md) |
| `backend/tests/` | v2 的测试，按工作包分目录：`cli`、`contracts`、`daemon`、`orchestr`、`results`、`planner`、`secrets`、`eef`、`deploy`、`viz` |
| `backend/scripts/` | 测试数据下载、找空闲 GPU、mcap 孪生数据集、可视化样例数据与样本集实测 |
| `frontend/` | 网页控制台：React 18 + TypeScript + Arco Design，接口类型由 `docs/contracts/openapi.yaml` 生成；安装、运行、测试、构建与逐页手动验证见 [frontend/README.md](frontend/README.md) |
| `frontend/mockups/` | 静态 HTML 预览稿（只读参考） |
| `tools/parity/` | 对账工具：黄金基线的录制与回放、比对、假模型、A 类守卫；见 [其 README](tools/parity/README.md) |
| `tools/regression_samples/` | 回归样本集的工具：合成注入、平台结果打分（设计 16）；见 [其 README](tools/regression_samples/README.md) |
| `tools/eef_eval/`、`tools/eef_convert.py` | EEF 离线评估；`trajectory.json` 在 LeRobot 与 mcap 孪生数据集之间互转 |
| `deploy/` | 镜像（`deploy/Dockerfile`，构建上下文是仓库根）与集群上的运维步骤（[deploy/README.md](deploy/README.md)）；Chart 在 rerun 仓库的 `deploy/helm/dataverse`（D53） |
| `docs/design/`、`docs/contracts/` | 设计文档；契约（JSON Schema、OpenAPI、示例、锁文件，见 [docs/contracts/README.md](docs/contracts/README.md)） |

### 本地环境

Python 依赖装在仓库根目录的 `.venv`，配方见 [tools/parity/README.md](tools/parity/README.md) 的「本地环境」一节。镜像与 CI 用
Python 3.10，本机 `.venv` 是 3.12：别用 3.11 以后才有的语法和标准库。前端要 Node 20.19+ 或 22.13+，装依赖只用 `npm ci`。

本机起服务用 `.claude/launch.json` 里的配置：`curator-daemon-dev`（`/curation` 前缀、不鉴权、临时数据目录）、`curator-daemon-local`
（再加本地数据根与本地交付替身）、`curator-daemon-eef`（数据根是 EEF 的样例数据）、`curator-fakevlm`（假模型）、`curator-frontend-dev`
（接模拟数据的前端开发服务器）和 `curator-mockups`（静态预览稿）。

### 测试

在仓库根目录执行：

| 套件 | 命令 | 说明 |
|---|---|---|
| 内核单测 | `cd backend && ../.venv/bin/python -m pytest -q curation/tests` | 要在 `backend/` 下跑；用真数据集的用例在本机缺数据时跳过 |
| 契约 | `cd backend && ../.venv/bin/python -m pytest -q tests/contracts && ../.venv/bin/python -m curation.contracts check` | 契约与锁不一致就失败；改契约的步骤见 [docs/contracts/README.md](docs/contracts/README.md) |
| v2 各工作包 | `cd backend && ../.venv/bin/python -m pytest -q tests/<目录>` | 最慢的是 `orchestr`（约 10 分钟；`-m "not slow"` 跳过真起命令行的用例）和 `cli`（几分钟） |
| 对账工具 | `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests` | 约一分钟；`-m "not e2e"` 只跑单元部分 |
| 回归样本工具 | `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests` | 秒级 |
| 前端 | `cd frontend && npm run check:api && npm run lint && npm run typecheck && npm test && npm run build` | Vitest + jsdom，模拟数据的响应按契约校验 |

任何可能影响判决的改动，先跑对账工具的黄金基线回放：`PYTHONPATH=tools .venv/bin/python -m pytest tools/parity/tests -m e2e`。

### 手动验证步骤

各组件的 README 都有手动验证的一节，行为变了要同步改。从仓库根目录起，按这个顺序走一遍：

1. **对账工具**：按 [tools/parity/README.md](tools/parity/README.md) 的「手动验证步骤」录一份黄金基线再回放，最后一行应为 `conclusion: PASS`（不需要任何密钥）。
2. **契约**：跑上表的契约测试；故意改一处契约再跑 `check`，会报出是哪个文件变了。
3. **命令行**：按 [CLI README](backend/curation/cli/README.md) 的「手动验证步骤」在合成数据集上逐条跑原子命令，再试运维命令 `ls`、`prune`。
4. **Daemon**：按 [Daemon README](backend/daemon/README.md) 真起进程核对；编排、结果读取、密钥、可视化各有自己的 README。
5. **前端**：`npm run dev` 看模拟数据，逐页核对项见 [frontend/README.md](frontend/README.md)；接真 Daemon 用 `curator-daemon-local`（先 `npm run build`）。
6. **静态预览稿**：`python3 -m http.server 4173 --directory frontend/mockups`，浏览器打开 <http://localhost:4173/tasks.html>，核对项见 [frontend/mockups/README.md](frontend/mockups/README.md)。
7. **镜像与部署**：本机没有 docker，镜像构建看 CI；集群上的安装、升级续跑与网关挂载按 [deploy/README.md](deploy/README.md) 核对。

### 跑通一次完整质检（真数据）

上面的步骤都不碰真数据和真密钥。真跑一次：

1. **起服务**：集群上按 [deploy/README.md](deploy/README.md) 补主密钥、装 dataverse，经网关访问；只在本机跑就用 `curator-daemon-local`，
   打开 <http://localhost:8080/curation/>。
2. **填密钥**：只能由使用者本人在界面里填，不写进仓库、不写进 CI、不进设计文档。「系统和资源配置」添加访问密钥与 VLM 后端，
   保存后状态应为「已验证」，给常用的模型点「设为默认」。
3. **照上面的[开始前：登记访问密钥和数据集](#开始前登记访问密钥和数据集)起，把用户指南走一遍**：登记数据集、新建任务（先用「前 50 条」试）、看进度、读报告、人工裁决。
   滚动升级时运行中的任务会被系统暂停，升级完自动续跑。
4. **看交付**：交付目录这一批的 `<run_id>/` 下有各个结果版本（`revisions/rNNNN/` 的清单、报告与明细）、裁决的 CSV 副本和 `_COMPLETE`，
   `latest` 指向它。

### CI

`.github/workflows/ci.yml`（工作流 `curator-ci`）：推送到 `feat/curator-v2` 和所有 PR 都会跑，四个任务都绿才算过：

| 任务 | 内容 |
|---|---|
| `tests` | Python 3.10 下串行跑 13 套：内核单测、契约与锁、命令行、planner、Daemon、密钥、结果读取、编排、镜像与部署约定、EEF、数据可视化、对账工具、回归样本工具；前面失败不影响后面的步骤 |
| `frontend` | Node 20 与 22 各一遍：`check:api`、`lint`、`typecheck`、`test`、`build` |
| `a-class-guard` | 原样搬来的算法文件（A 类，清单见设计 10 §2）逐个比对冻结时的哈希；有意的改动在 `tools/parity/a_class_declared.json` 登记，或在 PR 描述里写 `parity-change:` |
| `image` | 用公网源构建镜像，确认 Daemon 和命令行能在镜像里启动 |

失败时看运行页面上的注解和 Job summary；命令行用 `gh run list --branch feat/curator-v2` 和 `gh run view <id> --log-failed`。

## License

Apache 2.0（见 [LICENSE](LICENSE)）。
