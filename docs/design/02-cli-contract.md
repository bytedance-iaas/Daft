# 02 CLI 契约

## 1. 设计原则

1. **一条命令一件事**。命令之间通过文件系统/对象存储传递中间产物，不在一条命令里串流程。
   组合由 Daemon 或用户脚本完成。
2. **默认输出给人看（英文），`--json` 给机器看**。两种输出的信息量可以不同，
   但 `--json` 的 schema 是冻结契约，改动需要走版本号。
3. **零隐式状态**。不读用户 home 下的配置、不写隐藏文件；一切从参数、环境变量和显式指定的配置文件来。
4. **不重试、不并发**，除非显式要求。默认参数下就是一次直来直去的执行；
   `--concurrency` / `--retry` / `--hedge` 是可选行为开关。这条是需求的硬要求。
5. **两类命令，界线分明**（D22）：
   - **原子命令**（§3.1–3.9）在本地干活，不知道 Daemon 的存在，也没有「任务」这个概念；
   - **客户端命令**（§3.10，`curation task …`）只调 Daemon 的 REST API，给 Agent 和脚本用。

## 2. 全局参数

| 参数 | 说明 |
|---|---|
| `--json` | 机器可读输出到 stdout，人类日志一律走 stderr |
| `--config <path>` | 流水线 YAML，叠加在出厂默认之上 |
| `--set k=v` | 单值覆盖，可重复 |
| `--log-level` | `error`/`warn`/`info`/`debug`，默认 `info` |
| `--region` / `--input-region` / `--output-region` | TOS 地域 |

**凭证只走环境变量**，不进 argv。理由：argv 在 `ps` 里全局可见，容器里同样。

| 用途 | 环境变量 |
|---|---|
| 读输入数据集 | `CURATION_INPUT_TOS_ACCESS_KEY` / `CURATION_INPUT_TOS_SECRET_KEY` |
| 写交付目录 | `CURATION_OUTPUT_TOS_ACCESS_KEY` / `CURATION_OUTPUT_TOS_SECRET_KEY` |
| 两者相同时的简写 | `TOS_ACCESS_KEY` / `TOS_SECRET_KEY`（v1 既有；上面两组没设时回落到它） |
| 临时凭证的会话令牌（可选） | `CURATION_INPUT_TOS_SESSION_TOKEN` / `CURATION_OUTPUT_TOS_SESSION_TOKEN` |
| VLM | `--vlm-api-key-env <变量名>` 指定从哪个变量读（v1 既有设计），默认 `ARK_API_KEY` |

输入和输出必须能用**两套不同的访问密钥**：源数据常常是别人账号下的只读桶，交付写进自己的桶。
HuggingFace 缓存桶匿名读，不需要输入密钥。

凡是读源数据的命令都接受 `--source-manifest <file>`（§3.3）：传了就逐个对象校验大小与 ETag，
对不上立即以退出码 6 结束。不传则不校验 —— 直接用 CLI 的人自己负责数据不变。

episode 一律用**整数下标**表达，语法沿用 v1：`34`、`10-20`、`3,10-12`，或 `@file`（每行一个下标）。
负数、倒序区间、跨度超过 100 万的区间直接拒绝。下标是数据集自己的编号：多数数据集是 0 … count-1；从大数据集里取出、
没有重新编号的子集保留源编号，mcap 按文件名 `episode_<N>.mcap` 编号，这时预检另写 `dataset.episode_indices`（§3.1），
「全部」「前 N 条」与自选都从这些编号里选（F12.8，2026-10-02）。

**mcap / Lance 数据集**（D44）另有两处约定：

- 读源数据的命令（`check`、`aggregate`）接受 `--selection <整个任务的所选>`。
  v1 用所选 episode 的前 100 条判定这两种数据集的语义（控制模式、单位等），而 v2 的一条命令只读某一档的幸存者，
  所以 Daemon 把整个任务的所选另外传进来；不传时取 `--episodes`。LeRobot 数据集的语义样本是数据集本身的前 100 条，不看它。
