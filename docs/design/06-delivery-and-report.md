# 06 交付布局、增量重导出与报告

## 1. 交付目录布局

不做历史兼容（D7），所以这次可以按「增量友好」重新设计。

```
deliveries/<delivery-name>/
├── <run_id>/                      一次跑批 = 一个任务（run_id 是启动时刻的时间戳），独立且不可被别的任务覆盖
│   ├── run.json                   任务快照：输入/模块/参数/预检/版本指纹
│   ├── source_manifest.json       ★ 源文件清单与版本指纹（D27）
│   ├── plan.json                  执行计划（04 篇），存档便于复现与调优
│   ├── autolabel/captions.jsonl   无标注条目的补充描述
│   ├── checks/                    ★ 模块级结果，子任务按模块覆盖
│   │   ├── timestamp_check/{parts/*.jsonl, results.jsonl}
│   │   ├── task_success/{parts/*.jsonl, results.jsonl}
│   │   ├── skill_profile/{captions.jsonl, taxonomy.json, assignments.jsonl, label_audit.json}
│   │   └── ...
│   ├── revisions/r0001/           ★ 一个结果版本 = 一套判决清单 + 一份报告，不可变，旧版本保留
│   │   ├── verdicts.jsonl  keep.txt                           漏斗判决（aggregate --phase funnel）
│   │   ├── passed.json  reject.json  held.json  review.json   终判（aggregate --phase final）
│   │   ├── report.md  report.json  perf.json  tables/*.parquet
│   │   └── commit.json            最后写：这一版用了每个模块的哪些 part、应用了哪些裁决、源数据指纹
│   ├── passed.json  reject.json  report.md   当前版本的副本，方便离线翻看；切换版本后才覆盖
│   ├── human-decisions/           本任务的人工裁决 CSV 副本（DB 是权威，这里是自包含副本）
│   │   ├── label_decisions.csv
│   │   ├── task_verdicts.csv
│   │   └── reject_appeals.csv
│   ├── details/                   明细 CSV、证据帧、同步曲线、裁决视频片段、vlm_latency.csv
│   ├── logs/<stage>.jsonl         各 stage 的完整日志
│   ├── export/
│   │   ├── manifest.json          ★ 产物清单，增量导出的依据
│   │   └── lerobot_curated/       交付数据集
│   └── _COMPLETE                  完整性标志，交付核验通过后最后写
└── latest                         指向最近一次发布成功的完整版本
```

★ 是本期的关键新增：`checks/` 让模块结果可独立覆盖，`export/manifest.json` 让导出可增量，
`source_manifest.json` 钉住源数据版本，`revisions/` 让结果的替换是原子的 ——
对象存储没有跨文件事务，所以不去改已有的文件，而是写一个新版本目录、最后落 `commit.json`，
再由 Daemon 用 CAS 把库里的 `result_rev` 指过去。读方只认有 `commit.json` 的版本。

**同一个交付目录可以跑多次**（D29）：每个任务一个 `<run_id>/`，互不覆盖，构成这个交付目录的历史版本。
目录名撞了就换一个，绝不写进别人的批次目录。

**`latest` 只指向完整成功的版本**。一个批次要同时满足：任务状态是已完成（没有失败的模块、没有待补跑的条目）、
交付数据集已导出且不是过期状态、交付核验通过（`_COMPLETE` 在）。满足时才把 `latest` 原子地改过去；
补跑、裁决之后要重新导出并核验通过，才会再动它。它指向的是**最近一次发布成功**的版本，不是最近一次启动的任务。
这和 v1 不同：v1 的 `latest` 只表示「最近跑的是哪一次」。

**同一交付目录的发布串行**：导出、核验、写 `_COMPLETE`、改 `latest` 这一段，Daemon 按交付目录加锁，一次只让一个任务做。

v1 用 `passed.json` 兼作完整性标志，并靠「普通文件 → `meta/info.json` → `passed.json` → `latest`」的
上传顺序来保证读方看不到半成品。v2 改用显式的 `_COMPLETE`，但**上传顺序的纪律保留**：
`_COMPLETE` 和 `latest` 永远最后传，且不参与按大小跳过的续传判断。

## 2. 版本指纹

`run.json` 里记录一组指纹，用于判断「结果能不能复用」：

```jsonc
{"fingerprints": {
  "code": "git:abc1234",                      // 产品代码版本
  "module_impl": {"visual_quality": "sha256:...", "task_success": "sha256:..."},
  "config": "sha256:...",                     // 生效的流水线配置
  "prompt": {"task_success": "sha256:...", "caption": "sha256:..."},
  "vlm": {"model": "doubao-seed-2-0-pro-260215", "reasoning_effort": null}   // null = 请求里没带该字段
}}
```

