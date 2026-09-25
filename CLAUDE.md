# Curator v2 · 开发说明

Curator v2 是 Physical AI Kit 的机器人数据质检平台。它读入机器人操作数据集（LeRobot v2 / v3、mcap、Lance；来源是私有 TOS、
HuggingFace 缓存桶，或站点开放的本地挂载路径），逐条 episode 跑一串质检模块，给出通过 / 拒绝 / 待定的判决和待人工复核的清单；
人工裁决后重判受影响的条目，生成质检报告，并把通过的数据导出成交付数据集写回 TOS。
产品是「网页控制台 + REST API + 命令行」三件套，打成一个镜像，用 Helm Chart 部署在火山引擎 VKE 上。

## 架构

```
前端控制台   React 18 + Arco，纯静态，所有状态都向 Daemon 要
    │  HTTP/JSON（C4）+ SSE（进度、日志）
API Daemon   FastAPI 单副本：routes → orchestr / planner / exec → repo（SQLite）
    │  子进程：argv 传参，stdout 出一份 --json（C2），stderr 出进度（C3）
命令行       curation 原子命令，命令之间靠运行目录里的文件交接
    │
内核         质检算法、数据集读取、导出、VLM 客户端
```

| 组件 | 位置 | 做什么 |
|---|---|---|
| 前端控制台 | `frontend/` | 概览、数据集、质检任务（新建 / 列表 / 详情）、质检报告、人工裁决、系统和资源配置；构建产物由 Daemon 托管 |
| API Daemon | `backend/daemon/` | REST 与 SSE、SQLite 仓储、鉴权、任务编排（排队、逐档流水线、暂停 / 停止 / 继续、崩溃恢复、子任务、发布到交付目录）、密钥与 VLM 后端管理、结果读取 |
| 命令行 `curation` | `backend/curation/cli/` | `preflight → plan → snapshot → autolabel → check（逐档）→ aggregate → report → export → verify`，另有 `adjudicate-apply` 和 REST 薄客户端 `curation task …`；Daemon 是它最大的用户 |
| planner | `backend/curation/planner/` | 执行计划：分档、并发、八把 VLM 闸门、请求合并；`curation plan` 与 Daemon 共用 |
| 内核 | `backend/curation/` 下的 `core/`、`registry/`、`ingest/`、`dataset_level/`、`export/`、`pipeline/`、`adapters/` | 算法（`core/` 是纯函数：不碰 I/O、不 import daft）、读取器、导出器、编排壳、VLM 客户端与视频输入 |
| 扩展模块 | `backend/curation/extensions/` | `eef_consistency`（EEF–视频一致性，设计 12）、`integrity`（数据完整性，设计 14） |
| 对账工具 | `tools/parity/` | 黄金基线的录制、回放、比对，假模型，A 类守卫 |
| 部署 | `deploy/` | 一个镜像（Daemon + CLI + 前端产物，缺省起 Daemon）、Helm Chart（StatefulSet 单副本 + 数据盘） |

Daemon 用子进程调 CLI，不在进程内 import：原生库崩溃只带走子进程；暂停、停止就是给进程组发信号；CLI 也因此一直是活的一等入口
（设计 00 §2.1）。

**质检漏斗**（C1 注册表，按档从前往后；前面硬门拦下的条目不进后面的档）：
`integrity`（data_integrity）→ `numeric`（timestamp_check、kinematic_limits、motion_quality）→ `frame`（visual_quality、
video_action_sync）→ `vlm`（eef_video_consistency、task_success）→ `post_verdict`（dedup）→ `profile_vlm`（skill_profile）。
task_success 让 VLM 读多机位连续视频判定成败（设计 13）。

**一次任务**：建任务时预检，开跑时生成并冻结执行计划 → Daemon 排队，逐档调 CLI（每档一个常驻 worker，episode 逐条交接）→
结果落在运行目录 `runs/<task_id>/`（`checks/<模块>/`、结果版本 `revisions/rNNNN/` 里的清单与报告、`export/`）→ 同步到交付目录，
最后写 `_COMPLETE` → 前端经 REST / SSE 看进度和报告。重试、继续运行、执行裁决、重新导出都作为子任务跑；执行裁决会产生新的结果版本。

## 目录

