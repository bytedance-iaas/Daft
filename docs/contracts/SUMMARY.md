# 契约要点与冻结时的取舍（2026-09-22，1.5.2 修订后）

一页读懂 C1–C5：每份管什么、冻结时定下了哪些原文没写死的细节，以及需求方拍板的那一处（D35）。
文件索引和改契约的流程见同目录 `README.md`；以契约文件本身为准，本页是导读。

## 一、五份契约

| # | 管什么 | 要点 |
|---|---|---|
| C1 模块注册表 `backend/curation/contracts/modules.py` → `modules.json` | 有哪些模块、中文名、需要什么输入、在哪一档跑、依赖谁、有哪些参数 | 8 个模块 = 6 项漏斗检查 + 去重、技能画像两项数据集级模块；档序 numeric → frame → vlm → post_verdict；`depends_on` 决定上游结果变了谁要作废重算；只有两个参数：`video_action_sync.sync_plots`、`task_success.evidence_frames`，取值 `flagged / all / off`；参数带表单用的 `title` 与选项名，新建任务第二屏按它生成（D38）；带一份复核种类目录，每个模块写明产生哪几种复核、它的拒绝可否复议（D43） |
| C2 CLI 输出与中间文件 `cli/*.schema.json`（20 份） | 每条命令 `--json` 的输出，以及命令之间传递的文件 | 退出码 0 / 2 / 3 / 4 / 5 / 6 / 130，非零时打统一的错误信封；**退出码 0 不等于每条都成功**，Daemon 看逐状态计数定模块状态；每条结果的判定只有 `pass / fail / abstain / scored / error` 五种，`error` 必须带出错明细；漏斗判决 `keep / drop / held`；终判四份清单 `passed / reject / held` 互斥，除了缺源文件被剔除的条目（D40）之外完备，`review` 是正交的复核视图；`commit.json` 最后写，没有它的结果版本一律不认 |
| C3 进度协议 `progress.schema.json` | CLI 子进程往 stderr 写的 JSON Lines，Daemon 转成 SSE | 四种行：进度、日志、token 用量、降并发通知；进度行是累计值，**用量行是增量**，由 Daemon 按「子任务 × 模块 × 调用种类 × 模型 × 账本」累加；推给浏览器的 SSE 一律是累计值 |
| C4 REST API `openapi.yaml`（OpenAPI 3.1，1.5.2） | 前端、Agent、`curation task …` 客户端看到的全部接口 | 45 个路径、57 个操作，全部挂在 `{base}/api/v1` 下（生产环境是 `/curation`）；Basic 鉴权，探针免鉴权；一种错误体，`code` 19 个给程序判断、`message` 中文给人看；写接口支持 `Idempotency-Key`（24 小时内同 key 返回首次结果）；任务列表用页码 + 总数，日志、裁决队列、episode 列表用游标；报告、计划、预检等结构直接引用 C2，不另写一份 |
| C5 Repository 与状态机 `backend/daemon/repo/protocol.py` | Daemon 内部读写状态的唯一入口 | 10 个任务状态，允许的迁移逐条列出（契约测试逐条对照 01 篇 §3.1）；状态变更一律比较后交换（CAS），不许先读后写；事务由调用方显式开启；每个查询都带 owner（本期固定为 `default`，为以后接 IAM 留路）；结果版本切换也是 CAS |

防漂移：26 份契约文件的 sha256 记在 `CONTRACTS.lock`，改了契约而没刷新锁，CI 变红；
`examples/` 里 25 组合法与不合法样例，契约测试双向校验。

## 二、冻结时定下的细节（已按此落地，可否决）

| # | 定了什么 | 为什么 |
|---|---|---|
| 1 | C1 放在 `contracts/`，不放设计原文写的 `registry/` | `registry/` 是 A 类目录，受冻结清单守护，放进去每次改注册表都会触发 A 类告警 |
| 2 | 判定结果多一个 `scored` | 运动质量、视觉质量只打分不投票。不单列的话会被算成「弃权」，和模型「判不了」混在一起，弃权数和待人工复核的数量都会虚高 |
| 3 | 证据模式取值用 v1 的 `off`（原文写 `none`） | v1 配置原样对应，对账时不用翻译 |
| 4 | `aggregate`、`report` 加 `--revision N`；报告类接口加 `?rev=N` | 结果版本号由 Daemon 分配，CLI 只往指定版本里写；历史版本可以回看 |
| 5 | 新增 `GET /tasks/{id}/timeline` | 任务详情页静态稿里有执行时间线（系统暂停、子任务、结果版本），原端点表里没有数据来源 |
| 6 | `report.json` 里不存 URL | 签名链接会过期，对外地址随部署变；链接由 REST 层在读取时补上 |
| 7 | 被去重剔除的条目只出现在 `reject` | 由「三份清单互斥且完备」推出。v1 的 `passed.json` 里也有它们（交付的数据集里没有）；如果下游有脚本拿 `passed.json` 数交付条数，v2 的数字会变小，也就是变准了 |