- v1 的两个读取器只认本地目录，`tos://` 上的数据先拉到本地副本再读：环境变量 `CURATION_SOURCE_CACHE` 指定副本放在哪
  （Daemon 给每个任务一个目录，同一任务的各条命令复用，运行结束时删掉）；不设时每条命令用自己的临时目录，结束就删。
  mcap 读到哪条下载哪条的文件，Lance 整表下载。每个副本按列举时的大小与 ETag 核对，对不上退出码 6；只读源桶，从不往里写。

## 3. 命令清单

### 3.1 `curation preflight` — 预检

```bash
curation preflight --input tos://bucket/datasets/my_dataset --input-region cn-beijing \
                   [--source tos|public|local] [--vlm-backend <name>] \
                   [--embodiment-id <型号>] [--modules a,b,...] --json
```

只读 metadata（`meta/info.json`、任务表、文件清单），**不读样本数据**，秒级返回。输出：

```jsonc
{
  "schema_version": "1.0",
  "format": {
    "kind": "lerobot",            // lerobot | mcap | lance | lancedb | rrd | unknown（前三种可支持，D44）
    "version": "v2",              // v2 | v3 | null；lance 是元数据的 LeRobot 版本 v3，mcap 为 null
    "supported": true,            // false ⇒ 全部模块不支持
    "detail": "LeRobot v2, 200 episodes, 3 cameras"
  },
  "validation": [],               // info.json 结构校验的错误；非空 ⇒ supported=false，原因照抄给用户
  "dataset": {
    "episode_count": 200,         // 条数；编号是 0 … count-1 时只给条数，不返回完整清单
    // "episode_indices": "1,3,5", // 只在编号不是 0 … count-1 时出现（保留源编号的子集、按文件名编号的 mcap），写法同 --episodes（F12.8）
    "cameras": ["exterior_1", "exterior_2", "wrist"],
    "fps": 15.0,
    "robot_type": null,           // info.json 里读不到为 null
    "total_frames": 51200,
    "labels": {"with_task": 112, "without_task": 88},
    "profile": {"matched": "droid", "by": "robot_type"}   // 数据集语义 profile；未命中为 null
  },
  "modules": [
    {"id": "timestamp_check", "availability": "available"},
    {"id": "kinematic_limits", "availability": "needs_input",
     "reason": "robot_type not found in info.json; pick a model or skip this module",
     "reason_code": "robot_type_unknown", "reason_args": {"robot_type": null},
     "input_hint": {"field": "embodiment_id",
                    "options": ["agibot", "aloha", "franka", "google_robot", "pusht",
                                "so100", "so101", "ur5", "widowx"]}},
    {"id": "task_success", "availability": "available",
     "notes": ["88 episodes have no task text; the model will caption them first"]}
  ],
  "meta_fingerprint": "sha256:…",   // meta 文件的指纹，和全量文件清单的指纹一起记在数据集登记上；开始任务时再比（D37）
  "warnings": ["episodes_stats.json missing; per-episode stats will be recomputed"]
}
```

读到了型号但规格库不支持时，是另一种形态（以 umi 为例）：

```jsonc
{"id": "kinematic_limits", "availability": "unsupported",
 "reason": "robot_type 'umi_dual_handheld_gripper' is not in the embodiment registry (supported: agibot, …)",
 "reason_code": "embodiment_unsupported",
 "reason_args": {"subject": "umi_dual_handheld_gripper", "given_by": "robot_type", "supported": ["agibot", "…"]}}
```

`reason` 是英文，给终端和日志看；界面按 `reason_code` 和 `reason_args` 渲染中文，已知的原因码列在
`cli/preflight.schema.json` 里，遇到不认识的就显示 `reason`（契约 1.1）。

三态 `availability` 的判定规则见 `05-modules-and-preflight.md` §4。
没传 `--vlm-backend` 时，VLM 模块是 `needs_input`（`input_hint.field = "vlm"`），不是 `unsupported`。

mcap 与 Lance（D44，v1 PR #155）同样只读元数据、秒级返回：