| 路径 | 内容 |
|---|---|
| `backend/curation/` | 内核、编排壳 `pipeline/`、命令行 `cli/`、planner、C1 注册表与 Schema 校验 `contracts/`、扩展模块 `extensions/`；内核单测在包内 `tests/` |
| `backend/daemon/` | API Daemon：`routes/`（REST、SSE、静态资源）、`orchestr/`（编排）、`exec/`（CLI 执行器）、`repo/`（C5 与 SQLite 实现）、`results/`（结果读取）、`secrets/`（密钥封存）；`python -m daemon` 或 `curator-daemon` |
| `backend/tests/` | v2 的测试，按工作包分目录：`cli`、`contracts`、`daemon`、`orchestr`、`results`、`planner`、`secrets`、`export`、`eef`、`optimizations`、`deploy` |
| `backend/scripts/` | 零散脚本：测试数据下载、标注工作台、规模压测、VLM 选型评测、环境安装 |
| `backend/curation/ui/` | 已下线的 v1 界面，只剩待移植的逻辑（鉴权、深链解析、报告数据整形），移植完整包删除；新代码不要 import 它 |
| `frontend/` | 网页控制台（React + Arco），接口类型由 `docs/contracts/openapi.yaml` 生成（改了 C4 要跑 `npm run gen:api`） |
| `frontend/mockups/` | 静态 HTML 预览稿（只读参考） |
| `tools/parity/` | 对账工具与黄金基线流程 |
| `tools/eef_eval/`、`tools/eef_convert.py` | EEF 离线评估（唯一读真值的代码）；`trajectory.json` 在 LeRobot 与 mcap 孪生数据集之间互转 |
| `deploy/` | 镜像（`deploy/Dockerfile`，构建上下文是仓库根）与 Helm Chart（`deploy/charts/curator/`），说明见 `deploy/README.md` |
| `docs/design/`、`docs/contracts/`、`docs/v1/` | 设计、契约、v1 的使用文档与发布说明 |
| `.github/` | CI：`workflows/ci.yml`、`scripts/pytest-summary.sh` |
| `.claude/launch.json` | 本机预览配置：Daemon、前端开发服务器、静态稿 |

## 设计与契约

**设计**在 `docs/design/`，以仓库里的 Markdown 为准（飞书上的是同步副本）。先读 `00-overview.md`：分层、核心数据流，
§7 是全部冻结决策。改设计先改文档，再改代码。

| 篇 | 内容 |
|---|---|
| 00 | 总览：分层、数据流、冻结决策（D、P 编号）、需求覆盖 |
| 01 | 数据模型：实体、状态机、启动对账 |
| 02 | CLI：命令、运行目录里的文件、退出码 |
| 03 | REST API |
| 04 | 并发、VLM 闸门与请求合并（planner） |
| 05 | 模块注册表与预检 |
| 06 | 交付与报告 |
| 07 | 前端（主规格） |
| 08 | 密钥与鉴权 |
| 09 | 部署 |
| 10 | 对账与迁移（§2 是 A 类清单） |
| 11 | 工作包（W0–W12）与接口冻结顺序 |
| 12 | EEF–视频一致性（DEMO 模块；首节是开工指引，格式规范与 Schema 在 `docs/contracts/eef/`） |
| 13 | 提速（`13-curation-speedup.md`，实施记录在 `13-speedup/`）；视频原生判定（`13-video-native-vlm.md`） |
| 14 | 数据完整性（首节是开工指引；决策 D50–D52） |
| `review-2026-09-20.md` | 设计评审记录 |

**契约**在 `docs/contracts/`（一页导读 `SUMMARY.md`）。CLI、Daemon、前端之间只通过这些文件对话，谁都不 import 对方的内部模块：

| # | 契约 | 文件 |
|---|---|---|
| C1 | 模块注册表：模块、档、依赖、参数 Schema、报告明细 | `backend/curation/contracts/modules.py`，导出为 `modules.json` |
| C2 | CLI 的 `--json` 输出与命令之间交换的文件 | `cli/*.schema.json` |
| C3 | 进度协议（stderr 上的 JSON Lines） | `progress.schema.json` |
| C4 | REST API | `openapi.yaml` |
| C5 | 仓储接口与状态机 | `backend/daemon/repo/protocol.py` |
| 其他 | EEF 输入格式；对账录制带格式 | `eef/`、`parity/` |