## 三、已拍板（2026-09-21，D35）：已确定拒绝的条目又有模块出错

原先两处写法不一致：
- 设计原文（02 篇 §3.6、06 篇 §3）：任何已勾选的模块对某条 episode 出错，这条就进 `held`，等补跑。
- 冻结的漏斗判决契约：某个硬门已经确定判它失败，就直接拒绝（v1 也是这样）。

**需求方同意的方案**：只有判决取决于出错的模块时才进 `held`。如果拒绝理由来自正常判完的模块，就直接拒绝。
「正常判完」是指某个硬门确定失败，或者所有软分模块都给了分、加权分低于阈值。出错的模块照样记在这条上，报告里看得到。
- 与 D33 不冲突。D33 不信的是降级得来的结论，而这里的拒绝理由并不是降级得来的。
- 补跑翻不了案。出错的模块重跑出什么结果，这一条都已经够拒绝了。
- 解码失败、算法异常这类错误多半每次都复现。按设计原文，这种条目会一直卡在「待补跑」，报告里也看不到它真正的拒绝理由。
- 连带两处：重试只补跑 `held` 里的条目，不浪费模型调用；任务终态看 `held` 是否为空，01 篇 §3 的终态规则相应改一句。

已落地：决策表加 D35；00、01、02、03、04、06 篇相应改写；`cli/verdict-line.schema.json` 加了两条约束
（`held` 不能带确定失败的硬门；`drop` 必须有硬门失败或软分），`examples/` 补了对应的合法与不合法样例。
「所有软分模块都给了分」这一半要知道哪些模块是软分，schema 表达不了，由 W3 的 `aggregate` 单测守住。

## 四、顺带修掉的

- `openapi.yaml` 错误体 `message` 的描述里有个逗号没加引号，被 YAML 解析成了一个多余字段。
  它不影响校验，但契约不干净。已修，另加了一条测试专抓这类解析事故，锁已刷新（`51e048568`）。

## 五、1.1 修订（2026-09-21，已完成）

批次 1 各包报告的缺口，和静态稿评审新增的接口，一次修订完。C4 升到 1.1.0，C1 注册表升到 1.1；
C2、C3 只做了向后兼容的扩充（新增可选字段、放宽枚举），`schema_version` 仍是 1.0。下表每一项都已落地，
括号里是原来的临时做法。W4 的报告到了再补一轮。

| # | 契约 | 要改什么 | 来源 |
|---|---|---|---|
| 1 | C4 | 数据集登记：`GET/POST /datasets`、`GET/PATCH/DELETE /datasets/{id}`、`/recheck`、`/repreflight`；原来的「列数据集」挪到 `GET /datasets/browse`；`POST /tasks/{id}/repreflight`；`GET /overview`；`GET /tasks` 加 `module` 筛选，列表条目加所选模块 id；`POST /tasks` 的 `input` 可给 `dataset_id`；`start` 指纹对不上返回 409 `source_changed`，`details` 带变化 | D36、D37；设计 03 §12 |
| 2 | C5 | `Dataset`、`DatasetCheck` 实体与仓储方法；任务加 `dataset_id` | D36；设计 01 §2.8 |
| 3 | C2 `error` | 命令行客户端被 Daemon 拒绝、未预期的异常、交付目录读不到，现在分别借用 2/3/6、4、3（`input_unreachable`）；考虑补专门的 code | W3 |
| 4 | C2 | `task wait --timeout` 到时没有退出码（现为 0 + 警告，调用方看 `state`） | W3 |
| 5 | C2 `export-manifest` | 产物条目没有 `size`，只在远端的文件没法比大小；`verify` 核验哪些「关键文件」没写进契约 | W3，待 W7 确认 |
| 6 | C2 `preflight` / 设计 02 §3.1 | 补 `--embodiment-id`、`--modules` 两个参数；`cameras` 用去掉 `observation.images.` 前缀的短名；原因文案用英文还是中文 | W3 |
| 7 | C2 `source-manifest` | 写明 digest 算法：每个对象「键、大小、ETag 或修改时间」一行，排序后取 sha256 | W3 |
| 8 | 设计 02 §2 | 可选的会话令牌变量 `CURATION_INPUT_TOS_SESSION_TOKEN` / `CURATION_OUTPUT_TOS_SESSION_TOKEN` | W3 |
| 9 | C3 `usage` | `call_kind` 只有 v1 的五种加 `merged`，新模块单发时没有自己的标签（暂借五种之一） | W6 |
| 10 | C3 `usage` | 实际账里合并请求记在哪个 `module` 下没定义（暂用参与模块排序后以 `+` 连接）；`requests` 与 `requests_unknown_usage` 互斥、两者之和才是发出的总数，要写明 | W6 |
| 11 | C2 `check` | `merge.requests` 遇到跨模块请求怎么计没定义（暂在每个参与模块里各记一次，任务总数以实际账为准） | W6 |
| 12 | C2 `plan` | 闸门只有 v1 那八把、档位 id 是固定枚举，新模块接入时要能扩展 | W6 |
| 13 | 数据契约 | `vlm_latency.csv` 的五个标签没有 `merged`，接线时定合并请求的延迟行用什么标签 | W6 |

