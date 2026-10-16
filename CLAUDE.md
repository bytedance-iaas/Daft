# Curator v2 · 开发说明

Curator v2 是 Physical AI Kit 的机器人数据质检平台。它读入机器人操作数据集（LeRobot v2 / v3、mcap、Lance，另有只跑 EEF 的 UMI 原始会话；
来源是私有 TOS、HuggingFace 缓存桶，或站点开放的本地挂载路径），逐条 episode 跑一串质检模块，给出通过 / 拒绝 / 待定的判决和待人工复核的清单；
人工裁决后重判受影响的条目，生成质检报告，并把报告与结果清单写回 TOS 上的交付目录（2026-10-07 的 D69 起不再导出交付数据集）。控制台还内置数据可视化：一条进度条同步播放多路相机
与运动曲线，完整版是「数据集 › 可视化」页，报告、人工裁决与任务详情里的迷你版定位到每条发现（设计 18、19）。
产品是「网页控制台 + REST API + 命令行」三件套，打成一个镜像，作为 rerun 仓库 dataverse Helm Chart 的一个组件部署在火山引擎 VKE 上（D53）。

## 架构

```
前端控制台   React 18 + Arco，纯静态，所有状态都向 Daemon 要
    │  HTTP/JSON（C4）+ SSE（进度、日志）
API Daemon   FastAPI 单副本：routes → orchestr / planner / exec → repo（SQLite）
    │  子进程：argv 传参，stdout 出一份 --json（C2），stderr 出进度（C3）
命令行       curation 原子命令，命令之间靠运行目录里的文件交接
    │
内核         质检算法、数据集读取、报告生成、VLM 客户端
```

| 组件 | 位置 | 做什么 |
|---|---|---|
| 前端控制台 | `frontend/` | 概览、数据集（列表、详情、「可视化」页）、质检任务（新建 / 列表 / 详情）、质检报告、人工裁决、系统和资源配置，报告、裁决与任务详情里的迷你播放器；构建产物由 Daemon 托管 |
| API Daemon | `backend/daemon/` | REST 与 SSE、SQLite 仓储、鉴权、任务编排（排队、两块并行的逐条流水线、暂停 / 停止 / 继续、崩溃恢复、子任务、发布到交付目录）、密钥与 VLM 后端管理、结果读取、数据可视化（`viz/`：LeRobot / mcap / Lance / UMI 会话读取器 → 统一展示模型，曲线、帧包与样本包、转封装 / 转码，按区间读 TOS） |
| 命令行 `curation` | `backend/curation/cli/` | `preflight → plan → snapshot → check（一次跑一段）→ aggregate → report → verify`，另有 `adjudicate-apply`、REST 薄客户端 `curation task …`、把 UMI 原始会话转成 LeRobot 的离线工具 `stage-umi` 和运维命令 `ls`、`prune`、`fetch`；Daemon 是它最大的用户 |
| planner | `backend/curation/planner/` | 执行计划：分段、并发、VLM 闸门（`episode`、`probe`）；请求合并与自适应降并发的框架在这里，现有模块都不合并；`curation plan` 与 Daemon 共用 |
| 内核 | `backend/curation/` 下的 `core/`、`registry/`、`ingest/`、`dataset_level/`、`export/`、`pipeline/`、`adapters/`、`streams/`、`declaration/`、`viz/` | 算法（`core/` 是纯函数：不碰 I/O、不 import daft）、机器人规格库、读取器、报告生成（`export/`）、编排壳、VLM 客户端与视频输入、按块读 TOS 与块缓存（`streams/`，设计 23）、数据集声明的校验与起草（`declaration/`，C7）；`viz/` 是可视化的格式解析（曲线分组与抽稀、标注识别、mcap 探测 / 映射 / 扫描、Annex-B 与转封装、Lance 布局、转码） |
| 扩展模块 | `backend/curation/extensions/` | `eef_consistency`（EEF–视频一致性，设计 12、25）、`integrity`（数据完整性，设计 14）、`camera_defects`（镜头画面缺陷与机位身份，骑在 task_success 那一次判定请求上作答，设计 13） |
| 对账工具 | `tools/parity/` | 黄金基线的录制、回放、比对，假模型，A 类守卫 |
| 回归样本工具 | `tools/regression_samples/` | 回归样本集（设计 16）的合成注入、平台结果打分（按检测项的 precision / recall、与基线比较） |
| 部署 | `deploy/` | 一个镜像（Daemon + CLI + 前端产物，缺省起 Daemon）；Helm Chart 在 rerun 仓库 `deploy/helm/dataverse`（StatefulSet 单副本 + 数据盘，D53） |

