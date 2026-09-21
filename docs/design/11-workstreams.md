# 11 并行工作包与接口冻结

> 开发阶段会开多个 subagent 并行推进。并行的前提是**接口先冻结**——
> 接口不冻死，并行就会互相打架，合并时的返工比串行还贵。

## 1. 两条铁律

1. **W2（契约冻结）完成前，任何实现类工作包不得开工。**
2. **跨包通信只走冻结的契约**，不直接 import 对方的内部模块。
   需要对方能力时，先改契约（走评审），再各自实现。

## 2. 工作包

| 包 | 名称 | 产出 | 依赖 | 可并行 |
|---|---|---|---|---|
| **W0** | 对账工具与黄金基线 | `curation-parity` 工具 + v1 侧导出脚本 `dump_v1.py` + 两个基线的 `golden/v1/` 存档（含 v1 自身的噪声底） | 无 | — |
| **W1** | Baseline 清理与重组 | 新目录结构、依赖清理、测试挑拣 | 需需求方点头删上游 | — |
| **W2** | 契约冻结 | CLI JSON schema、REST OpenAPI、Repository 接口、模块注册表、进度协议 | W1 | — |
| **W3** | CLI 层 | 10 条原子命令（preflight / plan / snapshot / autolabel / check / aggregate / export / report / adjudicate-apply / verify）+ 客户端命令 `curation task …` + 辅助命令 | W2（客户端命令另依赖 W4 的 OpenAPI） | ✅ |
| **W4** | Daemon 骨架 | FastAPI、SQLite Repository、鉴权、SSE、静态资源、挂载前缀、启动对账 | W2 | ✅ |
| **W5** | 任务编排 | 状态机（含待启动、系统暂停）、worker 池、四种子任务、暂停恢复、工作目录与回灌、日志 | W4 | ✅ |
| **W6** | planner 与 VLM 合并 | 执行计划、闸门推导、合并框架（现有模块不参与，示例模块验证）、usage 采集、外层重试、自适应降并发 | W2 | ✅ |
| **W7** | 增量导出 | manifest、diff 算法、v2/v3 两条路径 | W2 | ✅ |
| **W8** | 密钥与资源管理 | 加密存储、验证状态标记、任务开始前的三项检查、模型列表（能拉就拉，拉不出手填）、思考强度映射表 | W4 | ✅ |
| **W9** | 前端静态稿 | 6 页 HTML mockup（先 2 页定风格） | 无（可最先开） | ✅ |
| **W10** | 前端实现 | React 工程、6 个页面 | W2 + W9 定稿 | ✅ |
| **W11** | 镜像与 Chart | Dockerfile、Helm Chart | W3 + W4 | ✅ |
| **W12** | 对账执行 | 逐模块对账报告、合并对账报告 | W0 + W3 + W6 | — |

## 3. 推进批次

```
批次 0（串行，谁也绕不开）
   W0 对账工具 + 黄金基线   ←── 工具最先做；基线必须在 W3 动 B 类代码之前存档
   W1 baseline 清理
   W2 契约冻结
        │
批次 1（大并行）
   ├── W3 CLI 层 ────────┐
   ├── W4 Daemon 骨架 ───┤
   ├── W6 planner/合并 ──┤
   ├── W7 增量导出 ──────┤
   └── W9 前端静态稿 ────┘   （W9 其实可以和批次 0 同时开，它不依赖任何代码）
        │
批次 2
   ├── W5 任务编排（依赖 W4）
   ├── W8 密钥管理（依赖 W4）
   └── W10 前端实现（依赖 W2 + W9 定稿）
        │
批次 3
   ├── W11 镜像与 Chart
   └── W12 黄金对账 + 合并对账
```

**W9 建议和批次 0 同时启动**：静态稿不依赖任何后端代码，而它的评审需要需求方的时间，
早开早定，避免后面卡在等风格确认。（2026-09-20：第一批两页的风格已确认。）

**批次 0 的一处放宽**（2026-09-20 开工前定）：W0 分两半 —— 工具本身（导出脚本、对账工具、录制与回放、夹具与测试）
不依赖任何凭证，最先做；基线生成要在现网 Pod 里跑，等环境的这段时间 W1、W2 照常推进
（W1 只搬位置、删退役代码，W2 只定契约，都不碰算法）。卡点改为：**W3 改动 B 类代码之前，两个基线必须存档**。

## 4. W2 要冻结的五份契约

这是整个并行开发的地基，按此顺序定稿：

| # | 契约 | 文件 | 消费方 |
|---|---|---|---|
| C1 | 模块注册表 | `backend/curation/registry/modules.py` | CLI、Daemon、前端 |
| C2 | CLI `--json` schema（含规范化的 `results.jsonl` 行，对账工具也消费它；这一行的格式已由 W0 先行定稿为 `result-record.schema.json`） | `docs/contracts/cli/*.schema.json` | Daemon、对账工具 |
| C3 | 进度协议（stderr JSON Lines） | `docs/contracts/progress.schema.json` | Daemon → SSE |
| C4 | REST OpenAPI | `docs/contracts/openapi.yaml` | 前端 |
| C5 | Repository 接口 | `backend/daemon/repo/protocol.py` | Daemon 内部 |

**三份 schema 文件是真的文件，不是文档里的示意**：CI 用它们做契约测试，
任何一方改了 schema 而没同步改测试，CI 就红。

## 5. 每个包的验收标准

通用要求（每个包都要满足，不再逐包重复）：

- 单元测试覆盖核心路径，CI 绿。
- 契约变更同步更新 schema 文件与契约测试。
- 面向用户的文案过一遍 humanizer-zh 规则。
- `README.md` 里该包的手动验证步骤可执行（规范要求，不是装饰）。