W6 另建议补进设计文档的：09 §2.1 的 values.yaml 加 `vlm.merge`；04 §3 的示例计划补 `limits`、`command`、`guard_caption`；
04 §2.2 注明 endstate、arbitration、guard_caption 三把闸门跟着 episode 闸门走；01 §2.6 写明第 10 条的约定；
05 §7 写明 `merge_units` 的约定（`DeclaredMergeUnits`）。

W7 报告的缺口同样在这一轮落地：`export-manifest` 加可选的每条 `task` 和顶层 `files`（大小与 sha256），
`export` 结果加 `videos_renamed`、`full_reason`，并写明 `videos_copied`、`videos_reencoded` 的口径和 v3 `chunk` 的含义；
设计 02 §3.7 补 `--revision`，06 §4.3 写明旁挂文件、任务表编号与退回全量的条件。

修订带出来的代码改动（交给批次 2）：

| 谁 | 改什么 |
|---|---|
| W3 后半 | `curation task …` 改用新错误码：Daemon 回错误 → `rejected`（7），连不上 → `daemon_unreachable`（3），`wait` 超时 → `wait_timeout`（8）；`verify` 读不到交付目录用 `output_unreachable`；`export` 命令补 `--revision` |
| W3 后半（接线 W6） | planner 里 `DeclaredMergeUnits` 只认 v1 五种调用种类的限制放开（C3 1.1 允许模块自己的种类） |
| W7 续 | 导出时把每条的 `task` 和顶层 `files` 写进 `manifest.json`，`verify` 就能按它逐文件比大小和 sha256 |
| W4 续 | 数据集登记、概览、开始时的指纹核对与 `repreflight`；任务列表的 `module` / `dataset_id` 筛选与 `modules` 字段 |

预检的 `reason_code` 已由我直接补进 W3 的 `preflight`（七种原因码都有测试）。

## 六、1.2 修订（2026-09-21，W4 合并后）

W4 报告的缺口，外加任务编排（W5）开工前必须补的状态机缺口。C4 升到 1.2.0，C5 补方法和字段：

| 契约 | 改了什么 | 为什么 |
|---|---|---|
| C5 状态机 | 新增 `stopped / failed → succeeded / completed_with_errors`，只对任务、不对子任务 | 「继续运行」的 resume 子任务成功结束后，要按当前结果重算父任务的终态（01 §2.5）；原表里 stopped、failed 没有出边，W5 没法落地。子任务自己的 stopped / failed 仍是终局 |
| C5 | `Subtask.pause_reason`，`update_subtask_state(pause_reason=…)`，`set_subtask_result_rev`，`subtasks_in_states` | 子任务的系统暂停与自动恢复、时间线上链到子任务产出的结果版本、启动对账时扫描子任务 |
| C5 | 写明几条一致性测试钉住的行为：`list_events` 新的在前；`rebind` 传 `None` 表示不改；有活动子任务时不能软删除；建子任务时检查父任务状态 | 以后换 RDS 实现时不至于各做各的 |
| C4 | 写请求一律带 `Content-Type: application/json`，没有请求体也要带；所有写接口都接受 `Idempotency-Key` | W4 已经按这个实现；命令行客户端要跟上（交给 W3） |
| C4 | 新错误码 `method_not_allowed`（405） | 原来只能拿 `not_found` 顶替 |
| C4 | `Task.vlm` 改用独立的 `TaskVlm`，带 `snapshot` | 原来的 allOf 写法永远通不过校验 |
| C4 | 访问密钥被删后，响应里的 `input.credential` / `output.credential` 为 `null` | 原来是必填字符串，只能输出空串 |
| C4 | SSE 的 `state`、`done` 加 `subtask_id`、`reason` | W4 已经在发 |
| C4 | 日志接口：新的在前；`subtask` 不传 = 全部，传空串 = 只看主流程，传 id = 只看这个子任务 | 语义原来没写 |
| C4 | `UsageRow.call_kind`、`Perf.latency[].call_kind`、`StageProgress.id` 放开为同一格式的 id | 和 C2、C3 1.1 保持一致，否则新模块接入后这三处会通不过校验 |

设计文档跟进：01 §2.8（`dataset_check.change`、区域为空时的唯一索引）、§3.1（新迁移和说明），03 §1、§8（写请求规则）。

## 七、1.3 修订（2026-09-21，已完成）

W8 合并时报告的缺口，除第 8、10 条外都已写进契约（第 8 条留到下一版，第 10 条是 W5 的实现要求）：

