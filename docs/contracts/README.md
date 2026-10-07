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
| C7 | mcap 字段映射（设计 18 §6，D62）：哪些 topic 是相机、曲线、任务描述与分段，可视化与质检共用 | `viz-mapping.schema.json` | Daemon（数据集的映射、模版库、可视化的 mcap 读取器、任务开始时冻结）、前端（「mcap 配置」）、质检（派生 `ingest.mcap_mapping`） |
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

2026-10-05：EEF 1.0 兼容增补 UMI 输入（设计 20）。sample 可携带 `umi`（相机所属手、历史时间窗口、来源）；
frame 可携带 `hands`（同一参考系的绝对位姿和夹爪开口，缺测为 null）。必须同时提供，读取器验证名称、参考系和时间轴。
模型意见 `aspect` 增加 `action`，原有输入与结果仍可读取；该分类只用于 UMI 动作意见，不改变策略判决。

```python
from curation.contracts import schemas
schemas.validate("cli/preflight.schema.json", payload)
schemas.validate("openapi.yaml#/components/schemas/TaskCreate", body)
```

实现方的测试（W3 的 CLI、W4 的 API）用它校验自己的真实输出，这才是契约真正生效的地方。

## 改契约的流程

1. 改文件；不兼容的改动把对应的 `schema_version` / `info.version` / `registry_version` 升一级。
2. 在 `examples/` 里补上合法与不合法的示例。C4 的常用接口还在 `openapi.yaml` 里就地带着请求 / 响应示例（媒体类型的 `examples`，
   API 文档页把它们放在每个接口旁边，见设计 07 §2）：改了这些接口的结构，就地的示例要跟着改；
   `backend/tests/contracts/test_openapi.py` 逐条拿所在位置的 Schema 校验（`test_every_example_fits_its_schema`），
   并要求常用接口都有示例（`EXAMPLES_EXPECTED`）。
   C4 里给客户看的文案（`info.description` 的概览 / 约定 / 变更记录、tag 的 `description`、示例的 `summary`）要写中英两版：英文在原字段，
   中文在 `x-description-zh` / `x-summary-zh`；文案里不写内部编号（C4、D36、F12.3、设计 18 §4.0、registry 1.5 等）。变更记录从 2.5.1 记起，
   写给客户看的改动，不写内部过程。接口自身的说明（`summary`、`description`、参数与字段描述）只写英文，可以带内部编号，发布到
   `{base}/openapi.json` 时由 `frontend/src/lib/publicText.ts` 去掉。测试：`test_the_reference_copy_has_both_languages_and_no_internal_references`、
   `test_the_changelog_starts_at_the_first_published_version`，前端 `src/lib/publicText.test.ts`（发布的版本里一个内部编号都不剩）。
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
- `openapi.yaml` 里就地的示例（C4 2.5.1 起）都能通过所在位置的 Schema：把 `POST /tasks` 的 201 示例里 `state: queued` 改成 `state: sleeping`
  再跑 `pytest -q tests/contracts/test_openapi.py`，`test_every_example_fits_its_schema[#/paths/~1tasks/post/responses/201/… queued]` 失败；改回来恢复。
- `modules.json` 与 `GET /api/v1/modules` 的内容一致（前端的模块清单只从这里来）。
- 注册表 2.0 与分类表（C6）：本机起 Daemon（`.claude/launch.json` 的 `curator-daemon-dev`）后
  `curl -s localhost:8080/curation/api/v1/modules | jq '.registry_version, .taxonomy_version, [.blocks[].stages], (.modules[] | {id, block, stage, covers})'`
  应得到 `"2.0"`、`"1.1"`、两块的段，每个模块的块、段与覆盖；`jq '.modules[] | select(.id=="task_success") | .codes'` 列出它的五个细码
  （`failure` 是 blocking 且可复议，`uncertain` 是 review、裁决线 `task_verdict`）。改了细码或覆盖要重新生成回归样本分类表的平台注记
  （`PYTHONPATH=tools .venv/bin/python -m regression_samples.coverage_from_registry`）。
- 2.0 与 1.0 两种写法：`examples/result-record.json`、`report.json`、`plan.json` 等改过的六份里，`valid` 同时有 2.0 与 1.0 的实例，
  `invalid` 里有把两种写法混用的（2.0 记录带 `verdict`、1.0 计划带 `block`）。
- 数据可视化（C4 2.4.0、C7，设计 18）：`examples/viz-mapping.json` 的四个合法映射（UMI、ABC-130k、ROS 2、只有附件分段）通过、
  十二个不合法的被拒（版本不对、少 `series`、相机没名字、`role` 不认识、点路径写错、不认识的变换、`message_timestamp` 没给字段、
  任务两种写法混用、分段少字段、不认识的内置模版、多出 `episode_files`、`ignore` 重复）。起 Daemon 后
  `curl -s 'localhost:8080/curation/api/v1/datasets?viz=true' | jq '.items[] | {name, format, viz}'` 只列 LeRobot 与 mcap，
  每条带 `viz.state`（`ready`；mcap 没确认映射时 `mapping_pending` 并写原因）；`jq '.viz_mapping, .annotations'` 看单个数据集的详情
  （非 mcap 的 `viz_mapping` 为 null）。可视化的数据接口在 F13.2 / F13.3 落地之前回 404（`daemon/operations.py` 的 `PENDING`）。

## EEF 输入格式（F5.1 冻结，2026-09-23）

`eef/` 下是 EEF–视频一致性模块（12 篇）的输入契约：`sample`、`frame`、`calibration`、`observation` 四份 Schema 与单文件容器
`trajectory_bundle`，规范正文在 `eef/format.md`，现有数据的迁移规则在 `eef/migration.md`。Schema 只管结构；跨段语义（引用一致、
单位四元数、SO(3)、时间单调、提供投影与重算投影之差等）由 `backend/curation/extensions/eef_consistency/load.py` 检查，
测试在 `backend/tests/eef/`。最小的合法与不合法示例在 `examples/eef-*.json`。