`module_impl` 是该模块实现文件的哈希，`prompt` 是提示词全文哈希（v1 已有这个做法，
`skill_profile.taxonomy_guideline` 换 sha256 存档）。子任务重跑时把新指纹并排记下，
报告里明确标注「本模块结果来自子任务，代码/提示词版本为 X」。

## 3. 判决聚合

规则原样搬运 v1 `pipeline/verdict.py`，分两步，对应 `aggregate` 的两个阶段：

**漏斗判决**（六项检查 → keep / drop）：

- **硬门（hard）**：`passed=False` → drop，理由归因到具体模块。
- **软分（soft）**：加权均值低于阈值（0.5）→ drop。
- **弃权**：硬门 `passed=None`（证据不足）**不 drop**，只记入该条的未决项。
  v1 的立场是保守放行：判废要有实据，拿不准的先留下，交给人看。

**终判**（叠加判决之后的模块和人工裁决）：

- `dedup` 找出的重复项改判拒绝，理由「与 X 字节级完全重复」。
- 已执行的人工裁决落到对应条目上（判失败、整条弃用 → 拒绝；捞回 → 通过）。

三个维度分开表达，互不替代：

| 维度 | 取值 | 落在哪 |
|---|---|---|
| 质量判决 | 通过 / 拒绝 | `passed.json` / `reject.json` |
| 执行完整性 | 完整 / 待补跑 | `held.json`：执行出错、还没有完整结论的条目，既不算通过也不算拒绝，暂不交付 |
| 人工复核 | 要不要人看、看什么 | `review.json`：一个**视图**，不是第三个桶 |

```
全部参与质检的 episode = passed ∪ reject ∪ held，三者互斥且完备
review 与它们正交：多数条目在 passed 里（成败弃权、标注分歧，保守放行后等人确认），
                  也可以在 reject 里（归因于任务成败判定的拒绝，可复议）
```

**正常弃权和执行出错，处理方式不同**（D24）：

- 模型或算法正常给出的「判不了」，以及标注分歧：维持 v1，**先交付，同时进人工复核**。
- 执行层面的失败（解码失败、模型调用重试用尽、进程崩溃）：**暂不交付**，进 `held`，等「重试」补跑；
  补跑成功后它回到正常的判决流程，该通过通过、该拒绝拒绝。
  口径取宽（D33）：只要有一次调用重试用尽或一路机位解码失败就算，哪怕 v1 的降级逻辑还能给出结论。
  补跑仍失败的（例如视频文件本身损坏）继续留在 `held`，报告里写明是哪一路、什么原因。
  规则对所有已勾选的模块一视同仁：哪怕只是技能画像给它打标失败了，这一条也先不交付 ——
  交付出去的每一条，报告里每个模块对它都有结论。

这是和 v1 的一处有意差异：v1 把调用失败也记成弃权、照常交付。

使用文档里那次实跑：输入 50 = 判废 7 + 交付 43，交付的 43 条里有 10 条待人工确认。
**待裁决的条目计入交付、会被导出**，人工判失败或弃用之后，再经一次重新导出剔除。
`aggregate` 每次全量重算，天然保证这些关系。

## 4. 增量重新导出

### 4.1 为什么需要

子任务或人工裁决改了判决 → `passed.json` 变化（条目增减，或任务文本被人工改标）→ 交付数据集需要同步。
需求明确：**不自动重建，显式提示用户点「重新导出」，且导出要增量、不要全量**（D9）。

### 4.2 两种源格式的代价差一个数量级

这是 v1 `export/lerobot_writer.py` 里已经写明的事实，增量方案必须分开设计：

| 源格式 | 布局 | 全量导出代价 | 增量可行性 |
|---|---|---|---|
| **v2** | 每条 episode 独立 parquet + 独立 mp4 | 文件拷贝，零视频重编码 | **高**：mp4 整文件复用 |
| **v3** | 多条 episode 合并进同一 parquet/mp4 | 视频要重编码拼接 | **中**：按 chunk 粒度重编码 |

### 4.3 manifest 驱动的增量算法