改契约：改文件，不兼容的改动升版本号（`registry_version` / `schema_version` / `info.version`）；在 `examples/` 补合法与不合法示例；
`cd backend && ../.venv/bin/python -m curation.contracts export-modules`（只在改了 C1 时）再 `… lock`；改了 C4 在 `frontend/` 跑
`npm run gen:api`。`CONTRACTS.lock` 没刷新、生成的类型没更新，CI 都会红。

文档和提交里的编号：W 是工作包（设计 11），F 是需求账本条目，D、P 是冻结决策（设计 00 §7），C1–C5 是契约。

## 各组件怎么开发

通用流程：先改设计 / 契约 → 实现 → 测试 → 更新对应 README 的「手动验证步骤」和设计文档 → 按路径提交。

**命令行**（`backend/curation/cli/`，说明见同目录 README）
- 一条命令只做一件事：stdout 只打一份 `--json` 文档（C2），进度走 stderr（C3），非零退出时打错误信封；退出码见设计 02 §4。
- SIGTERM 是暂停：不再开始新 episode，在途的做完落盘；SIGINT 是停止。`check --resume` 跳过已有非出错结果的条目。
- 命令之间只靠运行目录交接，Daemon 的调用顺序见 CLI README 的「Daemon 的调用顺序」。
- 测试在 `backend/tests/cli/`，模型用本地假端点 `tests/cli/fakevlm_server.py`（答案同 `tools/parity/fakevlm.py`，只看请求文字和画面尺寸，跨平台稳定）。

**API Daemon**（`backend/daemon/`，说明见同目录 README 与 `orchestr/README.md`）
- `routes/` 只管 HTTP；任务状态只经 `transitions.py` 迁移（CAS、审计事件、SSE 一处做完）；存取只走 C5 接口；编排在 `orchestr/`，调 CLI 只经 `exec/`。
- 加 / 改接口：先改设计 03 和 `openapi.yaml`（C4）→ 实现 → `tests/daemon`（接口）或 `tests/orchestr`（编排；标了 `slow` 的用例会真起 CLI）→ 前端 `npm run gen:api`。
- 配置都走环境变量，多数是 `CURATOR_*`（Daemon README「配置」一节）；数据库迁移在 `repo/migrations.py`（`PRAGMA user_version`）。

**前端**（`frontend/`，说明见同目录 README；主规格是设计 07）
- `src/api/schema.d.ts` 由契约生成，不要手改；全部界面文案在 `src/locales/zh.ts`；页面在 `src/pages/`，几页共用的块在 `src/features/`，纯逻辑放 `src/lib/` 并配单测。
- `npm run dev` 默认接 MSW 模拟数据（`src/mocks/`，每个 C4 接口都有处理器，改接口时一起改）；接真 Daemon 用 `VITE_API_TARGET=http://127.0.0.1:8080 npm run dev`。
- 改界面要同步更新设计 07 和 README 的手动验证步骤。

**质检模块与判定**
- 新模块放 `extensions/<模块>/`。要动哪些地方（C1 注册表、预检、`check`、planner 与 Daemon 的档、前端、测试与对账基线），照设计 12、14 的开工指引做，它们是现成的样板。
- VLM 输入是多机位连续视频（设计 13）；出厂默认模型只在 `pipeline/default.yaml` 的 `checks.task_success.vlm.model` 一处，线上按任务选的后端和模型由 Daemon 的「VLM 后端」管理。

**部署**（`deploy/README.md`）：镜像由火山 CP 流水线从分支构建，版本号是完整提交号；安装、升级（升级时运行中的任务被系统暂停）、备份与排障都在 deploy README。

## 开发环境

- 任何可能影响判决的改动，先跑对账工具的黄金基线回放（`PYTHONPATH=tools .venv/bin/python -m pytest tools/parity/tests -m e2e`）。
- 代码注释、提交信息用英文；文档、界面文案用中文。

Python 依赖装在仓库根目录的 `.venv`（配方见对账工具 README 的「本地环境」一节；macOS 27 上 scipy 用 1.16.3）。镜像与 CI 用
Python 3.10，本机 `.venv` 是 3.12：别用 3.11 以后才有的语法和标准库。前端要 Node 20.19+ 或 22.13+，装依赖只用 `npm ci`；增删依赖用
`npx -y npm@11 install …`（npm 10.9.2 解析依赖树会崩）。本机起服务可以用 `.claude/launch.json` 里的配置：`curator-daemon-dev`
（`/curation` 前缀、不鉴权、临时数据目录）和 `curator-frontend-dev`。

