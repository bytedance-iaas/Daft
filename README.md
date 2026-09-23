# Curator v2 · 机器人数据质检平台

Physical AI Kit 的数据质检平台（Curator）第二版。v1 是 Gradio 单体加 Python CLI，在 `release_v1` 分支上运行。
v2 把它重构成三层：原子 CLI → REST API Daemon → 火山风格的中文前端，以 Helm Chart 交付，跑在 VKE 上。
**质检算法一行不改**，本期做的是骨架、契约和产品化能力。

- 设计：[docs/design/](docs/design/)（12 篇，入口 [00-overview.md](docs/design/00-overview.md)，§7 是全部冻结决策）
- 需求账本与进度：[feature_list.md](feature_list.md)、[claude-progress.txt](claude-progress.txt)
- v1 的使用文档与发布说明：[docs/v1/](docs/v1/)

## 目录

| 路径 | 内容 |
|---|---|
| `backend/curation/` | 质检内核（从 v1 原样搬来）与 v1 的编排；v1 的测试在包内 `tests/` |
| `backend/curation/cli/` | v2 命令行（W3）：`preflight`、`snapshot`、`verify`、`curation task …` 客户端；v1 的子命令经 `legacy.py` 原样转交。说明与手动验证见 [其 README](backend/curation/cli/README.md) |
| `backend/curation/export/` | 导出器：v1 的全量导出（A 类，原样）+ 增量重新导出（W7）。说明与手动验证见 [INCREMENTAL.md](backend/curation/export/INCREMENTAL.md) |
| `backend/curation/planner/` | 执行计划与 VLM 请求合并框架（W6）：闸门推导、合并执行器、Token 两本账、外层重试与自适应降并发。说明与手动验证见 [其 README](backend/curation/planner/README.md) |
| `backend/daemon/secrets/` | 密钥与资源管理（W8）：AES-GCM 加密存储与主密钥轮换、访问密钥与模型服务的校验、开始前三项检查、命令行子进程的环境、预签名；说明与手动验证见 [其 README](backend/daemon/secrets/README.md) |
| `backend/curation/extensions/eef_consistency/` | EEF–视频一致性（阶段 5，DEMO 模块，建议性、不影响判决）：`trajectory.json` 读取与校验、几何、能力预检、L0 数值轨迹、P-A 独立观测、五项指标与诊断、离线报告；接入 v2 的 `preflight` / `plan` / `check --param` / `aggregate` / `report`；说明与手动验证见 [其 README](backend/curation/extensions/eef_consistency/README.md)，离线评估器在 `tools/eef_eval/` |
| `backend/curation/ui/` | 已下线的 v1 界面里待移植的逻辑（鉴权、深链解析、报告数据整形），移植完成后整包删除 |
| `backend/curation/contracts/` | C1 模块注册表与契约校验工具 |
| `backend/daemon/` | API Daemon（W4 骨架：FastAPI、SQLite 仓储、鉴权、SSE、探针、静态资源与挂载前缀、启动对账）；用法与手动验证见 [其 README](backend/daemon/README.md) |
| `backend/daemon/orchestr/`、`backend/daemon/exec/` | 任务编排与 CLI 执行器（W5a）：运行、分档、worker 池、暂停 / 停止 / 继续、崩溃恢复、发布、数据集操作、工作目录清理；说明、配置与手动验证见 [其 README](backend/daemon/orchestr/README.md) |
| `backend/daemon/results/` | 读结果（W5b）：报告、明细表切片、单条 episode 下钻、性能剖析、裁决队列与记录裁决；说明与手动验证见 [其 README](backend/daemon/results/README.md) |
| `backend/tests/` | v2 的测试：`contracts/`、`daemon/`、`secrets/`、`results/`、`orchestr/`、`cli/`、`planner/`、`export/`、`deploy/`、`eef/` |
| `frontend/` | 网页控制台（W10）：React 18 + TypeScript + Arco Design，按 C4 开发，接口类型由 `openapi.yaml` 生成；安装、运行、测试、构建与逐页手动验证见 [frontend/README.md](frontend/README.md) |
| `frontend/mockups/` | 静态 HTML 预览稿（F3.1） |
| `tools/parity/` | 对账工具与黄金基线流程（W0） |
| `deploy/` | 镜像（`deploy/Dockerfile`，多阶段：前端构建 + Daemon，构建上下文是仓库根）与 Helm Chart（`deploy/charts/curator/`）；构建、密钥、安装升级、主密钥轮换、备份恢复与部署前检查见 [deploy/README.md](deploy/README.md) |
| `docs/design/`、`docs/contracts/` | 设计文档；冻结的契约（JSON Schema、OpenAPI、示例、锁文件，见 [docs/contracts/README.md](docs/contracts/README.md)；EEF 输入格式在 `docs/contracts/eef/`） |

