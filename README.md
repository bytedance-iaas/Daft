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
| `backend/curation/` | 质检内核（从 v1 原样搬来）、v1 的编排与 CLI；测试在包内 `tests/` |
| `backend/curation/ui/` | 已下线的 v1 界面里待移植的逻辑（鉴权、深链解析、报告数据整形），移植完成后整包删除 |
| `backend/curation/contracts/` | C1 模块注册表与契约校验工具 |
| `backend/daemon/` | API Daemon；目前只有 C5 Repository 接口 |
| `backend/tests/` | v2 的测试（目前是契约测试） |
| `frontend/mockups/` | 静态 HTML 预览稿（F3.1） |
| `tools/parity/` | 对账工具与黄金基线流程（W0） |
| `deploy/Dockerfile` | 镜像，构建上下文是仓库根：`docker build -f deploy/Dockerfile .` |
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
4. **契约**：`cd backend && ../.venv/bin/python -m pytest -q tests && ../.venv/bin/python -m curation.contracts check`，测试全部通过、`check` 无输出；故意改一处契约再跑 `check` 会报出是哪个文件变了（步骤见 [docs/contracts/README.md](docs/contracts/README.md)）。
5. **静态预览稿**：`python3 -m http.server 4173 --directory frontend/mockups`，
   浏览器打开 <http://localhost:4173/tasks.html>，逐页核对项见 [frontend/mockups/README.md](frontend/mockups/README.md)。

## CI

`.github/workflows/ci.yml`：v1 单测、契约测试与漂移锁、对账工具测试（含合成数据上的端到端回放对账）、A 类算法文件保护检查、镜像构建。

## License

Apache 2.0（见 [LICENSE](LICENSE)）。