| # | 契约 | 要改什么 | 现状 |
|---|---|---|---|
| 1 | C4 `precheck_failed` | 给 `details` 定格式 | `details.checks: [{id: input/output/vlm, ok, code, reason, target, elapsed_ms}]` |
| 2 | C4 `ProbeResult.error.code` | 定枚举 | forbidden / not_found / auth_failed / unreachable / server_error / failed；写成但探针没删掉是 `leftover`，此时 `ok: true` |
| 3 | C4 `/media/sign` | 写明可签的前缀 | 任务批次目录 `<交付目录>/<run_id>/`；片段起止时间由 episode 接口给，不由签名接口算 |
| 4 | C4 删除访问密钥 / 模型服务 | 只被已结束任务引用时的「需确认」 | 409 `credential_in_use` / `backend_in_use` + `details.confirm_required: true`，带 `?confirm=true` 再删 |
| 5 | C5 | 模型服务被多少任务引用的计数查询；后端的 `models_listed`、`has_api_key` 两列 | 删后端时扫描任务表；两列暂存在后端 API Key 那一行的 `payload_meta` 里 |
| 6 | C5 / 设计 01 §2.1 | 写明后端的 API Key 也是 credential 表的一行（`kind` 为 ark / custom_vlm，名字 `vlm-backend/<后端 id>`），用户建的访问密钥不能用这个前缀 | 已这样实现；概览的 `credentials_failed` 只数 `kind='tos'` |
| 7 | C4 `CredentialCreate` | 加可选的 `session_token` | CLI 环境已支持会话令牌，接口还设不进来 |
| 8 | 设计 02 §2 | 访问密钥上自定义的 endpoint 怎么传给 CLI（分角色的 endpoint 变量） | 只对 Daemon 自己的调用生效 |
| 9 | 设计 08 §4 | `unverified` 的用法：列桶被拒、又没填测试用存储桶时记 `unverified` 并附提示（签名已被接受，身份没问题），不记 `failed` | 已这样实现 |
| 10 | 编排（W5） | 任务级思考强度要校验在模型的有效档位内 | `taskspec.resolve_vlm` 不查；W5 建任务时调 `svc.check_task_effort(...)` |

数据集登记引用的访问密钥被删时不阻止删除，登记上的 `credential_id` 置空（与已结束任务相同），交 W4 续作实现。

1.3 同时落地的还有 W4 续作报告的缺口：`Decision`、`DatasetDetail` 改成「共享字段 + `unevaluatedProperties: false`」，
终于能通过校验；19 个写接口补声明 `Idempotency-Key`；`Link.rel` 加 `dataset`；`Subtask.pause_reason`；数据集 id 以 `ds_` 开头；
概览各数字的口径写进 `Overview` 的说明；C5 并入原来放在 `repo/extras.py` 的六个查询（按地址找登记、待裁决积压、
交付待导出、近期完成、未结束的子任务、Token 时间线），新增 `vlm_backend_references`、`credential_dataset_references`，
并写明一致性测试钉住的几条行为（resume 结束时的 `finished_at`、重新预检的四项一起刷新、过时的核对不改状态、
登记只能用同一 owner 的 TOS 访问密钥、模型服务的 API Key 行命名为 `vlm-backend/<id>`）。

## 八、1.4 修订（2026-09-21，已完成）

需求方对 W3 三个问题的拍板（D39–D41），以及 W3 核对 v1 后定下的 review 口径：

| # | 契约 | 改了什么 | 依据 |
|---|---|---|---|
| 1 | C4 执行裁决，C2 `decisions.json`、`adjudicate-apply` | 可选请求体 `{relabel_rerun: v1 \| full}`，缺省 `v1`：改了标、又没有人工成败结论的条目，照 v1 的 `rejudge` 只跑多视角打分和逐机位复核两层，结论和调用与 v1 一致；`full` 走首轮的完整判定（多了任务类型、机位提示、判废护栏、取证仲裁），同样的裁决可能判得和 v1 不同。选择记在子任务的 `scope.relabel_rerun` 和每条改标上，之后重试这几条沿用同一口径；裁决页的执行对话框要说明两者的差别 | D39 |
| 2 | C2 `source-manifest`、`check`、`report`、`final-list`，C4 `Summary` | 源文件缺失（parquet 或某路视频不在）的 episode 照 v1 剔除：不质检、不进四份清单、不计入 total。`snapshot` 从列表里就能认出来，记进清单的 `skipped_episodes`（缺哪些键），带清单的命令都不读它们；没带清单时读到才发现的，`check` 不写结果行、列在输出的 `skipped_missing_source` 里。报告 `integrity.skipped_episodes` 列出全部，`overview.counts.skipped` 与任务汇总的 `skipped` 给出条数。四份清单共用 `common.schema.json` 里的 `skipped_episodes` 定义 | D40 |
| 3 | 设计 04、06（契约不变） | 技能画像整个模块失败时维持 P10 + P11：全部待补跑，一条都不交付，等「重试」成功；和 v1 不同（v1 从不因画像挡交付），记进 10 篇 §3.0 | D41 |
| 4 | C2 `final-list`（原待修订第 3 条） | review 的种类：`task_verdict` 只收任务成败判定的弃权，别的模块判不了的留在判决行和报告里；`label_conflict` 来自技能画像；`reject_appeal` 收所有只归因于任务成败判定（没有别的硬门失败、不是软分拒绝）、没有生效复议、没被弃用的拒绝，复议选了「拿不准」的仍留在队列 | W3 核对 v1 |
| 5 | 设计 02（原待修订第 1 条） | `check`、`autolabel` 的 `--vlm-reasoning-effort <档位>`：给了才在每个请求里带 `reasoning_effort`，不给什么都不发，对账口径不变；档位是否有效由 Daemon 建任务时按模型校验 | W3 |