## 本地环境

依赖装在仓库根目录的 `.venv`，配方见 [tools/parity/README.md](tools/parity/README.md) 的「本地环境」一节。

## 手动验证步骤

在仓库根目录执行：

1. **对账工具离线自检**（约 1 分钟，不需要任何密钥）：按 [tools/parity/README.md](tools/parity/README.md) 的「手动验证步骤」执行，
   最后一行应为 `conclusion: PASS`。
2. **v1 单测**：`cd backend && ../.venv/bin/python -m pytest -q curation/tests --ignore=curation/tests/test_environment.py`，
   应全部通过（`test_environment` 检查的是 GPU 主机，本机和 CI 都跳过）。冻结点上就不过的三条测试已修正，
   原因见 [docs/v1/test-fixes.md](docs/v1/test-fixes.md)。
3. **对账工具测试**：`PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests`（约 40 秒）。
4. **契约**：`cd backend && ../.venv/bin/python -m pytest -q tests/contracts && ../.venv/bin/python -m curation.contracts check`，测试全部通过、`check` 无输出；故意改一处契约再跑 `check` 会报出是哪个文件变了（步骤见 [docs/contracts/README.md](docs/contracts/README.md)）。
5. **静态预览稿**：`python3 -m http.server 4173 --directory frontend/mockups`，
   浏览器打开 <http://localhost:4173/tasks.html>，逐页核对项见 [frontend/mockups/README.md](frontend/mockups/README.md)。
6. **v2 命令行（W3）**：`cd backend && ../.venv/bin/python -m pytest -q tests/cli`（约 10 秒），应全部通过；逐条手动核对见 [backend/curation/cli/README.md](backend/curation/cli/README.md) 的「手动验证步骤」。
7. **planner 与 VLM 请求合并（W6）**：`cd backend && ../.venv/bin/python -m pytest -q tests/planner`（约 3 秒），应全部通过；逐项核对见 [backend/curation/planner/README.md](backend/curation/planner/README.md) 的「手动验证步骤」。
8. **增量重新导出（W7）**：`cd backend && ../.venv/bin/python -m pytest -q tests/export`（约 30 秒；官方 loader 的 3 条用例在共享 venv 里跳过），再按 [INCREMENTAL.md](backend/curation/export/INCREMENTAL.md) 的手动验证步骤跑一遍 v2、v3 的演示，并在两个独立 venv 里跑官方 loader，输出应为 `ok: true`、`warnings: []`。
9. **Daemon 骨架（W4）**：`cd backend && ../.venv/bin/python -m pytest -q tests/daemon`（约 40 秒），应全部通过；真起进程的 11 步手动验证见 [backend/daemon/README.md](backend/daemon/README.md)。
10. **密钥与资源管理（W8）**：`cd backend && ../.venv/bin/python -m pytest -q tests/secrets`（约 30 秒），应全部通过；再按 [backend/daemon/secrets/README.md](backend/daemon/secrets/README.md) 的 6 步手动核对。
11. **读结果（W5b）**：`cd backend && ../.venv/bin/python -m pytest -q tests/results`（约 30 秒），应全部通过；真起 Daemon 用 curl 逐个接口核对的步骤见 [backend/daemon/results/README.md](backend/daemon/results/README.md)。
12. **镜像与 Chart（W11）**：`cd backend && ../.venv/bin/python -m pytest -q tests/deploy`（约 10 秒，需要本机有 `helm`）；本机没有 docker，镜像构建看 CI；集群上的安装、升级续跑与网关挂载按 [deploy/README.md](deploy/README.md) 核对。
13. **任务编排（W5a）**：`cd backend && ../.venv/bin/python -m pytest -q tests/orchestr -m "not slow"`（约 1.5 分钟；去掉 `-m` 跑全部约 6 分钟，含真跑 CLI 的端到端），应全部通过；再按 [backend/daemon/orchestr/README.md](backend/daemon/orchestr/README.md) 的 10 步真起 Daemon 核对。
14. **前端（W10）**：`cd frontend && npm ci && npm run check:api && npm run lint && npm run typecheck && npm test && npm run build`；用模拟数据看页面是 `npm run dev`，逐页核对项见 [frontend/README.md](frontend/README.md)。由真的 Daemon 托管构建产物（`/curation` 前缀、不鉴权、开发用主密钥）：用 `.claude/launch.json` 里的 `curator-daemon-dev`，浏览器打开 <http://localhost:8080/curation/>。
15. **EEF–视频一致性（F5，DEMO）**：`cd backend && ../.venv/bin/python -m pytest -q tests/eef tests/cli/test_eef_check.py`，应全部通过（DEMO 数据在仓库外，缺了相关用例会跳过）；
    校验上传件、看能力表、真值键拒绝与自洽警告、离线评估、受控异常矩阵、在 v2 命令行链路上跑一遍、控制台上传与 Daemon 执行（F5.5，
    `tests/orchestr/test_eef_tasks.py`）、VLM 复核（F5.6，固定 tape 下各分支与离线回放）的逐项核对见 [其 README](backend/curation/extensions/eef_consistency/README.md)。

