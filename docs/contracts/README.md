# 冻结的契约（W2 / F1.3）

并行开发的地基：CLI、Daemon、前端之间只通过这里的文件对话，谁都不直接 import 对方的内部模块（设计 11 篇 §1）。

先读 [`SUMMARY.md`](SUMMARY.md)：一页讲清各份契约的要点、冻结时定下的细节和待拍板的事项（2.0 的改动在第十五节，删掉 1.0 与旧任务兼容的
C1 7.0 / C4 7.0.0 在第二十二节，完整性拦截与坏视频的 C1 7.1 / C4 7.1.0 在第二十三节）。

| # | 契约 | 文件 | 谁读 |
|---|---|---|---|
| C1 | 模块注册表 | `backend/curation/contracts/modules.py`，导出为 `modules.json` | CLI、Daemon、前端（`GET /api/v1/modules`） |
| C2 | CLI `--json` 输出与命令之间交换的文件 | `cli/*.schema.json` | Daemon、对账工具 |
| C3 | 进度协议（stderr 上的 JSON Lines） | `progress.schema.json` | Daemon → SSE |
| C4 | REST API | `openapi.yaml`（报告、计划、预检等直接引用 C2 的 Schema） | 前端、Agent、`curation task …` |
| C5 | Repository 接口与状态机 | `backend/daemon/repo/protocol.py` | Daemon 内部 |
| C6 | 检测项分类表（设计 17 §1.3，D56）：注册表的细码与覆盖都指向它的编号 | `taxonomy.json`（Schema `taxonomy.schema.json`） | 注册表、报告、前端（经 `GET /api/v1/modules` 的 `taxonomy`）、评估工具 |
| C7 | 数据集声明（设计 25 §3，D83；第一层就是设计 18 §6 的 mcap 字段映射，D62）：哪些 topic / 列是相机、曲线、任务描述与分段，位姿、关节、夹爪记录的含义，每路相机的安装方式与内外参、工具、手持夹爪的标定；可视化与质检共用 | `dataset-declaration.schema.json`（`viz-mapping.schema.json` 留一版，`viz-mapping/1.x` 文件读成只有第一层的声明） | Daemon（数据集的声明与版本、模版库、可视化的 mcap 读取器、任务开始时冻结）、前端（「数据集声明」「mcap 配置」）、质检（派生 `ingest.mcap_mapping`；EEF 按声明生成机械臂的轨迹） |
| EEF | EEF–视频一致性的输入格式（`eef-video/1.0.0` 四段 + `trajectory-bundle/1.0` 单文件容器），规范正文 `eef/format.md`、迁移规则 `eef/migration.md` | `eef/*.schema.json` | `check` 的 EEF runner、预检、（F5.5 起）Daemon 上传校验 |

C2 各文件对应的命令与产物：

| 文件 | 内容 |
|---|---|
| `preflight` / `plan` / `source-manifest` | 三条命令的 `--json`；`source-manifest` 也是 `snapshot` 写出的 `source_manifest.json` |
| `stage-umi` | `stage-umi --json`（UMI 原始会话转成 LeRobot 数据集与 `trajectory.json`） |
| `result-record` | `check` 写的每一行（`parts/*.jsonl`、`results.jsonl`）：发现、评估过与评估不了的检测项、读数；对账工具拿它校验记录 |
| `check` | `check --json`：逐模块的计数（ok / error 与按细码的发现数），Daemon 据此定模块状态 |
| `verdict-line`、`final-list`、`aggregate` | 判决行（按策略的 blocking / review）、四份终判清单、`aggregate --json`（只有 `final` 一种） |
| `commit` | 结果版本的 `commit.json`，最后写 |
| `report`、`report-output` | `report.json` 与 `report --json` |
| `decisions`、`adjudicate-apply` | 裁决的输入与 `adjudicate-apply --json` |
| `verify` | `verify --json` |
| `error` | 任何命令非零退出时 `--json` 打出的错误信封 |
| `common` | 公共定义 |
| `parity/vlm-tape-entry` | 对账工具的录制带格式（v2 的回放后端读同一种格式） |