## 九、1.5 修订（2026-09-21，已完成）

需求方对 1.4 里三条默认做法的答复：重复项与复议改了一处（D42），复核种类要留好扩展的接口（D43），思考参数只在设置了才发送不变。

| # | 契约 | 改了什么 | 依据 |
|---|---|---|---|
| 1 | C1 注册表 1.2 | 顶层加复核种类目录 `review_lines`：每种写明编号、标题、`review_kind`（review.json 里的叫法）、条目所在的清单（`passed` / `reject`）、未判时算不算待裁、可选的判断与按钮名；现有三种照 v1。每个模块加 `review_lines`（产生哪几种）与 `appealable`（归因于它的拒绝可否复议）；`dedup` 的 `produces_adjudication` 改为 true | D43、D42 |
| 2 | C4 1.5.0 | `ModuleRegistry` 带上目录与两个新字段；裁决的 `line`、`decision` 从枚举改为开放字符串（`^[a-z][a-z0-9_]*$`），由 Daemon 按目录和卡片上的问题校验，不合法 400；问题可带 `duplicate_of`；`tab=appeals` 列出归因于可复议模块的拒绝；`AdjudicationCounts` 写明三个数都按卡片（episode）计、复议不算待裁 | D42、D43、W10 |
| 3 | C2 `final-list`、`decisions.json`、`report.json` | review 条目的 `kind` 开放，另带 `line` 与 `duplicate_of`；已被拒绝的条目不再有成败裁决项，`held` 里的在补跑出结论前没有任何复核项；复议项覆盖只归因于任务成败判定的拒绝和去重的拒绝；恢复只推翻被复议的那个模块，另有模块出错的恢复后进 `held`。`decisions.json` 的 `line`、`decision` 开放，已知三种的取值仍逐一钉住；报告模块小节的 `adjudication` 加可选的 `appealable`（可复议条数，不算待裁） | D42、D43 |

加一种复核以后只需要：目录加一项、产生它的模块在 `review_lines` 里写上、`adjudicate-apply` 加这种判断的执行规则；
裁决页对没有专用视图的种类按目录通用渲染，C2、C4 都不用改。

**1.5.1（2026-09-22）**：1.5 的严格校验挡掉了 v1 的一项功能 —— 只有标注分歧的卡片上，选了「采纳新标注」之后可以
顺手给出成败结论（选填；给了机器直接采信、不再重判，留空则按新标注重判）。现在由目录表达：注册表 1.3 的复核种类可带
`follow_ups`（选了哪些判断之后，卡片多出哪条线的哪些判断、是否选填），标注分歧在「采纳新标注 / 自行改写标注」之后多出
选填的成败结论；打开它的判断一变，这个回答就作废（不执行、不计数）。同时写明：`status` 筛选在两个页签上都按卡片状态
（复议卡没答也是 pending，只是不计入待裁数）；「采纳新标注」可以不带 `new_label`，取问题里的建议；单条 episode 视图的
`reasons` / `review` 条目形状（`EpisodeNote`，同 C2 的拒绝原因）；读结果的接口在本地没有文件、又取不回来时回
404 `not_found`，`details.reason` 为 `run_dir_missing` 或 `revision_missing`。

**1.5.2**：卡片上由后续问题带出的问题标 `follow_up_of`（由哪条线的回答带出），打开它的回答一变，这个问题就不再出现在卡片上（W10 报告的缺口）。

同日补的说明（C2，不改结构）：`decisions.json` 里「采纳新标注」一律带 `new_label`（人没填时由 Daemon 取问题里的建议补上）；成败结论的作废规则 CLI 与 Daemon 同一条；因成败结论作废而重新列入重判的改标，沿用它当初应用时记下的口径（W3 报告的缺口）。

## 十、下一版（1.6）待修订