Daemon 用子进程调 CLI，不在进程内 import：原生库崩溃只带走子进程；暂停、停止就是给进程组发信号；CLI 也因此一直是活的一等入口
（设计 00 §2.1）。数据可视化是例外：它只读、不碰判决，`daemon/viz` 在进程内调 `curation/viz` 读数据集，只有转码在子进程里
（`python -m curation.viz.transcode`）。

**两块并行**（设计 17，D57，F12.4 起的执行）：CPU 块 `integrity`（data_integrity）→ `numeric`（timestamp_check、kinematic_limits、motion_quality）
→ `frame`（visual_quality、video_action_sync）→ `duplicate`（duplicate_check），选了 EEF 时另有一条根链 `vlm_prep`（EEF 的 CPU 半段，设计 23、25）；
VLM 块 `vlm`（eef_video_consistency 的模型半段、task_success，camera_defects 骑在 task_success 的请求上；没有任务标注的条目不判成败、
只发一次画面缺陷请求，D72 / D73；task_success 每条恰好一次模型请求，没有复核、仲裁和判废护栏）。两块同时跑、互不过滤：每一段都拿全部所选条目，一条在本段有了记录（判完或出错）就交给
本块下一段。例外是**完整性拦截**（D92，设计 28）：完整性判废的条目，其它模块不读数据、不调模型，只写一条跳过记录（`details.skipped`），VLM 块
与 `vlm_prep` 链逐条等完整性有了结论再派发；**坏视频是发现**（D93）：解码器报文件损坏（`InvalidDataError` 等）时，视觉质量、同步、任务成败各报一条判废的
`decode_failed`（FILE-4），不算执行出错、不进待补跑；跳过的条目在计数、报告与控制台里单列（D94）。**声明了却缺的数据有判决**（D95–D97，设计 29）：
v1 读取器读不下去时完整性按数据集的声明找原因（缺声明的列、空值或维度参差、维度与 info.json 不符、行数与 episode 表不符、mcap 缺动作 topic），判废而不是
执行出错；选了数据完整性的任务里缺数据文件或某路视频的条目判废、计入总数（`run.json` 的 `missing_sources`），没选时照 D40 剔除。task_success 让 VLM 读多机位连续视频判定成败（设计 13）。**重复检测只报告**（D87，设计 26）：动作与各路画面字节相同的一组条目，每一条都得到
一条 SET-1 的 info 发现、写明同组其余几条；不判废、不留第一条、不能复议（精确去重已删）。控制台把段**按组显示**（D88）：数据完整性、CPU 检查
（numeric、frame、duplicate、vlm_prep）、模型检查（vlm）、生成报告；后台的段、计划与日志仍用段 id，前端负责归组（`frontend/src/lib/taskView.ts` 的 `STAGE_GROUPS`）。
**判决是策略判决**（F12.2–F12.3）：模块只报发现（记录 2.0：细码、分类表的项、严重度、范围与区间），两块都结束后 `aggregate` 用任务的策略
（`run.json` 冻结；`default` 复刻今天的硬门、不再有软分拒绝，`report_only` 只报不拒，但完整性的判废与各模块的 `decode_failed` 照样拒，策略版本 3）
给每条发现定级 blocking / review / info，再判
keep / drop / held（`pipeline/policy.py`、`pipeline/verdicts.py`）：有 blocking 发现即拒，否则任一所选模块出错或没有记录即待补跑。

**一次任务**：建任务时预检，开跑时生成并冻结执行计划 → Daemon 排队，两块同时调 CLI（每个逐条段一个常驻 worker，episode 逐条交接）→
结果落在运行目录 `runs/<task_id>/`（`checks/<模块>/`、结果版本 `revisions/rNNNN/` 里的清单与报告）→ 同步到交付目录，
最后写 `_COMPLETE` → 前端经 REST / SSE 看进度和报告。重试、继续运行、执行裁决都作为子任务跑；执行裁决会产生新的结果版本。