```jsonc
// export/manifest.json
{"schema_version": "1.0",
 "source_format": "lerobot_v2",
 "episodes": [
   {"episode_index": 34, "new_index": 0,
    "content_key": "sha256:<源文件内容指纹>",
    "task_key": "sha256:<写入的任务文本 + 来源>",      // 人工改标后它会变
    "artifacts": {"parquet": "data/chunk-000/episode_000000.parquet",
                  "videos": {"wrist": "videos/chunk-000/wrist/episode_000000.mp4"}}}
 ],
 "meta_files": ["meta/info.json", "meta/episodes.jsonl", "meta/tasks.jsonl", "meta/stats.json"]}
```

重新导出时：

```
① 算新的 passed 名单（不含 held）→ 新 episode 序列
② 与 manifest.episodes 做 diff：
     keep     内容相同且编号不变  → 一个字节都不动
     relabel  只有任务文本变了    → 只改 parquet 的 task_index 列与 meta/tasks，视频不动
     renumber 内容相同但编号变了  → v2: parquet 改编号列 + 文件改名（视频整文件 mv/copy）
                                    v3: 落入受影响 chunk，该 chunk 重建
     add      新进来的           → 从源导出
     drop     被剔除的           → 删除产物
③ meta 文件总是重建（KB 级，不值得增量）
④ 远端同步：删掉不在新 manifest 上的旧对象（v1 sync_back 已有此逻辑，搬运）
⑤ 写新 manifest → 交付核验（curation verify）→ 写 _COMPLETE
```

任务文本按来源写入交付数据集，并带 `instruction_source`：原始标注 / 自产 caption（autolabel 补的）/ 人工改标。
这是 v1 的既有行为 —— 客户拿到的成品包里，无标注的条目有了描述，被人工纠正的标注是纠正后的。

**交付是否过期，按指纹算，不按「名单变没变」算。** 指纹 = 通过名单及其顺序 + 每条的任务文本与来源 +
源数据指纹 + 导出格式与参数。它和上次成功导出时记下的 `export_fingerprint` 不一样，`delivery_stale` 就是 1。
只改了标、名单一条没动，同样是过期 —— 成品包里的任务文本还是旧的。

**关键约束：LeRobot 要求 `episode_index` 和全局 `index` 连续。**
所以「剔除中间一条」必然引发后续所有 episode 的重新编号。
但重新编号 ≠ 重新编码：
- v2：只改 parquet 的编号列（廉价，几 MB/条）+ 视频文件重命名（零成本），
  这是增量导出的主要收益来源。
- v3：编号列同样廉价，但视频在合并 mp4 里，**只重编码 drop/add 命中的 chunk**，
  未受影响的 chunk 原样保留。

### 4.4 原子性

- 所有写入先写临时前缀，完成后再发布（搬 v1 `export/publish.py` + `safe_write.py`）。
- ⚠️ 已知坑（v1 注释里有实锤）：TOS 的 FSX 挂载**拒绝随机写**，PyAV 复用编码器要 seek 回
  文件头改写 moov → EINVAL。所以视频必须**先写本地临时文件，再整文件拷贝到交付目录**。
  增量导出同样受此约束，不得图省事直接往远端写。
- `_COMPLETE` 最后写。读方（报告页、下游训练）只认带 `_COMPLETE` 的批次。
- 增量重新导出是**就地**改这个批次的 `export/`（对象存储没有原子的目录切换，整份另存一遍又要翻倍占空间）。
  所以顺序是：先删 `_COMPLETE` → 改 → 核验 → 再写 `_COMPLETE`。这段时间里顺着 `latest` 找过来的读方会看到
  「没有 `_COMPLETE`」，应当稍后再来，而不是读一个改到一半的数据集。读方的正确姿势写进交付目录的 README。

## 5. 人工裁决

### 5.1 三条线（原样搬运 v1 语义）

| 线 | 来源模块 | 人可以做的判断 | 执行后果 |
|---|---|---|---|
| 标注分歧 | 技能画像 ③ | 采纳建议改标（可自行改写）/ 维持原标注 / 拿不准 / 整条弃用 | 改标的按新标注**重跑任务成败判定**（例外见纪律 4）；弃用直接进 reject |
| 任务成败弃权 | 任务成败判定 | 判成功 / 判失败 / 拿不准 | **不跑 VLM**，人说了算 |
| 被拒复议 | 任务成败判定的 reject | 捞回 / 维持拒绝 | 捞回则回到 passed |

一条 episode 可能同时有标注分歧和成败弃权，裁决页把它们放在同一张卡片里，视频只看一次。

四条纪律不能丢：