- **mcap**：数据集根目录下一个 `.mcap` 文件一条 episode，编号照 v1（`episode_<N>.mcap` 按 N，混进来的其他 `.mcap` 不读；一个都不是就按文件名排序）。
  每个文件只读摘要区（通道、消息数、元数据记录），TOS 上是几次按范围读，不读消息。动作、状态、相机、任务文本、机器人型号
  按 v1 的 topic 规则认（站点的 `ingest.mcap_mapping` 优先）；时间轴取动作 topic 的 `log_time`，`fps` 为 null。
  全部 episode 都缺动作或都没有相机时是 `metadata_invalid`（v1 读不了）。
- **Lance**：lerobot-lance-convert（0.3.0 起）的三表布局，`meta/` 是 LeRobot v3.0 元数据、带 `storage_format: "lance"`，
  预检像 LeRobot v3 一样读 `meta/`（含 v1 的 `validate_info`）。三表齐但没有这个标记的旧插件布局是 `metadata_invalid`；
  只有别的 Lance 表的是 `lancedb`，不支持。
- 两种格式下 EEF–视频一致性与它的复核是 `unsupported`（`format_unsupported_by_module`：它们只读 LeRobot 的视频）；
  站点关掉了这种格式（`ingest.mcap_enabled` / `ingest.lance_enabled`，默认开）时全部模块 `unsupported`（`format_disabled`）。
  `.rrd` 维持不支持。

### 3.2 `curation plan` — 并发与合并计划

```bash
curation plan --preflight preflight.json --modules timestamp_check,visual_quality,task_success \
              --episodes 0-199 --cpu-cores 32 --vlm-parallelism 64 --json
```

纯计算，不碰网络。输出执行计划（分档、每档并发度与各闸门、VLM 请求合并分组）。
`--episodes` 不给就是数据集的全部条目；给了就要都在数据集里（按预检的 `episode_indices`，没有它时是 0 … count-1），
不在的是用法错误（退出码 2），提示写明数据集有哪些编号。
Daemon 内部直接调用同一个 planner 库函数；CLI 形态是为了可调试、可复现。
计划 schema 见 `04-concurrency-and-vlm-merge.md` §3。

### 3.3 `curation snapshot` — 固化源文件清单

```bash
curation snapshot --input tos://... --episodes 0-199 --out <run-dir>/source_manifest.json --json
```

列出本次任务要读的全部源对象（meta 文件 + 所选 episode 的 parquet 与视频），记下键、大小、ETag：

```jsonc
{"schema_version": "1.0", "input": "tos://bucket/datasets/droid_lerobot",
 "objects": [{"key": "meta/info.json", "size": 4211, "etag": "\"9b2c…\""},
             {"key": "videos/chunk-000/wrist/episode_000034.mp4", "size": 1839201, "etag": "\"77aa…\""}],
 "summary": {"count": 804, "bytes": 1520331122, "digest": "sha256:…"}}
```

只列目录，不读内容：一万条 episode 也就几十次 LIST 请求。本地路径用大小 + 修改时间代替 ETag。
mcap 数据集记全部 `.mcap` 文件（v1 按目录里有哪些文件编号，每个文件又是一条的数据），Lance 数据集记 `meta/` 与三张表的全部对象
（读取器整表读）；这两种格式没有缺文件剔除的规则。
所选 episode 里缺 parquet 或某路视频的，照 v1 剔除（D40）：记进清单的 `skipped_episodes`（每条缺哪些键），
带这份清单的命令都不读它们，它们不进任何清单、不计入总数，报告的完整性一节列出来。
选了数据完整性的任务例外（D97，设计 29 §3）：清单照样记（快照不知道选了哪些模块），带清单的命令不去掉它们，完整性按缺的文件判废，
别的模块照 D92 写跳过记录；守卫对这类条只核对缺的文件仍然缺。
之后的每条命令带上 `--source-manifest` 去读，就保证了一个任务从头到尾只认这一个版本的源数据（D27）。

**对象清单只列一次**（2026-10-08）：远端数据集的对象清单列起来不便宜（DROID 一万个对象 11.6 秒，十万条的数据集要按分钟算），
而每档 worker 原来各列三遍（命令自己、源文件守卫、v1 的读端）。现在 `snapshot` 把它列到的清单存在清单文件旁边的
`.source_listing.json`（隐藏文件，不交付、不回灌），带 `--source-manifest` 的命令直接读它、交给守卫、喂给读端，不再向 TOS 列；
没有缓存的命令列一次并存下来给下一条。缓存一小时过期，用之前先 HEAD 一下 `meta/` 下的几个对象（变了就重新列），Daemon 续跑、重试前把它删掉，
所以隔了很久才起的 worker 仍在新清单上做守卫；一小时内只改了某个数据文件的情况，要到下一次重新列清单才会被发现。