## 目录

| 路径 | 内容 |
|---|---|
| `backend/curation/` | 内核、编排壳 `pipeline/`、命令行 `cli/`、planner、C1 注册表与 Schema 校验 `contracts/`、扩展模块 `extensions/`、可视化的格式解析 `viz/`；内核单测在包内 `tests/` |
| `backend/daemon/` | API Daemon：`routes/`（REST、SSE、静态资源）、`orchestr/`（编排）、`exec/`（CLI 执行器）、`repo/`（C5 与 SQLite 实现）、`results/`（结果读取）、`secrets/`（密钥封存）、`viz/`（数据可视化：数据源、读取器、缓存与转码池，说明见 `viz/README.md`）；`python -m daemon` 或 `curator-daemon` |
| `backend/tests/` | v2 的测试，按工作包分目录：`cli`、`contracts`、`daemon`、`orchestr`、`results`、`planner`、`secrets`、`eef`、`deploy`、`viz` |
| `backend/scripts/` | 零散脚本：测试数据下载（`download_test_data.py`）、找空闲 GPU（`free_gpus.py`）、mcap 孪生数据集（`make_mcap_twin.py`）；可视化的样例数据（`make_cams_dataset.py` 多路相机、`make_lance_dataset.py` Lance 三种布局）、样本集实测（`viz_sample_check.py`）与 v3 切片读取实测（`viz_slice_reads.py`） |
| `frontend/` | 网页控制台（React + Arco），接口类型由 `docs/contracts/openapi.yaml` 生成（改了 C4 要跑 `npm run gen:api`）；播放器在 `src/features/visualizer/`（完整版与迷你版共用），「可视化」页在 `src/pages/visualize/` |
| `frontend/mockups/` | 静态 HTML 预览稿（只读参考；可视化的三页由 `viz-src/` 生成：改 `viz-src/`，再在该目录跑 `python3 viz-src/build.py .`） |
| `tools/parity/` | 对账工具与黄金基线流程 |
| `tools/regression_samples/` | 回归样本集的工具：`inject.py`、`inject_mcap.py`、`inject_v3.py` 合成注入，`register.py` 把子集登记成质检台的数据集（清单在 `console_picks.json`（v1 挑的 20 个）与 `console_picks_v2.json`（v2 全部 99 个）），`score.py` 给平台结果打分（只读 C2 2.0 的发现；`finding_map.json` 只剩对照项与预检项），`taxonomy.json` 是检测项分类（平台注记由 `coverage_from_registry.py` 从注册表生成；样本集在 TOS，不在仓库） |
| `tools/eef_eval/`、`tools/eef_convert.py` | EEF 离线评估（唯一读真值的代码）；`trajectory.json` 在 LeRobot 与 mcap 孪生数据集之间互转 |
| `data/umi-test/` | UMI 本手轨迹预览的两个小脚本（渲染标记视频、生成静态页） |
| `deploy/` | 镜像（`deploy/Dockerfile`，构建上下文是仓库根）与集群上的运维步骤（`deploy/README.md`）；Chart 本身在 rerun 仓库的 dataverse 里 |
| `docs/design/`、`docs/contracts/` | 设计、契约 |
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
| 11 | 工作包（W0–W13）与接口冻结顺序 |
| 12 | EEF–视频一致性（DEMO 模块；首节是开工指引，格式规范与 Schema 在 `docs/contracts/eef/`） |
| 13 | 提速（`13-curation-speedup.md`，实施记录在 `13-speedup/`）；视频原生判定（`13-video-native-vlm.md`） |
| 14 | 数据完整性（首节是开工指引；决策 D50–D52） |
| 15 | ReRun 经 Daemon 代签的预签名地址读登记的数据集（`15-rerun-presigned-access.md`，D55；首节是开工指引，涉及 rerun 仓库） |
| 16 | 回归测试样本集（`16-regression-samples.md`：检测项分类表、评测集、期望值与打分；样本与真值在 TOS） |
| 17 | 发现、策略判决与并行两块（`17-findings-and-parallel-blocks.md`，D56–D59；首节是开工指引；取代了逐档漏斗的执行短路与硬门 / 软分判决，§7 末尾是各 feature 落地时的细化） |
| 18 | 数据可视化（`18-data-visualizer.md`：控制台内置播放器——独立的「可视化」页（完整版）与报告 / 裁决 / 任务详情里的迷你版、读取器与统一展示模型、mcap 字段映射模版 C7；决策 D60–D64；首节是开工指引，§9 是各 feature 落地时的细化与样本集实测，§10 是第二期清单；静态稿在 `frontend/mockups/`（`visualize.html`、`episode-visualize-mini.html`、`dataset-add-mcap.html`）） |
| 19 | 可视化第二期先行三项（`19-visualizer-phase-two.md`：相机多于 9 路、浏览器内解码（WebCodecs）、Lance 读取器；决策 D65–D67；首节是开工指引） |
| 20 | UMI 动作投影（`20-umi-action-projection.md`：UMI 原始会话转 LeRobot 与 `trajectory.json`；文中的 `export-umi` 如今是 `curation stage-umi`，标定改为从会话推出，见设计 22、24） |
| 21 | 可视化第二期其余几项（`21-visualizer-phase-two-rest.md`：深度图、v3 切片与转码对齐（含 moov 在尾）、展示配置、数据集信息原文与小修；决策 D68–D71（2026-10-06 这一组，和同号的技能画像下线、数据集导出下线等不是一回事）；首节是开工指引） |
| 22 | 轨迹叠加进迷你播放器；手持夹爪（DAS mcap）的意见路线（`22-eef-overlay-and-umi-mcap.md`：叠加图层与对时、`export-umi-mcap` 与夹爪标定 `umi-calibration/2`、EEF 1.1 的 `per_hand`、插值间隔、自运动一致性、不传 trajectory.json 时平台从录制推出轨迹（内置 DAS DEMO 标定）；决策 D74–D75、D80；首节是开工指引，§7 是 DEMO 的假设与生产输入） |
| 23 | EEF 与任务成败的 CPU 半段挪进 CPU 块、源数据按块读只下载一次、完整性缺省只做 L1（`23-model-prep-and-source-cache.md`，D76–D79 提议；S1 已落地：L2 缺省关，`vlm` 段内 EEF 与任务成败共用按块读的缓存；S3 的 EEF 一半已落地：`vlm_prep` 段；S2、S4 未做） |
| 24 | EEF 的轨迹由平台生成，用户不上传 trajectory.json（`24-eef-trajectory-generated.md`；注册表 4.3；原始手持夹爪 mcap 由设计 22 §5.4 逐条推出） |
| 25 | EEF 重构：只出意见与置信度、不判废，两个渠道按分项取大、冲突才出裁决卡；数据集声明（C7 扩成 `dataset-declaration/1.0`：来源→角色、语义、标定）取代上传件里的声明，轨迹由声明 + 记录生成；叠加分数据集级与任务级（`25-eef-opinion-and-dataset-declaration.md`，D81–D86；首节是开工指引，§10 是开工时的缺省选择；F5.22–F5.26 已落地：注册表 5.4、C4 5.3.1，§9.1–§9.6 是落地记录） |
| 26 | 重复检测取代精确去重；进度按三组显示（`26-duplicate-check-and-progress-groups.md`，D87–D88；首节是开工指引：不留兼容，注册表 6.0、C4 6.0.0） |
| 27 | 删掉 v1 与旧格式（`27-remove-v1-and-old-formats.md`，D89–D91）：v1 代码删掉、`ls` / `prune` / `fetch` 留作运维命令；C2 只有 2.0，漏斗、`relabel_rerun`、旧任务兼容都删；对账与回归打分只认 2.0（注册表 7.0、C4 7.0.0） |
| 28 | 完整性拦截与坏视频（`28-integrity-gate-and-broken-video.md`，D92–D94）：完整性判废的条目其它模块只写跳过记录、VLM 块等完整性结论；解码器认定损坏的视频是判废发现 `decode_failed`；跳过单独记账（注册表 7.1、C4 7.1.0） |
| 29 | 声明了却缺的数据（`29-declared-but-missing.md`，D95–D97）：读不了的数据按声明诊断成判废发现（`action_missing`、`column_missing`、`values_invalid`、`length_mismatch`……），维度与 info.json 不符判废，确认过的 mcap 映射写明的流缺了存疑；选了数据完整性时缺源文件的条目判废、不再剔除（`data_missing` / `video_missing`，修订 D40）；v3 数据文件一条坏了不连累同文件的其它条（注册表 8.4） |
| 30 | 全量实跑暴露的五个问题（`30-full-run-fixes.md`，D98–D102）：给模型的视频按预算降档重编；seek 不到关键帧时从头解、开头解不出报 FILE-4 info（`head_undecodable`）；汇总读不了元数据不再让任务失败、protobuf repeated 字段取数；v3 行区间落在 index 之外报 FILE-9（`index_range_invalid`）；相机视频比条目短时照样判（注册表 8.6） |
| `review-2026-09-20.md` | 设计评审记录 |