## 跑通一次完整质检（真数据）

上面第 1–14 步都不碰真数据和真密钥。真跑一次是这样，界面上的每一步都在
[frontend/README.md](frontend/README.md) 的「手动验证（模拟数据）」里有对应的模拟版本：

1. **起服务**：集群上按 [deploy/README.md](deploy/README.md) 第 3–4 节建 Secret、`helm install`，
   经网关访问见第 6 节；只在本机跑就用 `.claude/launch.json` 里的 `curator-daemon-dev`，
   打开 <http://localhost:8080/curation/>。
2. **填密钥**（只能由使用者本人在界面里填，不写进仓库、不写进 CI、不进设计文档）：
   「密钥与资源」→「添加访问密钥」填对象存储的 AK/SK 与地域；「VLM 后端」页签添加模型服务
   （火山方舟填 endpoint 与 API Key，自建 vLLM 填 endpoint），保存后状态应为「已验证」。
   拉出模型列表后给常用的那个点「设为默认」，之后新建任务会预选它。
3. **登记数据集**：「数据集」→「添加数据集」，填 `tos://<bucket>/<path>` 并选访问密钥，
   等自动预检出条目数、缺失文件与模块可用性，保存进详情页。
4. **新建任务**：数据集详情点「新建质检任务」，第一屏选 episode 范围（先用「前 50 条」试）、
   勾模块、填交付目录与它的访问密钥；第二屏按模块填参数（运动学极限要选机器人型号或跳过）。
   点「创建并开始」。
5. **看进度**：详情页有分档进度、模块表与执行时间线，页头写着实时通道是 SSE 还是 5 秒轮询；
   运行中可暂停 / 继续 / 停止。滚动升级时运行中的任务会被系统暂停，升级完自动续跑（F4.1 验收②）。
6. **看报告**：跑完点「查看详细报告」，核对总览的「输入 = 判废 + 交付 + 待补跑」、判废原因分布、
   数据包完整性（缺源文件被跳过的条目列在这里）、各模块小节，再点任一 `ep N` 下钻看逐条结论与视频。
7. **人工裁决**：报告里点「去裁决」，逐条判完点「执行裁决」；被去重或任务成败判定拒掉的条目
   在「被拒复议」页签里可以恢复。
8. **导出交付**：裁决的子任务跑完后，在任务详情点「导出」（已导出过的显示「重新导出」，
   只补差异），产物用官方 LeRobot loader 验证，步骤见
   [INCREMENTAL.md](backend/curation/export/INCREMENTAL.md)。

## CI

`.github/workflows/ci.yml`：v1 单测、契约测试与漂移锁、Daemon 测试、密钥与资源管理测试、读结果测试、任务编排测试（含真跑 CLI 的端到端）、镜像与 Chart 检查、v2 命令行测试、planner 测试、EEF 模块测试、对账工具测试（含合成数据上的端到端回放对账），各组测试互不遮挡（前一组失败，后面照跑）；A 类算法文件保护检查、镜像构建（并在镜像里起一次 Daemon 与命令行）、前端（Node 20 与 22 各跑一遍 lint、类型、测试和构建）；另有一个独立 job 用官方 LeRobot loader 检查增量重导出的产物（lerobot 0.3.3 读 v2.1，0.6.1 读 v3.0）。

## License

Apache 2.0（见 [LICENSE](LICENSE)）。
