# 26 · 重复检测取代精确去重；进度按三组显示

> 状态：**实施稿 v1.0（2026-10-11）**。D87、D88 登记在 00 §7；账本阶段 16（F16.0–F16.3）。
> 来源：需求方 2026-10-11——「我们实际要做的是质检是否有重复的 episode，而不是『去重』」；「本质上来说，就是一个 CPU 档和 VLM 档，分这么细实在没必要……
> 保留数据完整性（单独放一条），然后下面就是 CPU 档和 VLM 档」；「我们现在是开发阶段，不用考虑旧任务。如果可以删就删掉」；「产物交付功能也去掉了，所以
> 进度那里『& 产物交付』也删掉吧」。需求方选定：重复只报告；单独的「重复检测」模块，完整性的文件比对并进来；分类表 SET-1 不改名；「模型准备」算进 CPU 检查；
> 只改显示，后台的段不动。
> 取代：D42 里精确去重「留第一条、其余判废」的口径，D70 里的「去重」段（改名、改语义）；设计 14 的 `duplicate_content`。不动：A 类 `dataset_level/dedup.py`
> （新模块照用它的 `action_hash`、`episode_fingerprint`，文件不改）；v1 的 `curation run` 与 `ui/`（v1 自己的去重留在 v1 里，对账工具读它）。

## 0. 开工指引

1. **不留兼容**（需求方）：注册表直接删 `dedup` 模块与 `dedup` 段、删完整性的 `duplicate_content` 细码；不加退役标记，不为旧任务保留读法。
   本机与集群上已有的、勾过精确去重的任务，续跑、重试、执行裁决会失败——开发阶段可接受，部署说明里写一句。
2. **顺序**：注册表与契约（C1 6.0、C2、C4 6.0.0）→ 执行（`check`、planner、Daemon）→ 汇总与报告 → 前端 → 对账与回归样本工具 → 文档。
3. **回归门**：不勾重复检测的任务判决逐位不变；勾了的任务里，原先被去重判废的条目现在留在通过里、带一条 SET-1 发现（对账工具把这一点当作已知差异）。

## 1. 重复检测（`duplicate_check`，D87）

**查什么**：同一个数据集里，动作数据与各路画面完全相同的条目。指纹沿用原精确去重（A 类 `episode_fingerprint`）：动作数组的字节与形状，加每路相机的
内容身份（本地文件整读 sha256，TOS 上取 etag + 大小）与时间窗（LeRobot v3 / Lance 多条共用一个视频文件，靠时间窗区分）。两遍走法不变：每条到达时只
哈希动作，动作哈希撞了才读这两条的画面算指纹，所以没有重复的数据集不额外读视频。

**结论**：只报告。一组重复的**每一条**都得到一条发现 `duplicate`（SET-1「同一数据集内的重复条目」，缺省级别 info），`readings.same_as` 列出同组的其他条、
`readings.group_id` 是组里最小的编号；文案「与 ep X、ep Y 完全相同（动作与各路画面字节一致）」。不判废、不转人工、不能复议，策略「默认」与「只报不拒」一样。
模块出错（读不了动作、撞了哈希却读不了画面）照常让这一条「待补跑」，和其他模块一致。

**流式与成组**：段内维护「动作哈希 → 指纹 → 成员」，一条加入已有的组时，它自己的记录写上同组的其他条；组里先到的成员的记录在这次调用结束（或暂停）时
补写一条新的，把后来的成员加进 `same_as`。续跑从已有记录与 `checks/duplicate_check/groups.json`（顺序、动作哈希撞车、算过的指纹、各组成员）重建状态。
跨条目的状态在进程里，所以这一段的并发固定为 1（和原去重一样）。

**和数据完整性的关系**：完整性原来用存储校验和比对整个视频 / mcap 文件（`duplicate_content`，转人工）。它查得出直接复制的文件，查不出 parquet 不同而
动作与画面相同的复制，也跳过 LeRobot v3 的共用文件。重复检测的指纹覆盖它查得出的全部情况，所以这一项从完整性里删掉，SET-1 只由重复检测给出。

**放在哪**：CPU 块的 `duplicate` 段（原 `dedup` 段改名），排在 `frame` 之后；「完整质检」「快速质检」两个预设都勾它（不调用模型）。

**报告**：小节写「评估 N 条 · 重复 K 组、涉及 M 条」，一张「重复组大小」图（只数确认的重复组，不再数动作哈希撞车），明细表 `duplicate_groups`
（episode、同组的其他条）。Episode 明细里这个模块一块写「无重复」或「与 ep X、ep Y 完全相同」，编号可点。

## 2. 进度按三组显示（D88）

后台的段（`integrity`、`numeric`、`frame`、`duplicate`、`vlm_prep`、`vlm`）与它们的并发、续跑都不变——段让轻的数值检查不必排在要解码的画面检查后面，
续跑也按段记位置；合并它们要动注册表、planner、Daemon 调度与对账基线，用户看不出区别。**只改显示**：