### 测试

| 套件 | 命令（仓库根目录起） | 说明 |
|---|---|---|
| 内核单测 | `cd backend && ../.venv/bin/python -m pytest -q curation/tests --ignore=curation/tests/test_environment.py` | 要在 `backend/` 下跑（有一条起子进程的用例靠工作目录找包）；`test_environment` 要 GPU |
| 契约 | `cd backend && ../.venv/bin/python -m pytest -q tests/contracts && ../.venv/bin/python -m curation.contracts check` | 契约与锁不一致就失败 |
| v2 各工作包 | `cd backend && ../.venv/bin/python -m pytest -q tests/<目录>` | 最慢的是 `orchestr`（约 10 分钟；`-m "not slow"` 跳过真起 CLI 的用例）和 `cli`（约 6 分钟）；Chart 检查要 helm 4 |
| 对账工具 | `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests` | 约一分半；`-m "not e2e"` 只跑单元部分 |
| 前端 | `cd frontend && npm run check:api && npm run lint && npm run typecheck && npm test && npm run build` | Vitest + jsdom，模拟数据的响应按契约校验 |

写端到端用例别靠时序：流水线下各档交叠执行，模型的快慢用假端点的 `delay_s`、`hold(needle)`、`fail` 控制，等条件满足再动作。

### CI 门禁

`.github/workflows/ci.yml`（工作流 `curator-ci`）：推送到 `feat/curator-v2` 和所有 PR 都会跑；同一分支上新的推送会取消还在跑的那一轮。
五个任务都绿才算过：

| 任务 | 内容 | 时长 |
|---|---|---|
| `tests` | Python 3.10 下串行跑 13 套：内核单测、契约与锁、CLI、planner、Daemon、密钥、结果读取、编排、执行优化、镜像与 Chart、增量导出、EEF、对账工具；前面失败不影响后面的步骤 | 约 30 分钟 |
| `frontend` | Node 20 与 22 各一遍：`check:api`、`lint`、`typecheck`、`test`、`build` | 几分钟 |
| `a-class-guard` | 原样搬来的算法文件（A 类，清单见设计 10 §2）逐个比对冻结时的哈希；有意的改动在 `tools/parity/a_class_declared.json` 登记新哈希和理由，或在 PR 描述里写 `parity-change:` | 秒级 |
| `lerobot-loader` | 官方 lerobot（0.3.3 读 v2.1、0.6.1 读 v3.0）加载增量重导出的产物 | 几分钟 |
| `image` | 用公网源构建镜像，确认 Daemon 和 CLI 能在镜像里启动 | 几分钟 |

失败时看运行页面上的注解和 Job summary（`scripts/pytest-summary.sh` 把 pytest 输出的尾部贴在那里，步骤日志要登录才能看）；
命令行用 `gh run list --branch feat/curator-v2` 和 `gh run view <id> --log-failed`。

## 协作约定

- 需求账本与进度：根目录 `feature_list.md`、`claude-progress.txt`——只在本地保存，不入库（已在 `.gitignore`，2026-09-24 需求方要求），照常读写、别提交。
- 同一个工作区里常有几个会话同时开发：提交按路径（`git add <路径>`、`git commit -- <路径>`），别 `stash`、`reset`、`checkout` 别人没提交的改动。
- 推送会取消同分支上还在跑的 CI：别人等着看 CI 结果时，先在本地提交，等那一轮跑完再推。
- 仓库是公开的：密钥和账号不进仓库，也不进日志（Daemon 日志里的密钥字段一律打成 `***`）。
- 每个组件的 README 都有「手动验证步骤」，行为变了要同步改，保证照着能跑通。

## 历史与遗留

- 仓库原是 daft 的 fork，上游 daft 源码已在 2026-09-20 删除（F1.2）；开发分支是 `feat/curator-v2`。
- 在运的 v1 代码在 `release_v1` 分支。v1 的子命令（`curation run`、`rejudge` 等）原样转交给 `cli/legacy.py`。
- 对账工具的 `dump-v1`、`v1_manifest.json` 留作工具自身与 v1 行为的离线回归；设计 13 起判定基线改为 v2 自录（`run-v2 --fake-vlm` 录，`--replay` 回放）。