| # | 契约 | 要改什么 | 来源 |
|---|---|---|---|
| 1 | 设计 02 §2 | 分角色的 TOS endpoint 变量（现在只认 `TOS_ENDPOINT`） | W3、W8 |
| 2 | C2 / C4 `Perf` | `vlm_latency.csv` 没有 subtask_id；CLI 算不出 `duration_s` | W3 |
| 3 | C2 | 辅助命令（backends probe、creds verify、datasets …、clips）没有输出 schema | W3 |
| 4 | C4 `GET /tasks` | 按「有待裁决」「交付过期」筛选 | W10 |
| 5 | C4 `Task` | 记下所用的预设（完整 / 快速 / 自选），列表的预设名不必反推 | W10 |
| 6 | C4 | 站点配置接口（公共缓存桶是否可用、本地路径开关、所在地域） | W10 |
| 7 | C4 `SignedUrl` | `expires_at` 写明单位（毫秒）；`from_ts` / `to_ts` 挪到 episode 接口 | W10、W8 |
| 8 | C4 报告 | 各模块 `summary` 与专用视图（卡顿时间线、同步曲线、判决卡、两级技能表）的数据形状；平均质量分、复议的「关键读数」 | W10 |
| 9 | C4 | 查某模块出错的是哪几条；裁决卡片带视频引用；裁决列表的 `source` 多选与「拿不准」状态；`DecisionInput` 的「撤回」 | W10 |
| 10 | C5 | 读一个任务裁决的完整历史（现在只能读每条线、每条 episode 的最新一行，CSV 副本因此只含这些行） | W5b |
| 11 | C2 / C4 `Perf` | 外层重试的次数没有落盘，性能剖析给不出 `retries`；同步曲线（`curves/*.json`）没有对应的形状 | W5b |
| 12 | C1 目录 | 选填的复核种类（`counts_as_pending: false`）在界面上叫什么，目录里没有字段；现在复用「可复议」 | W10 |

## 十一、EEF–视频一致性（2026-09-23，F5.1 / F5.4）

- **新契约 `eef/`**（F5.1）：`eef-video/1.0.0` 的 sample / frame / calibration / observation 四份 Schema 与单文件容器 `trajectory-bundle/1.0`，规范正文 `eef/format.md`，迁移规则 `eef/migration.md`；示例 `examples/eef-*.json`。
- **C1 1.4**：每个模块多两个字段 `input_scope`（`funnel` / `all_selected`）与 `affects_dataset_verdict`；需求多一个 `eef_input`；`depends_on` 可以是模块 id。新增两个建议性模块 `eef_video_consistency`（frame 档）与 `eef_video_review`（vlm 档，第一刀不提供）：`gate=none`、`all_selected`、不影响判决；三张明细表 `eef_camera_metrics` / `eef_segments` / `eef_diagnosis`。
- **C4 1.7.0**：`ModuleRegistry` 跟上 1.4（两个必填字段、needs 与 depends_on 的枚举）；前端类型已重新生成。
- **C2（兼容扩充，schema_version 仍 1.0）**：预检的模块条目可带 `subitems`（逐分项能力）与 `episode_counts`；新原因码 `trajectory_missing`、`trajectory_invalid`、`eef_review_not_available`。`preflight` 与 `check` 多一个可重复的 `--param MODULE.KEY=VALUE`（值按模块 param_schema 转类型并校验）。
- 口径：第一刀里界面拿不到文件，所以不给文件时预检报 `unsupported: trajectory_missing`；F5.5 上传落地后已改为 `needs_input`（见下）。
- 建议性模块的记录 `passed = score = null`（所以 `verdict` 按 C2 推导为 `abstain`），分项进 `details`；报告里它的小节不按通过 / 判废 / 弃权计数，而是候选、分项可疑数、被支持的诊断与覆盖率。
- **F5.5（2026-09-23）**：C1 1.5 文件型参数（`format: upload`、`x-upload-kind`、`x-accept`、`x-max-mb`；Daemon 里值是 `upload:<id>` 句柄，CLI 里是路径）；C4 1.8.0 `POST /uploads`（请求体即文件，上传即校验，400 `validation_failed` 的 `details.errors[]` 定位到字段 / 样本 / 帧 / 相机 / 点位）与 `GET /uploads/{id}`，新 schema `Upload`、`UploadKind`、`UploadIssue`；C2 预检的 `input_hint.field` 枚举加 `trajectory_json`，EEF 模块没给文件时是 `needs_input: trajectory_missing`。
- **F5.6（2026-09-23）**：C1 1.6，`eef_video_review` 加明细表 `eef_review_windows`；新 Schema `eef/review_output.schema.json`（模型对一个复核窗口的答复：只有分类、布尔与帧号，`additionalProperties: false`；帧号属于本次请求、解释里没有测量值由执行方另查）；C2 预检加原因码 `eef_base_unavailable {base_reason_code}`，复核条目跟随被复核模块（缺文件同样 `needs_input: trajectory_missing`），再要 VLM 后端（`vlm_backend_missing`）；调用种类 `eef_review`（C3 1.1 的模块 id 形式，C3 本身不变）。

## 十二、ID 风格、概览时间段与「运行中」筛选（2026-09-23，F6.4 / F6.1，D45 / D46）

- **C4 1.10.0 · ID（D45）**：新记录的 ID 是「前缀-9 位小写字母」，旧记录保留 `前缀_…`，两种都照常可用。
  数据集 ID 的 pattern 改为 `^(ds-[a-z]{9}|ds_[0-9A-Za-z]+)$`（原来只写了前缀 `^ds_`，仓储和路由一直只认 `ds_` 加字母数字），
  `UploadId` 为 `^(upl-[a-z]{9}|upl_[0-9a-z]{10,40})$`，上传句柄为 `^upload:(upl-[a-z]{9}|upl_[0-9a-z]{10,40})$`；其余 ID 本来就没有 pattern。