### 3.4 ~~`curation autolabel`~~ — 已删除（D72）

没有任务标注的条目不再由模型补描述：`check --modules task_success` 碰到这样的条目写一条「没判」的记录
（`details.skipped = "no_task_text"`、`task_desc_source = "无"`，LABEL-2 的 `task_text_missing` 仅报告），不发模型请求；
这条 episode 的去留由其余检查决定，通过时交付清单里 `task_text` 为 null。D94 起这样的记录叫「跳过」（判定词 `skipped`），不算评估、不算转人工（设计 28 §3）。

### 3.5 `curation check` — 跑检查

```bash
curation check --modules visual_quality,video_action_sync --input tos://... --run-dir <dir> \
               --episodes @frame.txt --part 0003 \
               [--resume] [--vlm-backend <name>] [--vlm-reasoning-effort <档位>] \
               [--plan-stage stage.json] [--concurrency N] [--retry N] [--hedge] --json
```

**这是整个 CLI 最重要的命令。**

- `--modules` 通常只传一个。**同一档内的模块可以一起传**（D18），同进程共享一次解码 ——
  目前只有帧档的 `visual_quality,video_action_sync` 有这个需要。跨档传入直接报参数错误。
  档的定义在模块注册表里（05 篇 §1）。
- 结果按模块落盘：`<run-dir>/checks/<module>/parts/<part>.jsonl`，一行一条 episode，逐条追加。
  一次调用写一个 part（主流程一档只调一次，所以通常只有一个；续跑和重试各自再追加一个）。
  同一条 episode 在多个 part 里出现时，**编号大的 part 为准** —— 重试就是追加一个编号更大的 part，
  不改旧文件。`aggregate` 读之前先把 parts 压实成 `results.jsonl`。
- `--resume`：跳过已经有非 `error` 结果的 episode。暂停后恢复、崩溃后续跑、重试补跑都靠它。
- 进程每开始处理一条 episode，就把它记进 `<run-dir>/checks/<module>/inflight.json`，处理完抹掉。
  进程被杀或自己崩了，Daemon 从这个文件知道「出事时手上是哪几条」（04 篇 §7）。
- 数据集级模块（重复检测 `duplicate_check`，`duplicate` 段，D87）也是逐条判的（D70）：一条 episode 进来就算它的 action 哈希，和已经判过的比；
  哈希撞上了才读视频算内容指纹。一组重复的每一条都写明同组的其他条，组里先到的成员在这次调用结束（或暂停）时补写一条新记录；
  `checks/duplicate_check/groups.json` 记顺序、动作哈希撞车、算过的指纹与各组成员。并发恒为 1（状态在一个进程里），`--resume` 跳过已有记录的条目，
  再从它们的记录与 `groups.json` 重建状态（设计 26 §1）。

每行是记录 2.0（设计 17 §1，D56；C2 `cli/result-record.schema.json`，D90 起只有 2.0）：

```jsonc
// parts/*.jsonl 每行
{"episode_index": 34, "module": "timestamp_check", "status": "ok",       // ok | error
 "findings": [{"code": "gap", "item": "STRM-3", "severity": "high", "frames": [89, 90],
               "message_zh": "第 90 帧前时间戳跳了 1.10 s", "readings": {"gaps": [{"frame": 89, "dt_s": 1.1}]}}],
 "assessed": ["STRM-1", "STRM-3"], "unassessable": [],                  // 查过哪些项、哪些项查不了与原因
 "readings": {"duration_s": 12.4},
 "details": { /* 模块自定义，算法给出的留痕原样保留 */ },
 "evidence": ["details/evidence/ep000034/probe3_f0120.jpg"],
 "elapsed_s": 3.4, "error": null}
```

- 模块只报发现：细码、分类表的项、严重度、范围，可选帧 / 秒区间与读数，带一句中文 `message_zh`。拒不拒由策略在 `aggregate` 定；
  拿不准、可疑、要人看的都是发现（review 级），不是错。