**契约**在 `docs/contracts/`（一页导读 `SUMMARY.md`）。CLI、Daemon、前端之间只通过这些文件对话，谁都不 import 对方的内部模块：

| # | 契约 | 文件 |
|---|---|---|
| C1 | 模块注册表：模块、块与段、细码目录与覆盖、依赖、参数 Schema、报告明细 | `backend/curation/contracts/modules.py`，导出为 `modules.json` |
| C2 | CLI 的 `--json` 输出与命令之间交换的文件 | `cli/*.schema.json` |
| C3 | 进度协议（stderr 上的 JSON Lines） | `progress.schema.json` |
| C4 | REST API | `openapi.yaml` |
| C5 | 仓储接口与状态机 | `backend/daemon/repo/protocol.py` |
| C6 | 检测项分类表（细码与覆盖都指向它的编号；2.0 起编号冻结：已有编号不改定义，新项接在本维度最后，删掉的不复用，守卫是 `backend/tests/contracts/taxonomy_frozen.json`） | `taxonomy.json`（Schema `taxonomy.schema.json`） |
| C7 | 数据集声明（第一层是 mcap 字段映射；可视化与质检共用，设计 18 §6、设计 25 §3） | `dataset-declaration.schema.json`（`viz-mapping.schema.json` 留一版） |
| 其他 | EEF 输入格式；对账录制带格式 | `eef/`、`parity/` |