逐包的特有验收：

| 包 | 验收 |
|---|---|
| W0 | 对 v1 自己的两次跑批做对账：确定性六项必须「完全一致」（工具自身的正确性验证）；VLM 三项给出 v1 自身的波动基线。两个基线数据集都要有存档，存档里含 v1 的 VLM 请求哈希与响应，可供回放。另加：基线跑批零失败；v1 接回放后端再跑一遍，请求 100% 命中、全部记录逐位一致 |
| W1 | 清理后 CLI 能跑、搬运的测试全绿、镜像能构建 |
| W3 | 每条命令 `--json` 输出通过 schema 校验；`preflight`→`verify` 全链路跑通 8 条 episode；`check --resume` 在中途被 SIGTERM / SIGKILL 后续跑，结果与一次跑完一致；带 `--source-manifest` 时源对象被改过即以退出码 6 结束；执行出错与正常弃权在结果里严格区分 |
| W5 | 暂停/恢复后结果与不暂停一致；停止后无孤儿子进程；崩溃重启后任务状态可恢复且系统暂停的任务自动续跑，用户暂停的保持暂停、用户停止的不恢复；优雅停机超时不把任务置为失败；`stopped` / `failed` 的任务「继续运行」后不重复已完成的工作；出错的 episode 不进后面的档、不进交付，补跑后按当前结果重算任务终态；新报告完整生成后才切换；反复搞崩进程的 episode 被点名跳过而不拖死任务；同一交付目录的发布串行，`latest` 只被完整成功的版本更新 |
| W6 | 示例模块上：合并模式与单发模式判决一致率 ≥98%（见 10 篇 §3.4）、分摊账合计等于实际账且任务总量只算实际账、单项解析失败只降级那一项；N=64 时推导出的八把闸门与 v1 出厂默认逐项相等；现有两个 VLM 模块的计划里 `merge.strategy` 恒为 `none` |
| W7 | 剔除中间一条 episode 后重新导出：产物可被官方 lerobot loader 无警告加载；未受影响的视频文件字节不变；只改了任务文本的条目（人工改标）不触发视频拷贝或重编码；待裁决条目在导出结果里、待补跑条目不在；另用一个小的 v3 夹具跑通同样的加载检查（不与 v1 逐位比） |
| W4 | 探针免鉴权且在根路径与前缀下都可达；`/curation` 前缀下全部路由可用、旧深链入口 302 正确；SSE 支持 `Last-Event-ID` 重放；主密钥缺失拒绝启动；任务列表页码分页的 `total` 正确 |
| W8 | 任何 API 响应与日志都不含密钥本体；保存 TOS 访问密钥时只验身份，任务开始前的输出检查含写探针；验证失败的密钥可保存但有标记，拿它开始任务会被三项检查拦下；`/models` 拉不出来时可以手填模型；思考强度为空时请求体里没有 `reasoning_effort` 字段 |
| W11 | VKE 上 helm install 一次成功（StatefulSet + EBS 数据卷）；升级时运行中任务被系统暂停、新 Pod 起来后自动续跑；`basePath=/curation` 下经 APIG 可用 |
| W10 | 深链参数行为与 v1 逐项一致（v1 的深链测试用例全部搬过来并通过）；在 `/curation` 前缀下刷新任意页面不 404；SSE 断线自动降级轮询；表单必填校验全覆盖 |
| W12 | 两个基线上：确定性六项逐位一致；VLM 三项回放对账请求 100% 命中且记录逐位一致，真模型比对判决差异 <2% 且不超过 v1 噪声底的 1.5 倍，逐条人工确认；droid 基线上同一组裁决输入的执行结果逐位一致。v3 格式本期不做对账 |

## 6. 给并行 agent 的分工提示

每个 subagent 启动时应当拿到：

1. 本册的 `00-overview.md` + 它负责的那一篇。
2. 冻结的契约文件（C1–C5），**只读**。
3. 明确的边界：「你只改 `backend/daemon/orchestr/` 下的文件」这种级别的目录所有权。
4. 对 A 类代码的纪律：**不得修改** `core/checks/` 下任何文件，
   需要改动时停下来报告，不自行决定。

目录所有权划分（避免并行写冲突）：

```
backend/curation/core/        ← 冻结，谁也不改（W1 只搬位置）
backend/curation/cli/         ← W3
backend/curation/pipeline/    ← W3（stage 拆分）+ W6（planner）需协调，建议同一 agent
backend/curation/export/      ← W7
backend/daemon/repo/          ← W4
backend/daemon/routes/        ← W4（骨架）+ W8（密钥路由）
backend/daemon/orchestr/      ← W5
backend/daemon/planner/       ← W6
frontend/mockups/             ← W9
frontend/src/                 ← W10
deploy/                       ← W11
tools/parity/                 ← W0
```

## 7. 外置记忆三件套

按仓库规范，长任务上下文用三件套管理，**每个工作包收尾必须更新**：

| 文件 | 位置 | 作用 |
|---|---|---|
| `feature_list.md` | 仓库根 | 需求账本：feature 列表、验收标准、完成状态。**需求描述与验收标准一经确定不得改写**，只更新状态位 |
| `claude-progress.txt` | 仓库根 | 当前进度、阻塞点、下一步。`TaskStatus` 只能是 `completed` / `not_completed` |
| git | — | 每个 feature 一个完成点 commit，可回滚可追溯 |

每个 feature 的固定流程：定义 → 实现 → 测试验证 → 更新 README → commit。