| 显示的组 | 包含的段 | 说明 |
|---|---|---|
| 数据完整性 | `integrity` | 单独一条，在最上面 |
| CPU 检查 | `numeric`、`frame`、`duplicate`、`vlm_prep` | 凡是用 CPU 的（「模型准备」按资源算进来，需求方选定） |
| 模型检查 | `vlm` | 调用模型 |
| 生成报告 | `final`、`report`、`verify`（及旧的 `export`） | 原「生成报告 & 产物交付」；D69 起不再导出交付数据集，最后一步仍把报告与结果清单传到交付目录并核验 |

组的口径：完成条数取组内各段完成数的最小值（每段都处理全部所选条目），在途条数相加，耗时取组内各段的并集；组的状态按 失败 > 进行中 > 有出错条目 >
等待中 / 已完成 合并（同今天「生成报告」的合并规则）。

显示档名的地方一律按组：任务详情「详细进度」（完整性一条横条，CPU 检查、模型检查两张卡片与运行时间线，生成报告一根条）、Episode 流水线（「最近阶段」
写三组的勾，耗时列是「完整性 / CPU / 模型」三列、按组内各段相加）、任务列表的进度、概览里运行中任务的当前档、模块卡片与报告「本次质检范围」的
「所在块」（只写组名）、执行计划、性能剖析的阶段表、日志的筛选（选组，按组内各段查）。接口与日志本身仍用段 id，前端负责归组。

## 3. 契约

- **C1 6.0**：删 `dedup` 模块；新模块 `duplicate_check`（name_zh「重复检测」，`native`，`needs: raw_bytes`，段 `duplicate`，细码 `duplicate` SET-1 info，
  明细表 `duplicate_groups`）；`BLOCKS["cpu"]` 的 `dedup` 改 `duplicate`；删 `data_integrity` 的 `duplicate_content`。`taxonomy.json` 的 SET-1 平台注记跟着改。
- **C2**：计划的段 id、终判清单里与去重相关的 `gate`、`kind: duplicate`、`duplicate_of` 删掉；报告的 `dedup_groups` 换成 `duplicate_groups`；版本号按
  `docs/contracts/README.md` 的规则升。
- **C4 6.0.0**：`RegistryStage` 等枚举里 `dedup` → `duplicate`，`duplicate_of` 从 `EpisodeNote` / 裁决问题里删掉，复议说明不再提去重；中英变更记录。
- **C3**：只改说明。

## 4. 验收（F16.3）

1. 一份含重复的数据集（对账工具的夹具里 ep 7 是 ep 3 的字节拷贝）：两条都有 SET-1 发现、互相写明，判决都是通过；没有「可复议」，人工裁决里没有它们。
2. 不勾重复检测的任务判决逐位不变（对账回放）。
3. 运行中与跑完的任务，详细进度、流水线、任务列表、报告里只出现「数据完整性 / CPU 检查 / 模型检查 / 生成报告」。
4. 代码（v2）、契约、设计、README、CLAUDE.md 里不再有精确去重与 `dedup` 段的说法（v1 的 `curation run` 与 `ui/` 除外）。

## 5. 落地记录（2026-10-11）

- **代码**：注册表 6.0（`contracts/modules.py`）；`pipeline/check_stage.py` 的 `_duplicate` / `_dup_settle` / `_dup_seed`（成组与续跑）；
  `pipeline/findings.py` 的 `duplicate_check` 派生；`aggregate.py` 删掉选留哪条与 `duplicate_of`；报告的 `duplicate_groups` / `duplicate_stats`；
  planner 与 Daemon 的段 id；`check --modules duplicate_check` 并发恒为 1。完整性的 `_duplicates` 与 `duplicate_content` 删掉。
- **只改显示的部分**：前端 `lib/taskView.ts` 的 `STAGE_GROUPS` / `groupStages()` / `groupsOf()`，`lib/pipelineRow.ts`、`PipelineActivity.tsx`、
  `PipelineEpisodesCard.tsx`、`PerfTab.tsx`（组内各段同时进行，墙钟取最长的一段）、`LogsTab.tsx`。日志按组筛要一次查几段，所以
  `GET /tasks/{id}/logs` 的 `stage` 可以逗号分隔（C4 6.0.0 里一并写了）。Daemon 内存准入的日志不再说「帧档」「这一档」。
- **测试**：CLI 测试链（`tests/cli/pipeline.py`）把重复检测挪进 CPU 块（frame 之后），和计划里的段顺序一致；`tests/cli/test_duplicate_stream.py`
  取代 `test_dedup_stream.py`（含跨调用成组、乱序到达、续跑）。对账工具的链路步骤叫 `check duplicate`，`compare` 比重复组（v1 的去重对读成组）。
- **旧任务**：不留兼容。有一处顺手的防御：Episode 流水线算单条耗时时跳过注册表里已经没有的模块（旧任务的 `dedup` 记录），列表不再 500；
  旧任务的报告、裁决等照常能打开，但续跑、重试、执行裁决会失败。
- **验收**：本机真 Daemon（`curator-daemon-local`，对账夹具 8 条，ep 7 是 ep 3 的字节拷贝）：ep 3、ep 7 都通过、各有一条 SET-1 info 发现、互相写明；
  没有被拒复议；详细进度只出现数据完整性 / CPU 检查 / 生成报告；日志按「CPU 检查」能查到 numeric、frame、duplicate 三段。对账黄金基线回放通过。