改契约：改文件，不兼容的改动升版本号（`registry_version` / `schema_version` / `info.version`）；在 `examples/` 补合法与不合法示例；
C4 的文案（`info.description`、tag 说明、示例标题）是 API 文档页给客户看的：中英两版（中文在 `x-description-zh` / `x-summary-zh`）、
不写内部编号，变更记录从 2.5.1 起只记客户可见的改动（`docs/contracts/README.md`「改契约的流程」）；
`cd backend && ../.venv/bin/python -m curation.contracts export-modules`（只在改了 C1 时）再 `… lock`；改了 C4 在 `frontend/` 跑
`npm run gen:api`。`CONTRACTS.lock` 没刷新、生成的类型没更新，CI 都会红。

文档和提交里的编号：W 是工作包（设计 11），F 是需求账本条目，D、P 是冻结决策（设计 00 §7），C1–C7 是契约。

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

**数据可视化**（设计 18、19；后端说明见 `backend/daemon/viz/README.md`，界面见设计 07 §4.5 与前端 README）
- 分层是「读取器 → 统一展示模型 → 视图」（D61）：每种格式一个读取器（`daemon/viz/` 的 `lerobot.py`、`mcap.py`、`lance.py`，格式解析在
  `curation/viz/`），都产出 C4 的 `VizDataset` / `VizEpisode`（相机、曲线组、标注、字段树），前端只认展示模型、不认格式。加一种格式就是
  加一个读取器、在 `service.py` 与 `status.py`（`READERS`）里登记，测试放 `tests/viz`（夹具造 LeRobot / mcap / Lance 数据集与最小本地 S3）。
- 只读、不改判决：mcap 的字段映射（C7 声明的第一层）确认后派生质检读取用的映射，开跑时冻结进 `run.json`（D62；整份声明冻结进
  `run.json["declaration"]`，EEF 据此生成轨迹）；迷你版走任务级接口，读任务冻结的输入。
