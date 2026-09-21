# 契约要点与冻结时的取舍（2026-09-21）

一页读懂 C1–C5：每份管什么、冻结时定下了哪些原文没写死的细节，以及需求方拍板的那一处（D35）。
文件索引和改契约的流程见同目录 `README.md`；以契约文件本身为准，本页是导读。

## 一、五份契约

| # | 管什么 | 要点 |
|---|---|---|
| C1 模块注册表 `backend/curation/contracts/modules.py` → `modules.json` | 有哪些模块、中文名、需要什么输入、在哪一档跑、依赖谁、有哪些参数 | 8 个模块 = 6 项漏斗检查 + 去重、技能画像两项数据集级模块；档序 numeric → frame → vlm → post_verdict；`depends_on` 决定上游结果变了谁要作废重算；只有两个参数：`video_action_sync.sync_plots`、`task_success.evidence_frames`，取值 `flagged / all / off` |
| C2 CLI 输出与中间文件 `cli/*.schema.json`（20 份） | 每条命令 `--json` 的输出，以及命令之间传递的文件 | 退出码 0 / 2 / 3 / 4 / 5 / 6 / 130，非零时打统一的错误信封；**退出码 0 不等于每条都成功**，Daemon 看逐状态计数定模块状态；每条结果的判定只有 `pass / fail / abstain / scored / error` 五种，`error` 必须带出错明细；漏斗判决 `keep / drop / held`；终判四份清单 `passed / reject / held` 互斥且完备，`review` 是正交的复核视图；`commit.json` 最后写，没有它的结果版本一律不认 |
| C3 进度协议 `progress.schema.json` | CLI 子进程往 stderr 写的 JSON Lines，Daemon 转成 SSE | 四种行：进度、日志、token 用量、降并发通知；进度行是累计值，**用量行是增量**，由 Daemon 按「子任务 × 模块 × 调用种类 × 模型 × 账本」累加；推给浏览器的 SSE 一律是累计值 |
| C4 REST API `openapi.yaml`（OpenAPI 3.1） | 前端、Agent、`curation task …` 客户端看到的全部接口 | 39 个路径、48 个操作，全部挂在 `{base}/api/v1` 下（生产环境是 `/curation`）；Basic 鉴权，探针免鉴权；一种错误体，`code` 17 个给程序判断、`message` 中文给人看；写接口支持 `Idempotency-Key`（24 小时内同 key 返回首次结果）；任务列表用页码 + 总数，日志、裁决队列、episode 列表用游标；报告、计划、预检等结构直接引用 C2，不另写一份 |
| C5 Repository 与状态机 `backend/daemon/repo/protocol.py` | Daemon 内部读写状态的唯一入口 | 10 个任务状态，允许的迁移逐条列出（契约测试逐条对照 01 篇 §3.1）；状态变更一律比较后交换（CAS），不许先读后写；事务由调用方显式开启；每个查询都带 owner（本期固定为 `default`，为以后接 IAM 留路）；结果版本切换也是 CAS |

防漂移：26 份契约文件的 sha256 记在 `CONTRACTS.lock`，改了契约而没刷新锁，CI 变红；
`examples/` 里 22 组从设计文档抄来的合法与不合法样例，契约测试双向校验。

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

## 五、下一版（1.1）要改的

批次 1 各包实现时发现的缺口，和静态稿评审新增的接口，攒在这里；W4、W7 合并后一次修订，统一升版本、刷新锁。
在那之前各包按括号里的临时做法实现。

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
