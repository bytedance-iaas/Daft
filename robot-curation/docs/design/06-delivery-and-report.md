# 06 交付布局、增量重导出与报告

## 1. 交付目录布局

不做历史兼容（D7），所以这次可以按「增量友好」重新设计。

```
deliveries/<delivery-name>/
├── human-decisions/               人工裁决 CSV 副本（DB 是权威，这里是自包含副本）
│   ├── label_decisions.csv
│   ├── task_verdicts.csv
│   └── reject_appeals.csv
├── <run_id>/                      一次跑批 = 一个任务
│   ├── run.json                   任务快照：输入/模块/参数/预检/版本指纹
│   ├── plan.json                  执行计划（04 篇），存档便于复现与调优
│   ├── checks/                    ★ 模块级结果，子任务按模块覆盖
│   │   ├── timestamp_check/results.jsonl
│   │   ├── visual_quality/results.jsonl
│   │   └── ...
│   ├── passed.json  reject.json  review.json     聚合判决
│   ├── report.md  report.json  perf.json
│   ├── details/                   明细 CSV、证据帧、同步曲线、裁决视频片段
│   ├── export/
│   │   ├── manifest.json          ★ 产物清单，增量导出的依据
│   │   └── lerobot_curated/       交付数据集
│   └── _COMPLETE                  完整性标志，最后写
└── latest                         指向最近一次 run_id
```

两个 ★ 是本期的关键新增：`checks/` 让模块结果可独立覆盖，`export/manifest.json` 让导出可增量。

## 2. 版本指纹

`run.json` 里记录一组指纹，用于判断「结果能不能复用」：

```jsonc
{"fingerprints": {
  "code": "git:abc1234",                      // 产品代码版本
  "module_impl": {"visual_quality": "sha256:...", "task_success": "sha256:..."},
  "config": "sha256:...",                     // 生效的流水线配置
  "prompt": {"task_success": "sha256:...", "caption": "sha256:..."},
  "vlm": {"model": "doubao-seed-2-0-pro-260215", "thinking_effort": "medium"}
}}
```

`module_impl` 是该模块实现文件的哈希，`prompt` 是提示词全文哈希（v1 已有这个做法，
`skill_profile.taxonomy_guideline` 换 sha256 存档）。子任务重跑时把新指纹并排记下，
报告里明确标注「本模块结果来自子任务，代码/提示词版本为 X」。

## 3. 判决聚合

原样搬运 v1 `pipeline/verdict.py`：

- **硬门（hard）**：任一违反 → `reject`，理由记录归因到具体模块。
- **软分（soft）**：加权求和，低于阈值 → `reject`。
- **弃权（abstain）**：证据不足不判废 → `review`（待裁决队列）。

三份清单互斥且完备：`passed + reject + review = 参与质检的全部 episode`。
`aggregate` 每次全量重算，天然保证这个不变式。

## 4. 增量重新导出

### 4.1 为什么需要

子任务改了某个模块的判决 → `passed.json` 变化 → 交付数据集需要同步。
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
   {"episode_id": "000034", "new_index": 0,
    "content_key": "sha256:<源文件内容指纹>",
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
     renumber 内容相同但编号变了  → v2: parquet 改编号列 + 文件改名（视频整文件 mv/copy）
                                    v3: 落入受影响 chunk，该 chunk 重建
     add      新进来的           → 从源导出
     drop     被剔除的           → 删除产物
③ meta 文件总是重建（KB 级，不值得增量）
④ 远端同步：删掉不在新 manifest 上的旧对象（v1 sync_back 已有此逻辑，搬运）
⑤ 写新 manifest → 写 _COMPLETE
```

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
| 标注分歧 | 技能画像 ③ | 采纳建议改标 / 维持原标注 / 拿不准 / 整条弃用 | 改标的**重跑任务成败判定**；弃用直接进 reject |
| 任务成败弃权 | 任务成败判定 | 判成功 / 判失败 / 拿不准 | **不跑 VLM**，人说了算 |
| 被拒复议 | 任务成败判定的 reject | 捞回 / 维持拒绝 | 捞回则回到 passed |

三条纪律不能丢：

1. **「整条弃用」压过成败裁决**：点了弃用就是弃用，不管另一块点了什么。
2. **复议只受理归因于任务成败判定的拒绝**：时间戳、残段、运动学、同步这些物理/结构硬门是终局，
   界面不给入口，后端再校验一次（裁决记录是可被手改的数据，不能只靠界面把门）。
3. **「拿不准」是合法答案**：只记一笔，条目保留在队列里，不等于「判了」。

### 5.2 与任务状态机的关系（D10）

```
任务跑完 → state=succeeded，附加字段 pending_adjudication_count=N
   │
   ├─ 用户在裁决页逐条判 → POST /adjudication（只记录，不执行，可反复改）
   │
   └─ 点「执行裁决」→ 建 kind=apply_adjudication 的子任务
         └─ 跑 curation adjudicate-apply → 更新三份清单 + 报告追加小节
             └─ 若判决变化影响 passed 名单 → 置 delivery_stale=1
                 └─ UI 提示「交付数据集已过期」+「重新导出」按钮
```

裁决**只改判决和报告，不动交付数据集** —— 数据集的更新永远是用户显式发起的一次导出。
这样「我点一下裁决」和「几百 GB 的数据集被重写」之间隔着一道明确的确认。

## 6. 报告结构

```jsonc
// report.json
{
  "overview": {
    "dataset": {...}, "run": {...},
    "counts": {"total": 640, "passed": 512, "rejected": 96, "review": 32},
    "pass_rate": 0.8,
    "reject_reasons": [{"module": "task_success", "count": 61}, ...],
    "token_usage": {"prompt": 1820000, "completion": 64000, "requests": 10240},
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
    {"id": "video_action_sync", "state": "failed",
     "error": "VLM endpoint unreachable", "retry_url": "/tasks/<id>/retry?modules=video_action_sync"}
  ],
  "skipped_modules": [{"id": "kinematic_limits", "reason": "robot_type 'umi' not in registry"}],
  "perf": {"url": "/tasks/<id>/perf"}
}
```

### 6.1 性能剖析（原样搬运口径）

v1 已有的延迟分桶不能改口径，否则新旧不可比：

- 四个调用点各自的延迟分布（probe / endstate / caption / llm），带百分位。
- **墙钟口径**：第一次发出 → 最后一次返回的真实时长。
  ⚠️ 不能用「次数 × 均值」，并发下那个数会把 8 分钟说成 8 小时（v1 注释里的原话）。
- 有效并发度 = Σ请求耗时 / VLM 段墙钟；≈1 说明请求被串行化了。
- 对冲补发：`attempt=0/1` 分列，失败原因分类（timeout / http_error / connect_error）。
- **本期新增**：每个 stage 的墙钟占比、合并请求的节省量（合并后请求数 vs 未合并估算）。

### 6.2 报告页与模块的对应

前端按 `report.json` 的 `modules` 数组顺序渲染，**不在前端维护模块列表**。
每个小节的渲染器按 `module.id` 从一张注册表里查，查不到就用默认表格渲染器 —— 
这样加模块不需要改前端（见 `05-modules-and-preflight.md` §7）。
