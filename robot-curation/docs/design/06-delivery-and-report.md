# 06 交付布局、增量重导出与报告

## 1. 交付目录布局

不做历史兼容（D7），所以这次可以按「增量友好」重新设计。

```
deliveries/<delivery-name>/
├── human-decisions/               人工裁决 CSV 副本（DB 是权威，这里是自包含副本），跨批次累积
│   ├── label_decisions.csv
│   ├── task_verdicts.csv
│   └── reject_appeals.csv
├── <run_id>/                      一次跑批 = 一个任务（run_id 是启动时刻的时间戳）
│   ├── run.json                   任务快照：输入/模块/参数/预检/版本指纹
│   ├── plan.json                  执行计划（04 篇），存档便于复现与调优
│   ├── autolabel/captions.jsonl   无标注条目的补充描述
│   ├── checks/                    ★ 模块级结果，子任务按模块覆盖
│   │   ├── timestamp_check/{parts/*.jsonl, results.jsonl}
│   │   ├── task_success/{parts/*.jsonl, results.jsonl}
│   │   ├── skill_profile/{captions.jsonl, taxonomy.json, assignments.jsonl, label_audit.json}
│   │   └── ...
│   ├── verdicts.jsonl  keep.txt                  漏斗判决（aggregate --phase funnel）
│   ├── passed.json  reject.json  review.json     终判（aggregate --phase final）
│   ├── report.md  report.json  perf.json
│   ├── details/                   明细 CSV、证据帧、同步曲线、裁决视频片段、vlm_latency.csv
│   ├── logs/<stage>.jsonl         各 stage 的完整日志
│   ├── export/
│   │   ├── manifest.json          ★ 产物清单，增量导出的依据
│   │   └── lerobot_curated/       交付数据集
│   └── _COMPLETE                  完整性标志，交付核验通过后最后写
└── latest                         指向最近一次 run_id
```

两个 ★ 是本期的关键新增：`checks/` 让模块结果可独立覆盖，`export/manifest.json` 让导出可增量。

和 v1 一样，**同一个交付目录可以跑多次**：每次一个 `<run_id>/`，互不覆盖，`human-decisions/` 在它们之上共用。
产品上「一个任务 = 一个批次 = 一份报告」。约束只有一条：一个交付目录只绑定一个输入数据集（01 篇 §2.7）。

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

产出三份清单，关系是这样的：

```
全部参与质检的 episode = passed ∪ reject，两者互斥
review ⊂ passed        带未决问题、等人工确认的那部分（成败弃权、标注分歧）
```

`review` 不是第三个互斥的桶。使用文档里那次实跑：输入 50 = 判废 7 + 交付 43，交付的 43 条里有 10 条待人工确认。
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
① 算新的 passed 名单 → 新 episode 序列
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
         ├─ report                              报告追加「人工裁决」小节，区分本次新裁与沿用的
         └─ 判决或任务文本变了 → 置 delivery_stale=1
               └─ UI 提示「判决已更新，交付数据集待重新导出」+「重新导出」按钮
```

裁决**只改判决和报告，不动交付数据集** —— 数据集的更新永远是用户显式发起的一次导出（D9）。
这样「我点一下裁决」和「几百 GB 的数据集被重写」之间隔着一道明确的确认。
这是和 v1 的一处有意差异：v1 的 `rejudge` 执行完会顺手重新导出。

### 5.3 裁决跟着交付目录走

v1 的裁决记录放在交付目录根上（`human-decisions/`），同一个交付目录再跑一次，之前的裁决还在。
这个语义保留，但要说清楚它**不是自动生效**：

- 新任务跑主流程时**不读**任何历史裁决（v1 的 `run` 也不读），判决只反映机器的结论。
- 新任务的裁决页上，历史裁决显示为「沿用自此前的人工裁决 · 待应用」，和本次新裁的分开计数；
  点「执行裁决」时一并落实。报告里单列一节说明哪些结论是沿用来的。
- 「是否已应用」按任务记录（01 篇 §2.7 的 `adjudication_applied`），所以重复点执行是安全的。

想从零开始、不沿用历史裁决，就换一个交付目录。

### 5.4 补判调用失败的弃权条目

v1 有 `rejudge --retry-abstained`：只重判因「VLM 调用/解析失败」而弃权的条目，模型真说「看不出来」的不重判
（那种重判一百次也一样）。v2 把它并入「重试」：`retry` 默认的 `episodes=errors` 就是这个范围（03 篇 §3.2）。

## 6. 报告结构

```jsonc
// report.json
{
  "overview": {
    "dataset": {...}, "run": {...},
    "counts": {"total": 640, "passed": 512, "rejected": 96, "review": 32},
    "pass_rate": 0.8,
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
