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
| `backend/curation/ui/` | 已下线的 v1 界面里待移植的逻辑（鉴权、深链解析、报告数据整形），移植完成后整包删除 |
| `backend/curation/contracts/` | C1 模块注册表与契约校验工具 |
| `backend/daemon/` | API Daemon（W4 骨架：FastAPI、SQLite 仓储、鉴权、SSE、探针、静态资源与挂载前缀、启动对账）；用法与手动验证见 [其 README](backend/daemon/README.md) |
| `backend/daemon/results/` | 读结果（W5b）：报告、明细表切片、单条 episode 下钻、性能剖析、裁决队列与记录裁决；说明与手动验证见 [其 README](backend/daemon/results/README.md) |
| `backend/tests/` | v2 的测试：`contracts/`、`daemon/`、`secrets/`、`results/`、`cli/`、`planner/`、`export/`、`deploy/` |
| `frontend/` | 网页控制台（W10）：React 18 + TypeScript + Arco Design，按 C4 开发，接口类型由 `openapi.yaml` 生成；安装、运行、测试、构建与逐页手动验证见 [frontend/README.md](frontend/README.md) |
| `frontend/mockups/` | 静态 HTML 预览稿（F3.1） |
| `tools/parity/` | 对账工具与黄金基线流程（W0） |
| `deploy/` | 镜像（`deploy/Dockerfile`，多阶段：前端构建 + Daemon，构建上下文是仓库根）与 Helm Chart（`deploy/charts/curator/`）；构建、密钥、安装升级、主密钥轮换、备份恢复与部署前检查见 [deploy/README.md](deploy/README.md) |
| `docs/design/`、`docs/contracts/` | 设计文档；冻结的契约（JSON Schema、OpenAPI、示例、锁文件，见 [docs/contracts/README.md](docs/contracts/README.md)） |

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
13. **前端（W10）**：`cd frontend && npm ci && npm run check:api && npm run lint && npm run typecheck && npm test && npm run build`；用模拟数据看页面是 `npm run dev`，逐页核对项见 [frontend/README.md](frontend/README.md)。由真的 Daemon 托管构建产物（`/curation` 前缀、不鉴权、开发用主密钥）：用 `.claude/launch.json` 里的 `curator-daemon-dev`，浏览器打开 <http://localhost:8080/curation/>。

## CI

`.github/workflows/ci.yml`：v1 单测、契约测试与漂移锁、Daemon 测试、密钥与资源管理测试、读结果测试、镜像与 Chart 检查、v2 命令行测试、planner 测试、对账工具测试（含合成数据上的端到端回放对账），各组测试互不遮挡（前一组失败，后面照跑）；A 类算法文件保护检查、镜像构建（并在镜像里起一次 Daemon 与命令行）、前端（Node 20 与 22 各跑一遍 lint、类型、测试和构建）；另有一个独立 job 用官方 LeRobot loader 检查增量重导出的产物（lerobot 0.3.3 读 v2.1，0.6.1 读 v3.0）。

## License

Apache 2.0（见 [LICENSE](LICENSE)）。