1. **「整条弃用」压过成败裁决**：点了弃用就是弃用，不管另一块点了什么。
   v1 在入口处先把被弃用的条目从成败裁决里滤掉，否则「判成功」会把它重新写回 passed。
2. **复议只受理归因于任务成败判定的拒绝**：时间戳、残段、运动学、同步这些物理/结构硬门是终局，
   界面不给入口，后端再校验一次（裁决记录是可被手改的数据，不能只靠界面把门）。
3. **「拿不准」是合法答案**：只记一笔，条目保留在队列里，仍计为待裁，执行时不动它。
4. **改标通常要重跑模型，但人已经给了成败结论的不重跑**：同一条既改了标、又被人判了成功/失败，
   就以人的结论为准，来源如实记为人工 —— 防的是机器自产自证，不是防人。

### 5.2 与任务状态机的关系（D10）

```
任务跑完 → state=succeeded，附加字段 pending_adjudication=N
   │
   ├─ 用户在裁决页逐条判 → POST /adjudication（只记录，不执行，可反复改，追加式保存）
   │
   └─ 点「执行裁决」→ 建 kind=apply_adjudication 的子任务
         ├─ curation adjudicate-apply          把裁决落到判决上，幂等：已应用的自动跳过
         ├─ check --modules task_success        只跑「改了标且没有人工成败结论」的那几条
         ├─ aggregate                           重算三份清单
         ├─ check --modules skill_profile --incremental
         │                                      被裁决的条目按新标注重新归位，被剔除的从画像里移除
         ├─ report                              生成新版本报告，追加「人工裁决」小节
         └─ 判决或任务文本变了 → 置 delivery_stale=1
               └─ UI 提示「判决已更新，交付数据集待重新导出」+「重新导出」按钮
```

裁决**只改判决和报告，不动交付数据集** —— 数据集的更新永远是用户显式发起的一次导出（D9）。
这样「我点一下裁决」和「几百 GB 的数据集被重写」之间隔着一道明确的确认。
这是和 v1 的一处有意差异：v1 的 `rejudge` 执行完会顺手重新导出。

### 5.3 裁决只属于本任务

**人工裁决不跨任务**（D32）。一个任务的裁决只对这个任务的判决、报告和交付数据集生效；
同一个数据集再建一个任务，或者往同一个交付目录再跑一次，都是从零开始，不会带上之前的裁决。
CSV 副本也因此放在批次目录里（`<run_id>/human-decisions/`），不放在交付目录根上。

这是和 v1 的一处有意差异。v1 把裁决放在交付目录根上、跨批次沿用（同名交付再跑，旧裁决还在，
但要再执行一次才生效）。拿掉它的理由很直接：只靠交付目录和 episode 下标去认旧裁决，
数据一旦变过（重新采集、重新编号、改过标注），旧裁决就会套到不相干的条目上。

### 5.4 补判调用失败的弃权条目

v1 有 `rejudge --retry-abstained`：只重判因「VLM 调用/解析失败」而弃权的条目，模型真说「看不出来」的不重判
（那种重判一百次也一样）。v2 里这类条目不再算弃权，而是 `error`、待补跑（§3），「重试」补跑的正是它们（03 篇 §3.2）。

## 6. 报告结构

```jsonc
// report.json
{
  "overview": {
    "dataset": {...}, "run": {...},
    "counts": {"total": 640, "passed": 508, "rejected": 96, "held": 36, "review": 32},
    "pass_rate": 0.79,               // 通过 / 全部；held 既不算通过也不算拒绝
    "reject_reasons": [{"module": "task_success", "count": 61}, ...],
    "token_usage": {"prompt": 1820000, "completion": 64000, "reasoning": 41000,
                    "cached": 903000, "requests": 10240},
    "duration_s": 3600
  },
  "modules": [                       // ★ 与勾选模块一一对应
    {"id": "visual_quality", "state": "succeeded", "gate": "soft",
     "summary": {...},
     "tables": [{"id": "visual_quality", "rows": 640, "url": "/report/tables/visual_quality"}],
     "adjudication": null},
    {"id": "task_success", "state": "succeeded", "gate": "hard",
     "summary": {...},
     "adjudication": {"pending": 32, "url": "/tasks/<id>/adjudication?source=task_success"}},
    {"id": "skill_profile", "state": "failed",
     "error": "VLM endpoint unreachable", "retry": {"modules": ["skill_profile"]}}
  ],
  "skipped_modules": [{"id": "kinematic_limits",
                       "reason": "机器人型号 umi_dual_handheld_gripper 不在规格库"}],
  "integrity": {...},                // 数据包完整性：格式、缺失字段、无标注条数、语义 profile / 动作语义预检结论
  "perf": {"url": "/tasks/<id>/perf"}
}
```

