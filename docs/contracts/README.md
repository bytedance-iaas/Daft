# 冻结的契约（W2 / F1.3）

并行开发的地基：CLI、Daemon、前端之间只通过这里的文件对话，谁都不直接 import 对方的内部模块（设计 11 篇 §1）。

先读 [`SUMMARY.md`](SUMMARY.md)：一页讲清五份契约的要点、冻结时定下的细节和待拍板的事项。

| # | 契约 | 文件 | 谁读 |
|---|---|---|---|
| C1 | 模块注册表 | `backend/curation/contracts/modules.py`，导出为 `modules.json` | CLI、Daemon、前端（`GET /api/v1/modules`） |
| C2 | CLI `--json` 输出与命令之间交换的文件 | `cli/*.schema.json` | Daemon、对账工具 |
| C3 | 进度协议（stderr 上的 JSON Lines） | `progress.schema.json` | Daemon → SSE |
| C4 | REST API | `openapi.yaml`（报告、计划、预检等直接引用 C2 的 Schema） | 前端、Agent、`curation task …` |
| C5 | Repository 接口与状态机 | `backend/daemon/repo/protocol.py` | Daemon 内部 |
| EEF | EEF–视频一致性的输入格式（`eef-video/1.0.0` 四段 + `trajectory-bundle/1.0` 单文件容器），规范正文 `eef/format.md`、迁移规则 `eef/migration.md` | `eef/*.schema.json` | `check` 的 EEF runner、预检、（F5.5 起）Daemon 上传校验 |

C2 各文件对应的命令与产物：

| 文件 | 内容 |
|---|---|
| `preflight` / `plan` / `source-manifest` | 三条命令的 `--json`；`source-manifest` 也是 `snapshot` 写出的 `source_manifest.json` |
| `autolabel`、`autolabel-line` | `autolabel --json` 与 `autolabel/captions.jsonl` 的每一行 |
| `result-record` | `check` 写的每一行（`parts/*.jsonl`、`results.jsonl`）；对账工具导出 v1 结果也用它 |
| `check` | `check --json`：逐模块、逐状态的计数，Daemon 据此定模块状态 |
| `verdict-line`、`final-list`、`aggregate` | 漏斗判决行、四份终判清单、`aggregate --json` |
| `commit` | 结果版本的 `commit.json`，最后写 |
| `export-manifest`、`export` | 交付数据集清单与 `export --json` |
| `report`、`report-output` | `report.json` 与 `report --json` |
| `decisions`、`adjudicate-apply` | 裁决的输入与 `adjudicate-apply --json` |
| `verify` | `verify --json` |
| `error` | 任何命令非零退出时 `--json` 打出的错误信封 |
| `common` | 公共定义 |
| `parity/vlm-tape-entry` | 对账工具的录制带格式（v2 的回放后端读同一种格式） |

## 怎么用

```python
from curation.contracts import schemas
schemas.validate("cli/preflight.schema.json", payload)
schemas.validate("openapi.yaml#/components/schemas/TaskCreate", body)
```

实现方的测试（W3 的 CLI、W4 的 API）用它校验自己的真实输出，这才是契约真正生效的地方。

## 改契约的流程

1. 改文件；不兼容的改动把对应的 `schema_version` / `info.version` / `registry_version` 升一级。
2. 在 `examples/` 里补上合法与不合法的示例。
3. `cd backend && ../.venv/bin/python -m curation.contracts export-modules`（只在改了 C1 时）
   和 `../.venv/bin/python -m curation.contracts lock`。
4. 提交时 `CONTRACTS.lock` 的差异让评审一眼看到哪些契约动了。没刷新锁，CI 就红。

## 手动验证步骤

在 `backend/` 下执行：

```bash
../.venv/bin/python -m pytest -q tests                  # 契约测试，约 2 秒
../.venv/bin/python -m curation.contracts check          # 无输出、退出码 0 = 契约与锁一致
```

逐项核对：
- 把 `cli/check.schema.json` 里随便一个 `minimum` 改掉再跑 `check`，应报 `cli/check.schema.json: changed since CONTRACTS.lock`、退出码 1；改回来恢复。
- `examples/` 下每个文件的 `valid` 都能通过、`invalid` 都会被拒；测试里对应 `test_examples[...]`。
- `modules.json` 与 `GET /api/v1/modules` 的内容一致（前端的模块清单只从这里来）。

## EEF 输入格式（F5.1 冻结，2026-09-23）

`eef/` 下是 EEF–视频一致性模块（12 篇）的输入契约：`sample`、`frame`、`calibration`、`observation` 四份 Schema 与单文件容器
`trajectory_bundle`，规范正文在 `eef/format.md`，现有数据的迁移规则在 `eef/migration.md`。Schema 只管结构；跨段语义（引用一致、
单位四元数、SO(3)、时间单调、提供投影与重算投影之差等）由 `backend/curation/extensions/eef_consistency/load.py` 检查，
测试在 `backend/tests/eef/`。最小的合法与不合法示例在 `examples/eef-*.json`。