C2 只有一种写法（D90）：设计 17 改过形状的文档（记录、`check`、判决行、清单、报告、计划，以及 `decisions.json`）是 2.0，写成 1.0 的被拒；
其余文档形状没变过，`schema_version` 仍是 1.0。

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
../.venv/bin/python -m pytest -q tests/contracts        # 契约测试，约 20 秒
../.venv/bin/python -m curation.contracts check          # 无输出、退出码 0 = 契约与锁一致
```

逐项核对：
- 把 `cli/check.schema.json` 里随便一个 `minimum` 改掉再跑 `check`，应报 `cli/check.schema.json: changed since CONTRACTS.lock`、退出码 1；改回来恢复。
- `examples/` 下每个文件的 `valid` 都能通过、`invalid` 都会被拒；测试里对应 `test_examples[...]`。
- `openapi.yaml` 里就地的示例（C4 2.5.1 起）都能通过所在位置的 Schema：把 `POST /tasks` 的 201 示例里 `state: queued` 改成 `state: sleeping`
  再跑 `pytest -q tests/contracts/test_openapi.py`，`test_every_example_fits_its_schema[#/paths/~1tasks/post/responses/201/… queued]` 失败；改回来恢复。
- `modules.json` 与 `GET /api/v1/modules` 的内容一致（前端的模块清单只从这里来）。
- 注册表与分类表（C6）：本机起 Daemon（`.claude/launch.json` 的 `curator-daemon-dev`）后
  `curl -s localhost:8080/curation/api/v1/modules | jq '.registry_version, .taxonomy_version, [.blocks[].stages], (.modules[] | {id, block, stage, covers})'`
  应得到 `"7.1"`、`"2.0"`、两块的段（CPU 块 `integrity`、`numeric`、`frame`、`duplicate`、`vlm_prep`，VLM 块 `vlm`），每个模块的块、段与覆盖；
  不起 Daemon 时对 `docs/contracts/modules.json` 跑同样的 `jq`。`jq '.modules[] | select(.id=="task_success") | .codes'` 列出它的三个细码
  （`failure` 是 blocking 且可复议，`uncertain` 是 review、裁决线 `task_verdict`，`task_text_missing` 是 info）。改了细码或覆盖要重新生成回归样本分类表的平台注记
  （`PYTHONPATH=tools .venv/bin/python -m regression_samples.coverage_from_registry`）。
- 只有 2.0（D90）：`examples/result-record.json`、`report.json`、`plan.json` 等改过的几份里，`valid` 只有 2.0 的实例，
  `invalid` 里各有一份 1.0 写法（记录带 `verdict` / `passed` / `gate`，计划、报告、清单等写 `schema_version: "1.0"`），应被拒。
- 数据可视化（C4 2.4.0、C7，设计 18）：`examples/viz-mapping.json` 的五个合法映射（UMI、ABC-130k、ROS 2、只有附件分段，
  以及 1.1 带深度图的 RoboMIND）通过、十四个不合法的被拒（版本不对、少 `series`、相机没名字、`role` 不认识、点路径写错、不认识的变换、
  `message_timestamp` 没给字段、任务两种写法混用、分段少字段、不认识的内置模版、多出 `episode_files`、`ignore` 重复，另有两份 `depths` 写错的）。起 Daemon 后
  `curl -s 'localhost:8080/curation/api/v1/datasets?viz=true' | jq '.items[] | {name, format, viz}'` 只列 LeRobot 与 mcap，
  每条带 `viz.state`（`ready`；mcap 没确认映射时 `mapping_pending` 并写原因）；`jq '.viz_mapping, .annotations'` 看单个数据集的详情
  （非 mcap 的 `viz_mapping` 为 null）。

## EEF 输入格式（F5.1 冻结，2026-09-23）

`eef/` 下是 EEF–视频一致性模块（12 篇）的输入契约：`sample`、`frame`、`calibration`、`observation` 四份 Schema 与单文件容器
`trajectory_bundle`，规范正文在 `eef/format.md`，现有数据的迁移规则在 `eef/migration.md`。Schema 只管结构；跨段语义（引用一致、
单位四元数、SO(3)、时间单调、提供投影与重算投影之差等）由 `backend/curation/extensions/eef_consistency/load.py` 检查，
测试在 `backend/tests/eef/`。最小的合法与不合法示例在 `examples/eef-*.json`。