- 视频能直连就直连（预签名地址、本地字节），浏览器放不了的由 Daemon 转封装或转码（D60，缓存只放本地盘）；mcap 的 H.264 / H.265
  缺省由浏览器 WebCodecs 解，失败退回转封装（D66）。开关与缓存都是 `CURATOR_VIZ_*`（Daemon README「配置」）。
- 前端：时钟只有一份（`features/visualizer/clock.ts`），格子都跟它走；开发构建是 StrictMode，解码器这类要释放的资源在同一个 effect 里建和放。
  本机看效果：`curator-frontend-dev` 是模拟世界（`src/mocks/vizWorld.ts`）；看真数据用 `curator-daemon-local`（它托管 `frontend/dist`，先 `npm run build`），
  数据集放在 `$TMPDIR/curator-local/inputs` 下、从「添加数据集」的本地挂载路径登记。

**质检模块与判定**
- 新模块放 `extensions/<模块>/`。要动哪些地方（C1 注册表的细码与覆盖、预检、`check` 的段、planner 与 Daemon 的块、报告、前端、测试与对账基线），照设计 05 §7 与设计 17 §7 的清单做；最近的样板是设计 26（重复检测）。设计 12、14 的开工指引写于逐档漏斗时期，只作参考。
- VLM 输入是多机位连续视频（设计 13）；出厂默认模型在 `pipeline/default.yaml` 的 `checks.task_success.vlm.model`（预设后端 `vlm_backends.ark.model` 写的是同一个，改时一起改），线上按任务选的后端和模型由 Daemon 的「VLM 后端」管理。

**部署**（`deploy/README.md`）：镜像由火山 CP 流水线从分支构建，版本号是完整提交号；Chart 是 rerun 仓库的 `deploy/helm/dataverse`（D53，自托管 vLLM 是同仓库里独立的 `deploy/helm/vllm`），升级质检台就是换它的 `image.curator` 再 `helm upgrade`（运行中的任务被系统暂停、新 Pod 上自动续跑）。Chart 给质检台的环境变量以设计 09 §2.1 的表为约定，改名或删设置要同时改那边的模板；安装、主密钥轮换、备份与排障都在 deploy README。

## 开发环境

- 任何可能影响判决的改动，先跑对账工具的黄金基线回放（`PYTHONPATH=tools .venv/bin/python -m pytest tools/parity/tests -m e2e`）。
- 代码注释、提交信息用英文；文档、界面文案用中文。

Python 依赖装在仓库根目录的 `.venv`（配方见对账工具 README 的「本地环境」一节；macOS 27 上 scipy 用 1.16.3）。镜像与 CI 用
Python 3.10，本机 `.venv` 是 3.12：别用 3.11 以后才有的语法和标准库。前端要 Node 20.19+ 或 22.13+，装依赖只用 `npm ci`；增删依赖用
`npx -y npm@11 install …`（npm 10.9.2 解析依赖树会崩）。本机起服务可以用 `.claude/launch.json` 里的配置：`curator-daemon-dev`
（`/curation` 前缀、不鉴权、临时数据目录）、`curator-daemon-local`（再加本地数据根与本地交付替身，都在 `$TMPDIR/curator-local` 下；
放一份 `make_mini_lerobot` 数据集、起一个 `FakeVlmServer(port=8766)` 登记成 custom 后端，就能真跑一个带模型模块的任务）、`curator-daemon-eef`（数据根是 dataset2，本地交付在 `$TMPDIR/curator-eef/tos`，EEF 模块与叠加用）、`curator-fakevlm`（上面那个假模型）、`curator-frontend-dev` 和 `curator-mockups`（静态稿，端口 4173）。

### 测试