- **C4 1.10.0 · 概览**：`GET /overview?days=7|30|90|365`（缺省 7，其它值 400）；`recent` 加 `bucket`（day / week / month）与 `since`，
  `tokens_per_bucket`（`start`、`label`、`tokens`）取代 `tokens_per_day`。90 是本周和之前 12 周（周一开头），365 是本月和之前 11 个月。
  唯一的调用方是前端概览页，已一起改。
- **C4 1.10.0 · 任务列表**：`state=running` 同时列出子任务正在排队或运行的已结束任务（D46，只是显示，行里的 `state` 不变）。
- **C5**：新异常 `IdTaken`（调用方自选的 ID 已被占用，重新生成后再试；与 `Conflict('name_taken')` 分开，不会出现在接口上）；
  模块说明写明 ID 随机、两种格式都认、同一毫秒建的行按插入先后排；`list_tasks` 加关键字 `running_subtasks`（`GET /tasks` 用它实现上一条）；
  `usage_buckets` 写明主流程在前、子任务按建的先后；Token 时间线至少保留 400 天（概览的「近 1 年」按月统计要用）。

## 十三、报告改版与 Episode 明细（2026-09-23，F6.2）

- **C4 1.11.0**（分支上写作 1.9.0，合并在 1.10.1 之后）：`GET /tasks/{id}/episodes`（`TaskEpisodePage`：结果版本里的 episode 按下标排，带所在清单、`reason_modules`、`review` / `review_modules`；按 `list`、`review`、`q`（编号子串，去掉 `ep` 前缀与前导零）筛选；游标带结果版本、绑定筛选条件；`total` 是筛选后的条数，`counts` 是整版的）与 `GET /tasks/{id}/episodes/{index}/sync-curves`（`SyncCurves`：逐相机的画面运动 / 机械臂运动曲线、互相关曲线与读数点，每条序列最多 600 点；没勾选、没走到或没保存曲线时 404，`details.reason` 分别是 `module_not_run`、`no_record`、`no_curves`）。示例 `examples/rest-episode-list.json`、`examples/rest-sync-curves.json`。上表第 11 条里「同步曲线没有对应的形状」由此解决。
- **C2 不变**：`report.json` 的模块 `summary` 仍是自由对象；1.9.0 起 CLI 在里面写入画图用的汇总键（计数、均值、按相机的小数组，不含 episode 清单），键名见设计 06 篇 §6.2。

## 十四、mcap 与 Lance 数据集（2026-09-23，F6.5，D44）

全部是兼容扩充：C2 的 `schema_version` 仍是 1.0，C4 升到 1.12.0（分支上写作 1.11.0，合并在报告改版的 1.11.0 之后），C5 只改注释；C1、C3 不变。

- **C2 预检**（`cli/preflight.schema.json`）：`format.kind` 多一个 `lance`（lerobot-lance-convert 0.3.0 起的三表布局；别的 Lance 表仍是 `lancedb`，不支持），
  `mcap` 与 `lance` 可以 `supported: true`；`format.version` 对 Lance 是元数据的 LeRobot 版本 `v3`，对 mcap 是 null；`dataset.fps` 对 mcap 是 null（时间轴取动作 topic 的 `log_time`）。
  新原因码：`format_disabled {format}`（站点用 `ingest.mcap_enabled` / `ingest.lance_enabled` 关掉了这种格式）、
  `format_unsupported_by_module {format}`（模块读不了这种格式：EEF–视频一致性与它的复核只读 LeRobot 的视频）。
- **C2 导出**（`cli/export.schema.json`、`cli/export-manifest.schema.json`）：`format` / `source_format` 多 `mcap`、`lance`；
  新字段 `dataset_dir`（交付数据集所在目录：缺省 `lerobot_curated`，mcap 是 `mcap_curated`，Lance 是 `lance_episodes`）和 `note`（Lance：原格式交付未做）；
  产物清单每条的 `artifacts` 三选一：LeRobot 的 `parquet` + `videos`、mcap 的 `file`、Lance 的 `videos` + `windows`（每路相机在视频文件里的起止秒）。
- **C2 报告**：`integrity` 本来就允许附加字段，mcap / Lance 源多一个 `container`（交付形态与 v1 的数据包体检），不改 schema。
- **C2 命令参数**：`autolabel`、`check`、`aggregate` 多一个 `--selection`（整个任务的所选，mcap / Lance 的语义样本取它的前 100 条）；环境变量 `CURATION_SOURCE_CACHE`（TOS 上的数据先拉到这里再读）。
- **C4 1.12.0**：`DatasetFormat` 与 `GET /datasets` 的 `format` 筛选多 `mcap`、`lance`；`BrowsedDataset.format_hint` 多 `mcap`、`lance`；
  `EpisodePreview` 多一个可选的 `task_unread`（mcap 的任务文本在 `/task` topic 里、预览读不到）；这两种格式的 `cameras` 为空。前端类型已重新生成。