- **出错与拿不准严格区分**（D24）。拿不准是模型或算法正常给出的「判不了」（证据灰区），照样交付，同时进人工复核。出错是执行层面的失败：
  解码失败、模型调用重试用尽、解析不了返回、内部异常；这一条暂不交付（held），等「重试」补跑。
- **口径取宽**（D33）：判这一条的过程中，任何一次模型调用在全部重试之后仍失败、或任一机位解码失败，这一行就是 `status: error`，
  `findings` 为空，`error` 字段列出失败的调用与机位：

```jsonc
"error": {"kind": "execution",
          "incidents": [{"step": "probe", "call_kind": "probe", "cause": "timeout", "attempts": 4},
                        {"step": "decode", "camera": "wrist", "cause": "InvalidDataError"}]}
```

  这份清单由编排壳记录：每条 episode 单独包一层模型调用与解码的封装，失败时记一笔再原样抛出，A 类算法代码一行不动。
- 单条 episode 出错不影响其他条，命令整体仍返回 0；只有「整个模块无法执行」（如 VLM 端点完全不可达）才非零退出。
- **退出码 0 不等于「全部成功」**。`--json` 的最终输出带逐状态计数、按细码的发现计数和输入集合的指纹，Daemon 据此给模块定
  `succeeded` 还是 `completed_with_errors`，不靠退出码猜：

```jsonc
{"schema_version": "2.0", "modules": {"task_success": {"part": "0001", "input_digest": "sha256:…",
   "episodes": {"total": 49, "ok": 47, "error": 2}, "error_episodes": [14, 31],
   "findings": {"failure": 6, "uncertain": 3},
   "skipped_reasons": {"integrity_rejected": 8, "no_task_text": 3}}}}   // 没有跳过时不出现（D94）
```

**完整性拦截与坏视频**（2026-10-11，设计 28，D92–D93）：
- 不是完整性本身的调用，动手前先看完整性对这条的记录（运行目录的分片，流水线下是同一份存储）；按任务策略有 blocking 发现的，不读数据、
  不解码、不调模型，给每个所选模块写一条跳过记录：`status: ok`、没有发现、覆盖的每一项记无法评估 `integrity_rejected`，
  `details = {"skipped": "integrity_rejected", "reason": "数据完整性已判废：…"}`。跳过的条目算 `ok`，不进 `error_episodes`，`--resume` 不重做。
- 视觉质量、同步、任务成败解码时遇到 `InvalidDataError`、找不到 moov 或视频流，记 `status: ok` 加一条 blocking 的 `decode_failed`（FILE-4，
  范围是坏的那一路相机），解码器原话在 `details.undecodable`；网络、超时等照旧是执行出错。
- 读数据集要先在前几条 episode 上抽样认语义；抽到读不了的文件时逐条读、跳过坏的那条，不再让整段失败（`pipeline/rows.py`）。
- 文字输出（不带 `--json`）在模块行尾写 `skipped: integrity_rejected 6`。

三个行为开关，默认全关：

| 开关 | 含义 |
|---|---|
| `--concurrency N` | CPU 模块：同时处理的 episode 数。VLM 模块：并行度 N，各闸门由它推导（04 篇 §2.2） |
| `--retry N` | 外层逻辑重试：一次 VLM 调用最终失败后，隔 1s / 2s / 4s 再来，最多 N 次（04 篇 §6） |
| `--hedge` | 启用超时对冲补发。不开时一次请求就是一次请求 |

`--plan-stage <file>` 传入 planner 为这一档生成的 VLM 请求合并分组；不传就逐模块单发。

`--vlm-reasoning-effort <档位>`：给了才在每个请求里带 `reasoning_effort`，
不给什么都不发，对账固定不给。档位是否在模型的有效档位内，由 Daemon 建任务时校验（03 篇）。

**改了标的条目怎么重判**（D39、D71）：有已应用的人工改标、又没有人工成败结论的 episode，`task_success` 用新标注重判，
和首轮一样每条一次请求；记录的 `details` 写明 `task_desc`（新标注）与 `task_desc_source`（`人工改标`）。