### 6.1 性能剖析（原样搬运口径）

v1 已有的延迟分桶不能改口径，否则新旧不可比：

- 五类调用各自的延迟分布（probe / endstate / arbitration / caption / llm），带 P50 / P90 / P99。
  这五个标签是 `details/vlm_latency.csv` 的数据契约，一个字不许改。
- 用的是哪个模型服务、流水线容器的 CPU / 内存配额（v1 的 `perf_backend` / `perf_env`）。
- **墙钟口径**：第一次发出 → 最后一次返回的真实时长。
  ⚠️ 不能用「次数 × 均值」，并发下那个数会把 8 分钟说成 8 小时（v1 注释里的原话）。
- 有效并发度 = Σ请求耗时 / VLM 段墙钟；≈1 说明请求被串行化了。
- 对冲补发：`attempt=0/1` 分列，失败原因分类（timeout / http_error / connect_error）。
- **本期新增**：每个 stage 的墙钟占比、外层重试的次数与挽回数、Token（含思维链与缓存命中）、
  合并请求的节省量（合并后请求数 vs 未合并估算）。
- **子任务之后同步更新**（需求硬要求）：延迟明细是追加式的，每行带 `subtask_id`；
  性能剖析默认展示全部调用的合计，可切到「仅主流程」或某一次子任务。Token 同理，合计里包含重试花掉的。
- **因中断而重做的 episode 数**：Pod 重启或崩溃时在飞的请求结果丢了，那几条恢复后会重新调用一次。
  服务端可能已为丢掉的那次计费，而它的 `usage` 我们收不到，Token 统计里没有，这里如实列出条数（D26）。

报告和判决清单同属一个结果版本：每次生成写进新的 `revisions/r<NNNN>/`，完整上传并核验之后才切换生效版本（D25）。
补跑前后的版本都留着，任务详情的执行时间线上可以逐个打开。

### 6.2 报告页与模块的对应

前端按 `report.json` 的 `modules` 数组顺序渲染，**不在前端维护模块列表**。
每个小节的渲染器按 `module.id` 从一张注册表里查，查不到就用默认表格渲染器 —— 
这样加模块不需要改前端（见 `05-modules-and-preflight.md` §7）。

现有八个模块的小节内容沿用 v1 报告已有的东西，不是一张默认表格能撑起来的：

| 模块 | 摘要指标 | 图 / 专用视图 | 明细 |
|---|---|---|---|
| 时间戳检查 | 不合格条数，按原因（乱序 / 跳变 / 残段） | — | 逐条的异常位置 |
| 运动学极限 | 越限条数、涉及的关节 | — | 逐条逐关节的越限幅度；速度域标定结论 |
| 运动质量 | 平均分、子项适用性（哪些子项自弃权） | **卡顿动作时间线**：每条一根三色条（卡顿 / 空闲 / 正常），可按卡顿时长排序 | `motion_details.csv` 切片；执行器卡死单列、不进总分 |
| 视觉质量 | 平均分、逐相机分布 | — | 逐相机打分明细；生效的质检参数 |
| 视频-动作同步 | 错位条数；被标注的相机数 | **同步曲线卡片**：逐相机的画面动量 / 关节速度曲线 + 互相关曲线，附逐相机诊断（入镜晚 / 信号弱 / 假峰）；可筛「只看有标注或异常的」 | 逐相机 lag 与相关峰值 |
| 任务成败判定 | 通过 / 判废 / 弃权条数；弃权原因分布 | 判决卡：初判 → 复核汇票 → 护栏 → 仲裁的留痕 | `task_details.json`；右上角「去裁决（N）」 |
| 精确去重 | 重复组数、被剔除条数 | — | 重复组清单 |
| 技能画像 | 技能族数、样本偏少的族 | **技能分布条形图** + **两级技能体系表**（按族着色，列出每个子技能下的 episode） | 标注分歧队列入口「去裁决（N）」 |

报告开头另有两块不属于任何模块：**质检总览**（输入 = 判废 + 交付，判废原因分布，平均质量分）
和**数据包完整性**。任一明细表里点某条 episode，打开逐条下钻抽屉（03 篇 §6），看它在所有模块下的读数、
证据帧和各机位视频，支持多机位同时播放 —— 这是 v1「轨迹」页的对应物，与「小节和模块一一对应」不冲突。