- **C5**：`list_datasets` 的 `fmt` 说明加上 `mcap | lance`；SQLite 的第 4 步迁移重建 `dataset` 表，放宽 `format` 的 CHECK（01 篇 §2.8）。
- **D49（2026-09-23，F5.9 / F5.10）**：C1 1.8——`eef_video_review` 并入 `eef_video_consistency`：vlm 档、`gate=hard`、`affects_dataset_verdict=true`、`input_scope=funnel`、needs 加 `vlm`，参数合并（复核窗口数、每窗口帧数；阈值只留 `demo`），明细表加 `eef_review_windows`；新增 `native_ids()`（v2 自己实现、不进 v1 检查配置的模块）。记录：`passed` 为判过 / 判废 / 转人工（`null`），`details.decision` 与 `details.reason` 写结论与理由，`details.review` 是复核窗口。答复 Schema `eef/review_output.schema.json` 升到 eef-review/1.1（去掉背景运动）。C4 形状不变（模块表的值变了）。
- **F5.11（2026-09-23，D49 / 设计 12 D-E13）**：C1 1.9——复核目录加 `eef_check`（review 项 `kind: eef_consistency`，`applies_to: passed`、计入待裁，决定 `consistent` / `inconsistent` / `unsure`）；`eef_video_consistency` 的 `review_lines = ["eef_check"]`、`appealable = true`、`produces_adjudication = true`。C2 `decisions` 固定这条线的三种决定，`final-list` 的说明加上它（schema 仍是 1.0）。C4 1.14.0 只改 `ReviewLineId` 与决定的说明（线与决定本来就是开放字符串）。C5：`AdjudicationLine` 改成开放字符串，SQLite 第 5 步迁移重建 `adjudication` 表、去掉 `line` 的固定取值（行与 id 不变），以后新加裁决线不再改表。
- **F6.7（2026-09-24）**：C4 1.15.0——`GET /tasks` 加 `has_result`（只列有结果版本的任务）与 `pending_adjudication`（只列有待裁条目的任务），给控制台的跨任务「人工裁决」列表用；不传或为 false 时不筛。C5 `list_tasks` 同名两个参数，待裁计数与 `adjudication_backlog` 同一段 SQL。顺带补上 1.14.0 缺的变更记录。
- **F6.9（2026-09-24）**：C1 1.10——参数可以带 `x-choice-group`（`{id, title, required}`）：同组的参数互为替代，表单把它们合成一项（先选用哪个、再给值），只提交选中的那个，`required` 表示表单里必须给其一；命令行照旧可给任意几个或都不给。EEF 模块的观测种子与夹爪外观模板组成「夹爪参考」（`gripper_reference`，required），两项说明里的「二选一」字样随之去掉。
- **2026-09-24（galbot 反馈）**：C2 预检的 `input_hint.field` 枚举加 `observation_seeds`：EEF 模块既没有观测种子也没有夹爪外观模板时，模块级为 `needs_input`（`reason_code: observation_seed_missing`），Daemon 建任务时拒收并指到 `modules.eef_video_consistency.params.observation_seeds`。此前这种情况报 `available`，跑完每条都以「位置无法评估」转人工。
- **F5.13（2026-09-24）EEF 模块读 mcap**：`eef/sample.schema.json` 的 `media` 加可选字段 `topic`（mcap 图像 topic；有它时 `uri` 须是 `.mcap`、`kind=video`、`clip_start_s=0`、`clip_end_s=null`，帧号按 topic 的 log_time 顺序从 0 数），`eef-video/1.0.0` 不变（纯增补，现有文件照样有效）。C2 预检：mcap 数据集上 EEF 模块不再是 `format_unsupported_by_module`（只剩 Lance）。

- **F7.1–F7.4（2026-09-24，设计 14，D50–D52）数据完整性**：C1 1.11——新档 `integrity`，排在 `numeric` 之前；新模块 `data_integrity`（`gate=hard`、`needs=[raw_bytes]`、不可复议、参数只有 `decode_test`，默认 false）；复核目录加 `integrity_check`（review 项 `kind: integrity_suspect`，`applies_to: passed`、计入待裁，决定 `intact` / `broken` / `unsure`）；`ModuleSpec.native` 取代按 `eef_input` 认 v2 自有硬门（不进 JSON）。记录：`passed` 为通过 / 判废 / 可疑（`null`），`details` 有 `outcome`、`reason`、`tiers`、`findings`（`level`、`code`、`tier`、`message`、`file`、`camera`、`span_s`）与 `files`；数据集级发现写 `checks/data_integrity/dataset.json`。C2 `decisions` 钉住这条线的三种决定，`final-list` 的说明加上它；预检多三条 `warnings`（字符串，schema 不变）。C4 1.16.0——档名的四处枚举加 `integrity`（注册表的 `stages`、模块的 `stage`、流水线的 `last_stage` 与 `stage_processing_s`），`next_stage` 加 `numeric`；`ReviewLineId`、`DecisionInput` 的说明加上新线。