没带 `--source-manifest` 时，读到才发现缺源文件的 episode 同样剔除（D40）：不写结果行，
列在输出的 `skipped_missing_source` 里；带了清单的，清单的 `skipped_episodes` 已经把它们排除在外。
数据完整性从不剔除：缺文件的条它判废（`data_missing` / `video_missing`）。这次调用跑完整性、运行目录的 `run.json` 写着
`missing_sources: judged`、或完整性已有这条的记录时，带清单也不排除它（D97）；完整性有记录的条也不算进「跳过」。

### 3.6 `curation aggregate` — 聚合判决

```bash
curation aggregate --run-dir <dir> --revision N --json
```

纯计算，秒级，**每次全量重算**（漏斗的 `funnel` 阶段随 D90 删掉，2026-10-12 起 `--phase` 参数也删了）。

判决是策略判决（设计 17 §4，D58）：模块只报发现，策略表给每条发现定级 blocking / review / info。有 blocking 发现就拒，`reasons` 列出全部
blocking 发现；否则任一已勾选模块出错或没有记录就 held（待补跑）；否则通过，review 级发现按各自的裁决线进 `review.json`。默认策略复刻硬门
（P18），没有软分拒绝；预设 `report_only` 只报不拒（数据完整性的致命问题与各模块的 `decode_failed` 仍判废，策略版本 3）。策略取 `run.json` 的 `policy`（开跑时冻结，连同各模块细码的
缺省级别），每个结果版本写一份 `policy.json`。

写进 `<run-dir>/revisions/r<NNNN>/`：

| 文件 | 内容 |
|---|---|
| `passed.json` / `reject.json` / `held.json` | 三份互斥，除缺源文件被剔除的条目（D40）外完备；每条带 `reasons`（`kind` 是 `finding`、`human` 或 `execution_error`，带 `code`、`item`）和 `findings`（这一条的全部发现与它在这一版的级别，`index` 指向模块记录里的那条，人的结论带它的一句话） |
| `review.json` | 独立的复核视图，不和三份清单并列：多数在 `passed` 里（保守放行、等人确认），也可以在 `reject` 里（被拒复议的候选：blocking 发现全部可复议的拒绝，D42）。已被拒绝的条目不再问成败；`held` 里的条目在补跑出结论之前不进来 |
| `verdicts.jsonl` | 机器判决行：`{episode_index, verdict, blocking, review, info_count, error_modules, reason}`，只看检查本身 |
| `keep.txt` | 交付的条：机器判决加已应用的人工裁决（弃用的、人工判失败的移出，复议恢复的、对拒绝条目人工判成功的加入） |
| `policy.json` | 这一版用的策略 |

人的结论落在它回答的发现上，复议按发现（全部 blocking 发现可复议才受理）；重复检测只报告（D87），没有要留哪条的问题。
任何一个已勾选的模块对某条 episode 出错，这一条进 `held`，既不算通过也不算拒绝，见 06 篇 §3。例外（D35）：别的模块的 blocking 发现已经足以拒绝它，
就直接拒，出错的模块照样记在判决行的 `error_modules` 里。细化见设计 17 §7「F12.3 落地时的细化」「F12.5 落地时的细化」。

### 3.7 `curation report` — 生成报告

```bash
curation report --run-dir <dir> --revision N [--format md,json] --json
```

产出 `report.md` + `report.json` + 性能剖析，写进 `--revision` 指定的结果版本目录
`<run-dir>/revisions/r<NNNN>/`（`aggregate` 的清单也写在这里），不覆盖旧版本。
目录里最后写 `commit.json`，列明这一版用了每个模块的哪些 part、应用了哪些裁决。
哪个版本生效由 Daemon 在它完整上传并核验之后切换（D25）。报告结构见 06 篇。
明细表一律写成 Parquet（按 episode 下标排序），供报告页分页读取，见 03 篇 §6。
延迟明细是追加式的，子任务跑过之后重新生成，性能剖析自然包含历次调用。

### 3.8 `curation adjudicate-apply` — 执行人工裁决

```bash
curation adjudicate-apply --run-dir <dir> --decisions decisions.json --json
```

