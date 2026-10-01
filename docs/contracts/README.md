# 冻结的契约（W2 / F1.3）

并行开发的地基：CLI、Daemon、前端之间只通过这里的文件对话，谁都不直接 import 对方的内部模块（设计 11 篇 §1）。

先读 [`SUMMARY.md`](SUMMARY.md)：一页讲清各份契约的要点、冻结时定下的细节和待拍板的事项（2.0 的改动在第十五节）。

| # | 契约 | 文件 | 谁读 |
|---|---|---|---|
| C1 | 模块注册表 | `backend/curation/contracts/modules.py`，导出为 `modules.json` | CLI、Daemon、前端（`GET /api/v1/modules`） |
| C2 | CLI `--json` 输出与命令之间交换的文件 | `cli/*.schema.json` | Daemon、对账工具 |
| C3 | 进度协议（stderr 上的 JSON Lines） | `progress.schema.json` | Daemon → SSE |
| C4 | REST API | `openapi.yaml`（报告、计划、预检等直接引用 C2 的 Schema） | 前端、Agent、`curation task …` |
| C5 | Repository 接口与状态机 | `backend/daemon/repo/protocol.py` | Daemon 内部 |
| C6 | 检测项分类表（设计 17 §1.3，D56）：注册表的细码与覆盖都指向它的编号 | `taxonomy.json`（Schema `taxonomy.schema.json`） | 注册表、报告、前端（经 `GET /api/v1/modules` 的 `taxonomy`）、评估工具 |
| EEF | EEF–视频一致性的输入格式（`eef-video/1.0.0` 四段 + `trajectory-bundle/1.0` 单文件容器），规范正文 `eef/format.md`、迁移规则 `eef/migration.md` | `eef/*.schema.json` | `check` 的 EEF runner、预检、（F5.5 起）Daemon 上传校验 |

C2 各文件对应的命令与产物：

| 文件 | 内容 |
|---|---|
| `preflight` / `plan` / `source-manifest` | 三条命令的 `--json`；`source-manifest` 也是 `snapshot` 写出的 `source_manifest.json` |
| `autolabel`、`autolabel-line` | `autolabel --json` 与 `autolabel/captions.jsonl` 的每一行 |
| `result-record` | `check` 写的每一行（`parts/*.jsonl`、`results.jsonl`）；对账工具导出 v1 结果也用它。2.0 是发现与覆盖，1.0 是漏斗的三态（旧任务仍可读） |
| `check` | `check --json`：逐模块的计数（2.0：ok / error 与按细码的发现数；1.0：逐状态），Daemon 据此定模块状态 |
| `verdict-line`、`final-list`、`aggregate` | 判决行（2.0：按策略的 blocking / review；1.0：漏斗）、四份终判清单、`aggregate --json` |
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
- 注册表 2.0 与分类表（C6）：本机起 Daemon（`.claude/launch.json` 的 `curator-daemon-dev`）后
  `curl -s localhost:8080/curation/api/v1/modules | jq '.registry_version, .taxonomy_version, [.blocks[].stages], (.modules[] | {id, block, stage, covers})'`
  应得到 `"2.0"`、`"1.1"`、两块的段，每个模块的块、段与覆盖；`jq '.modules[] | select(.id=="task_success") | .codes'` 列出它的五个细码
  （`failure` 是 blocking 且可复议，`uncertain` 是 review、裁决线 `task_verdict`）。改了细码或覆盖要重新生成回归样本分类表的平台注记
  （`PYTHONPATH=tools .venv/bin/python -m regression_samples.coverage_from_registry`）。
- 2.0 与 1.0 两种写法：`examples/result-record.json`、`report.json`、`plan.json` 等改过的六份里，`valid` 同时有 2.0 与 1.0 的实例，
  `invalid` 里有把两种写法混用的（2.0 记录带 `verdict`、1.0 计划带 `block`）。

## EEF 输入格式（F5.1 冻结，2026-09-23）

`eef/` 下是 EEF–视频一致性模块（12 篇）的输入契约：`sample`、`frame`、`calibration`、`observation` 四份 Schema 与单文件容器
`trajectory_bundle`，规范正文在 `eef/format.md`，现有数据的迁移规则在 `eef/migration.md`。Schema 只管结构；跨段语义（引用一致、
单位四元数、SO(3)、时间单调、提供投影与重算投影之差等）由 `backend/curation/extensions/eef_consistency/load.py` 检查，
测试在 `backend/tests/eef/`。最小的合法与不合法示例在 `examples/eef-*.json`。