| 套件 | 命令（仓库根目录起） | 说明 |
|---|---|---|
| 内核单测 | `cd backend && ../.venv/bin/python -m pytest -q curation/tests` | 要在 `backend/` 下跑（有一条起子进程的用例靠工作目录找包）；用真数据集的用例在本机缺数据时跳过 |
| 契约 | `cd backend && ../.venv/bin/python -m pytest -q tests/contracts && ../.venv/bin/python -m curation.contracts check` | 契约与锁不一致就失败 |
| v2 各工作包 | `cd backend && ../.venv/bin/python -m pytest -q tests/<目录>` | 最慢的是 `orchestr`（约 10 分钟；`-m "not slow"` 跳过真起 CLI 的用例）和 `cli`（约 6 分钟） |
| 对账工具 | `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/parity/tests` | 约一分半；`-m "not e2e"` 只跑单元部分 |
| 回归样本工具 | `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests` | 秒级；注册表改了细码或覆盖，要重新生成 `taxonomy.json` 的平台注记（`coverage_from_registry`） |
| 前端 | `cd frontend && npm run check:api && npm run lint && npm run typecheck && npm test && npm run build` | Vitest + jsdom，模拟数据的响应按契约校验 |

写端到端用例别靠时序：流水线下各段交叠执行，模型的快慢用假端点的 `delay_s`、`hold(needle)`、`fail` 控制，等条件满足再动作。
前端用例的 `findBy` / `waitFor` 缺省等 5 秒（`src/test/setup.ts`）：页面都是按需加载的，CI 比本机慢两到四倍，首次渲染只等 1 秒会偶发超时。

### CI 门禁

`.github/workflows/ci.yml`（工作流 `curator-ci`）：推送到 `feat/curator-v2` 和所有 PR 都会跑；同一分支上新的推送会取消还在跑的那一轮。
四个任务都绿才算过：

| 任务 | 内容 | 时长 |
|---|---|---|
| `tests` | Python 3.10 下串行跑 13 套：内核单测、契约与锁、CLI、planner、Daemon、密钥、结果读取、编排、镜像与部署约定、EEF、数据可视化、对账工具、回归样本工具；前面失败不影响后面的步骤 | 约 30 分钟 |
| `frontend` | Node 20 与 22 各一遍：`check:api`、`lint`、`typecheck`、`test`、`build` | 几分钟 |
| `a-class-guard` | 原样搬来的算法文件（A 类，清单见设计 10 §2）逐个比对冻结时的哈希；有意的改动在 `tools/parity/a_class_declared.json` 登记新哈希和理由，或在 PR 描述里写 `parity-change:` | 秒级 |
| `image` | 用公网源构建镜像，确认 Daemon 和 CLI 能在镜像里启动 | 几分钟 |

失败时看运行页面上的注解和 Job summary（`scripts/pytest-summary.sh` 把 pytest 输出的尾部贴在那里，步骤日志要登录才能看）；
命令行用 `gh run list --branch feat/curator-v2` 和 `gh run view <id> --log-failed`。

## 协作约定

- 需求账本与进度：根目录 `feature_list.md`、`claude-progress.txt`——只在本地保存，不入库（已在 `.gitignore`，2026-09-24 需求方要求），照常读写、别提交。
- 同一个工作区里常有几个会话同时开发：提交按路径（`git add <路径>`、`git commit -- <路径>`），别 `stash`、`reset`、`checkout` 别人没提交的改动。
- 推送会取消同分支上还在跑的 CI：别人等着看 CI 结果时，先在本地提交，等那一轮跑完再推。
- 仓库是公开的：密钥和账号不进仓库，也不进日志（Daemon 日志里的密钥字段一律打成 `***`）。
- 每个组件的 README 都有手动验证的一节（多数叫「手动验证步骤」），行为变了要同步改，保证照着能跑通。

## 历史与遗留

- 仓库原是 daft 的 fork，上游 daft 源码已在 2026-09-20 删除（F1.2）；开发分支是 `feat/curator-v2`。
- v1 的代码在 `release_v1` 分支，本分支已删掉（设计 27，D89）：`curation run`、`rejudge`、`review-page`、`backends`、`public` 都没有了；
  `ls`、`prune`、`fetch` 留作 v2 的运维命令（`cli/ops.py`，给人在终端里用，输出中文文字）。对账工具也只剩 v2 自录的基线
  （`run-v2 --fake-vlm` 录，`--replay` 回放）；A 类守卫读自己的 `a_class_manifest.json`（D91）。
- 「可视化」原先跳 ReRun（设计 15，D55）；内置播放器（设计 18、19，2026-10-04 合入）上线后，ReRun 入口暂留作「ReRun」（数据集列表与详情的「更多」里；2026-10-11 前叫「可视化（旧）」），下线列在第二期清单（设计 18 §10）。