把裁决落到判决上，**不调模型**。
`decisions.json` 由 Daemon 从库里导出，只含**本任务**尚未执行的裁决；裁决不跨任务（D32）。三条裁决线的优先级规则原样保留
（「整条弃用」压过成败裁决等，见 06 篇 §5）。输出里带两份名单，交给后续步骤：

```jsonc
{"applied": 14, "skipped_already_applied": 0,
 "rerun_task_success": [17, 29],     // 改了标、且没有人工成败结论的条目，要按新标注重跑
 "label_changes": [{"episode_index": 17, "new_label": "..."}]}
```

`decisions.json` 是 2.0：只有 `schema_version` 和 `decisions`。D90 起没有 `relabel_rerun`：改标的条用新标注重判，和首轮一样每条一次请求（D71）。

Daemon 据此接着调 `check --modules task_success --episodes 17,29` →
`aggregate` → `report`。

### 3.9 `curation verify` — 交付核验

```bash
curation verify --run-dir <dir> --output tos://... --json
```

逐个回读交付目录里的关键文件：存在、大小对、能解析（JSON / parquet 头 / mp4 的 moov / JPEG 魔数）。
关键文件就是运行目录里除在产物（`logs/`、`inflight.json`、隐藏与临时文件）之外的全部文件：
结果版本、报告与明细、证据帧与曲线图。
搬 v1 的 `_verify_delivery_visible`：写成功不等于读得到，读回来全零的文件 v1 见过六次。
核验通过才写 `_COMPLETE`。

### 3.10 客户端命令：`curation task …`

给 Agent 和脚本用。只做一件事：调 Daemon 的 REST API，把响应原样（`--json`）或渲染后打出来。
响应里带 `links`，见 §6。连接信息只从参数或环境变量来：`CURATOR_URL`、`CURATOR_USER`、`CURATOR_PASSWORD`。

| 命令 | 对应接口 |
|---|---|
| `curation task create --file task.json [--wait]` | `POST /api/v1/tasks` |
| `curation task list [--state …] [--page N]` | `GET /api/v1/tasks` |
| `curation task get <id>` | `GET /api/v1/tasks/{id}` |
| `curation task wait <id> [--timeout S]` | 轮询到终态，输出最终的任务 JSON |
| `curation task start\|pause\|resume\|stop <id>` | `POST /api/v1/tasks/{id}/actions/{action}` |
| `curation task retry <id> [--modules a,b]` | `POST /api/v1/tasks/{id}/retry`（只补跑出错的 episode） |
| `curation task continue <id>` | `POST /api/v1/tasks/{id}/continue`（stopped / failed 续跑） |
| `curation task report <id>` | `GET /api/v1/tasks/{id}/report` |
| `curation task adjudication <id>` | 待裁决条数 + 裁决页链接；裁决本身要人看视频，只能在网页上做 |

### 3.12 运维命令（D89）

`ls`、`prune`、`fetch` 是给人在终端里用的：输出中文文字、不进 C2、Daemon 不调用；退出码 0 / 1（远端错误）/ 2（输入错误）。
`curation --help` 里它们列在「运维」一组。

| 命令 | 作用 |
|---|---|
| `curation ls <地址>` | 列本地目录或 `tos://桶/前缀` 下一层（先目录后文件，带大小） |
| `curation prune <交付目录> [--keep-latest N] [--yes]` | 列一份交付目录下的历次跑批（`<run_id>/`：时间、最新结果版本的条数、占用），给了 `--keep-latest N --yes` 才删更早的；`latest` 指的那次永远留 |
| `curation fetch --source --ref` | 从内网 HuggingFace 缓存桶把公开数据集拉进本站数据集根，逐文件校验 |

v1 的 `run`、`rejudge`、`review-page`、`backends`、`public` 和 `ui` 都已删除（设计 27），它们的职责由 Daemon 编排原子命令承担；
v1 的代码在 `release_v1` 分支。

## 4. 退出码与信号

