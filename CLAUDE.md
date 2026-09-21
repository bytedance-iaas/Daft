# Curator v2 · 项目说明

这个仓库原是 daft 的 fork；上游 daft 源码已在 2026-09-20 删除（F1.2），现在只承载 Curator v2
（Physical AI Kit 的机器人数据质检平台）。在运的 v1 代码在 `release_v1` 分支。

## 先读什么

- 设计：`docs/design/00-overview.md`（入口，§7 是全部冻结决策）、`11-workstreams.md`（工作包与接口冻结顺序）
- 需求账本与进度：根目录 `feature_list.md`、`claude-progress.txt`
- 对账工具（W0）：`tools/parity/README.md`
- 契约：`docs/contracts/`

## 纪律

- **算法一行不改**：A 类代码（`core/`、`registry/`、`ingest/`、`dataset_level/` 等，清单见设计 10 篇 §2）逐字搬运，只允许改 import 路径。
  冻结点是 `release_v1` 的 `45bdf9292`（D34），`tools/parity/v1_manifest.json` 记着它逐文件的哈希。
- 任何可能影响判决的改动，先用对账工具证明与 v1 一致（`python -m parity compare`）。
- 代码注释、提交信息用英文；文档、界面文案用中文。

## 目录

| 路径 | 内容 |
|---|---|
| `backend/curation/` | 质检内核与 v1 的编排、CLI（测试在包内 `tests/`）；W3 起按设计拆成原子命令 |
| `backend/daemon/` | API Daemon：FastAPI、SQLite 仓储（实现 C5）、鉴权、SSE、启动对账；`python -m daemon` 或 `curator-daemon` |
| `backend/curation/ui/` | 已下线的 v1 界面，只剩待移植的逻辑（鉴权、深链解析、报告数据整形），移植完整包删除；新代码不要 import 它 |
| `frontend/mockups/` | 静态 HTML 预览稿 |
| `tools/parity/` | 对账工具与黄金基线流程 |
| `deploy/Dockerfile` | 镜像，构建上下文是仓库根 |
| `docs/design/`、`docs/contracts/`、`docs/v1/` | 设计、契约、v1 的使用文档与发布说明 |

## 开发环境

依赖装在仓库根目录的 `.venv`（配方见对账工具 README 的「本地环境」一节；macOS 27 上 scipy 用 1.16.3）：

```bash
.venv/bin/python -m pytest -q backend/curation/tests --ignore=backend/curation/tests/test_environment.py   # v1 单测（test_environment 要 GPU）
PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests   # 对账工具，约 40 秒
```