| 码 | 含义 | Daemon 的处理 |
|---|---|---|
| 0 | 成功（含部分 episode 出错） | 正常 |
| 1 | 命令自身的意外错误（`internal`，调用栈在 stderr） | 任务 `failed`，属于 bug，告警 |
| 2 | 参数错误 | 任务 `failed`，属于 bug，告警 |
| 3 | 读不到：输入数据集（`input_unreachable`）、交付目录（`output_unreachable`）、Daemon（`daemon_unreachable`，只有 `curation task …`） | 任务 `failed`，原因回显给用户 |
| 4 | 模块整体失败（VLM 端点不可达等） | 该模块 `failed`，其余模块继续 |
| 5 | 收到 SIGTERM，已收尾退出 | 按 Daemon 自己的意图置 `paused` / `stopped` |
| 6 | 源数据与 `--source-manifest` 对不上 | 任务（或子任务）`failed`，原因 `source_changed`，提示重新预检后另建任务 |
| 7 | `curation task …`：Daemon 回了错误（`rejected`），它的错误体原样放在 `details.rest_error` | 不涉及（给 Agent 和脚本看） |
| 8 | `curation task wait` 等到超时任务仍未结束（`wait_timeout`），最后一次的状态在 `details.task` | 不涉及 |
| 130 | 收到 SIGINT，已中止 | 同上 |

信号协议：

| 信号 | CLI 的行为 | Daemon 什么时候发 |
|---|---|---|
| SIGTERM | 不再开始新的 episode，在飞的跑完、结果落盘，退出码 5 | 暂停、优雅停机 |
| SIGINT | 取消在飞的请求，已完成的结果落盘，立即退出，退出码 130 | 停止 |
| SIGKILL | — | SIGTERM 后 90s、SIGINT 后 10s 仍未退出 |

结果是逐条追加的，整行写完再 flush + fsync，所以被 SIGKILL 也不会留下半行；没写完的那条恢复时重跑。

## 5. 进度与日志协议

Daemon 要把进度透传给前端 SSE，所以 CLI 的进度必须是结构化的：

- **stdout**：只有 `--json` 的最终结果，一次性输出，不混任何日志。
- **stderr**：逐行 JSON（JSON Lines），每行一个事件：

```jsonc
{"ts": 1758300000123, "level": "info", "kind": "progress",
 "stage": "check:visual_quality", "done": 12, "total": 640, "eta_s": 480}
{"ts": 1758300000456, "level": "warn", "kind": "log",
 "msg": "episode 18 too short (0.5s); marked as fragment"}
{"ts": 1758300001000, "kind": "usage", "model": "doubao-seed-2-0-pro-260215",
 "module": "task_success", "call_kind": "probe", "requests": 1,
 "prompt_tokens": 1820, "completion_tokens": 64, "reasoning_tokens": 40, "cached_tokens": 1536}
{"ts": 1758300002000, "kind": "throttle", "backend": "ark-prod",
 "limit": 32, "reason": "429 rate 12% in last 30s"}
```

四种 `kind`：`progress`（驱动进度条）、`log`（驱动日志流）、`usage`（驱动 Token 统计）、
`throttle`（VLM 客户端自适应降并发时上报，见 04 篇 §7）。
人类可读模式下 stderr 打印的是渲染过的文本行，`--json` 模式下才是 JSON Lines。

这个协议是 CLI 与 Daemon 之间唯一的运行时耦合点，**属于第一批冻结接口**（见 11 篇）。

## 6. 给 Agent 用的交互链接

需求要求：API/CLI 返回的 JSON 里，凡是需要人工介入的地方，给一个能一键打开的网页链接。

链接指向的是「任务」的页面，而任务只存在于 Daemon。所以 **`links` 只出现在 REST 响应和客户端命令
（`curation task …`）的输出里，原子命令不产出 `links`**（D22）。

```jsonc
{
  "links": [
    {"rel": "report",       "title": "Open QA report",
     "url": "https://<host>/curation/tasks/task-kqzmrtbwe/report"},
    {"rel": "adjudication", "title": "3 episodes need human judgement",
     "url": "https://<host>/curation/tasks/task-kqzmrtbwe/adjudication?status=pending"}
  ]
}
```

URL = `CURATOR_PUBLIC_BASE_URL` + 挂载前缀 + 路由。没配 `CURATOR_PUBLIC_BASE_URL` 时退回相对路径，
并带 `"absolute": false`，不拼一个错的 host 出去。
