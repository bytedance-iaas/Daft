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
   - **原子命令**（§3.1–3.10）在本地干活，不知道 Daemon 的存在，也没有「任务」这个概念；
   - **客户端命令**（§3.11，`curation task …`）只调 Daemon 的 REST API，给 Agent 和脚本用。

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
| VLM | `--vlm-api-key-env <变量名>` 指定从哪个变量读（v1 既有设计），默认 `ARK_API_KEY` |

输入和输出必须能用**两套不同的访问密钥**：源数据常常是别人账号下的只读桶，交付写进自己的桶。
HuggingFace 缓存桶匿名读，不需要输入密钥。

凡是读源数据的命令都接受 `--source-manifest <file>`（§3.3）：传了就逐个对象校验大小与 ETag，
对不上立即以退出码 6 结束。不传则不校验 —— 直接用 CLI 的人自己负责数据不变。

episode 一律用**整数下标**表达，语法沿用 v1：`34`、`10-20`、`3,10-12`，或 `@file`（每行一个下标）。
负数、倒序区间、跨度超过 100 万的区间直接拒绝。

## 3. 命令清单

### 3.1 `curation preflight` — 预检

```bash
curation preflight --input tos://bucket/datasets/my_dataset --input-region cn-beijing \
                   [--source tos|public|local] [--vlm-backend <name>] --json
```

只读 metadata（`meta/info.json`、任务表、文件清单），**不读样本数据**，秒级返回。输出：

```jsonc
{
  "schema_version": "1.0",
  "format": {
    "kind": "lerobot",            // lerobot | mcap | lancedb | rrd | unknown
    "version": "v2",              // v2 | v3 | null
    "supported": true,            // false ⇒ 全部模块不支持
    "detail": "LeRobot v2, 200 episodes, 3 cameras"
  },
  "validation": [],               // info.json 结构校验的错误；非空 ⇒ supported=false，原因照抄给用户
  "dataset": {
    "episode_count": 200,         // 下标连续，0 … count-1，不再返回完整清单
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
     "input_hint": {"field": "embodiment_id",
                    "options": ["agibot", "aloha", "franka", "google_robot", "pusht",
                                "so100", "so101", "ur5", "widowx"]}},
    {"id": "task_success", "availability": "available",
     "notes": ["88 episodes have no task text; the model will caption them first"]}
  ],
  "meta_fingerprint": "sha256:…",   // meta 文件的指纹；任务启动时再比一次，变了就要求重新预检
  "warnings": ["episodes_stats.json missing; per-episode stats will be recomputed"]
}
```

读到了型号但规格库不支持时，是另一种形态（以 umi 为例）：

```jsonc
{"id": "kinematic_limits", "availability": "unsupported",
 "reason": "robot_type 'umi_dual_handheld_gripper' is not in the embodiment registry"}
```

三态 `availability` 的判定规则见 `05-modules-and-preflight.md` §4。
没传 `--vlm-backend` 时，VLM 模块是 `needs_input`（`input_hint.field = "vlm"`），不是 `unsupported`。

### 3.2 `curation plan` — 并发与合并计划

```bash
curation plan --preflight preflight.json --modules timestamp_check,visual_quality,task_success \
              --episodes 0-199 --cpu-cores 32 --vlm-parallelism 64 --json
```

纯计算，不碰网络。输出执行计划（分档、每档并发度与各闸门、VLM 请求合并分组）。
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
之后的每条命令带上 `--source-manifest` 去读，就保证了一个任务从头到尾只认这一个版本的源数据（D27）。

### 3.4 `curation autolabel` — 给没有任务标注的条目补描述

```bash
curation autolabel --input tos://... --run-dir <dir> --episodes @unlabeled.txt \
                   --vlm-backend <name> [--concurrency N] [--retry N] [--hedge] --json
```

v1 的既有行为（`pipeline/run.py` 漏斗前的 caption 兜底）：没有任务标注的条目，VLM 无从判断任务成败，
所以先由模型看画面写一句任务描述。结果写 `<run-dir>/autolabel/captions.jsonl`：

```jsonc
{"episode_index": 17, "caption": "put the orange box into the bin", "source": "自产caption"}
```

下游三处用它：`task_success` 拿它当任务文本（**有原始标注就用标注，没有才用它**）；
`skill_profile` 直接复用，不再重打；`export` 把它写进交付数据集的任务文本，并标明来源。
抽帧与提示词原样搬运 `dataset_level/caption.py`。

### 3.5 `curation check` — 跑检查

```bash
curation check --modules visual_quality,video_action_sync --input tos://... --run-dir <dir> \
               --episodes @survivors.txt --part 0003 \
               [--resume] [--vlm-backend <name>] [--plan-stage stage.json] \
               [--concurrency N] [--retry N] [--hedge] --json
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
- 数据集级模块（`dedup`、`skill_profile`）不分批，一次调用吃整个 keep 集合；
  `skill_profile --incremental` 在已有技能体系上只处理变动的条目（搬 v1 的 `reassign` / `reprofile`）。

```jsonc
// parts/*.jsonl 每行
{"episode_index": 34, "verdict": "scored",        // pass | fail | abstain | scored | error
 "score": 0.82, "gate": "soft",
 "details": { /* 模块自定义，原样保留 v1 的字段名 */ },
 "evidence": ["details/evidence/ep000034/probe3_f0120.jpg"],
 "elapsed_s": 3.4, "error": null}
```

- 行格式由 W0 先行定稿：`docs/contracts/cli/result-record.schema.json`（对账工具导出 v1 结果用的是同一份）。
  `passed` / `score` 原样保留 v1 的三态与分数，`verdict` 由它们推出：硬门 `pass` / `fail`，无判无分为 `abstain`，打分项（视觉质量、运动质量这类不投票的）为 `scored`，执行出错为 `error`。
- `abstain`（弃权）是一等公民：证据不足不判废，进人工裁决队列 —— 这是 v1 的核心纪律，
  搬运时不得简化成二值。
- **`error` 和 `abstain` 严格区分**（D24）。`abstain` 是模型或算法正常给出的「判不了」（证据灰区、两问矛盾），
  维持 v1：照样交付，同时进人工复核。`error` 是执行层面的失败：解码失败、模型调用重试用尽、解析不了返回、内部异常。
  v1 把后者也记成弃权（理由以「VLM 调用/解析失败」开头）；v2 把它单列出来，因为处理方式不同：
  出错的这条**不再进入后面的档、暂不交付**，等「重试」补跑。
- **口径取宽**（D33）：判这一条的过程中，任何一次模型调用在全部重试之后仍失败、或任一机位解码失败，
  这一行就记 `error` —— 即使 v1 的降级逻辑还能给出结论（打分失败后复核救回、仲裁失败维持弃权、少一路机位照判）。
  `details` 照常保留算法给出的留痕，`error` 字段列出失败的调用与机位：

```jsonc
"error": {"kind": "execution",
          "incidents": [{"step": "probe", "call_kind": "probe", "cause": "timeout", "attempts": 4},
                        {"step": "decode", "camera": "wrist", "cause": "InvalidDataError"}]}
```

  这份清单由编排壳记录：每条 episode 单独包一层模型调用与解码的封装，失败时记一笔再原样抛出，
  A 类算法代码一行不动。模型正常给出的看不清、拿不准（如 caption 回 `unclear`）不是出错。
- 单条 episode 出错不影响其他条：该行 `verdict=error`，命令整体仍返回 0；
  只有「整个模块无法执行」（如 VLM 端点完全不可达）才非零退出。
- **退出码 0 不等于「全部成功」**。`--json` 的最终输出必须带逐状态计数和输入集合的指纹，Daemon 据此给模块定
  `succeeded` 还是 `completed_with_errors`，不靠退出码猜：

```jsonc
{"modules": {"task_success": {"part": "0001", "input_digest": "sha256:…",
   "episodes": {"total": 49, "pass": 38, "fail": 6, "abstain": 3, "error": 2},
   "error_episodes": [14, 31]}}}
```

三个行为开关，默认全关：

| 开关 | 含义 |
|---|---|
| `--concurrency N` | CPU 模块：同时处理的 episode 数。VLM 模块：并行度 N，各闸门由它推导（04 篇 §2.2） |
| `--retry N` | 外层逻辑重试：一次 VLM 调用最终失败后，隔 1s / 2s / 4s 再来，最多 N 次（04 篇 §6） |
| `--hedge` | 启用 v1 的超时对冲补发。不开时一次请求就是一次请求 |

`--plan-stage <file>` 传入 planner 为这一档生成的 VLM 请求合并分组；不传就逐模块单发。

### 3.6 `curation aggregate` — 聚合判决

```bash
curation aggregate --run-dir <dir> --phase funnel|final --json
```

纯计算，秒级，**每次全量重算**。分两个阶段，对应 v1 `run.py` 里的两段：

| 阶段 | 读 | 写 |
|---|---|---|
| `funnel` | 六项漏斗检查的结果 | `verdicts.jsonl`（每条 keep / drop、硬门失败项、软分、弃权项）和 `keep.txt` |
| `final` | 上一步 + `dedup`、`skill_profile` 的结果 + 已应用的人工裁决 | `passed.json` / `reject.json` / `review.json` / `held.json` |

判决规则原样搬运 v1 `pipeline/verdict.py`，不得重写：硬门 `passed=False` 才 drop；
硬门弃权（`passed=None`）只记入未决项，**不 drop**；软分加权均值低于阈值 drop。
`passed` / `reject` / `held` 三份互斥且完备；`review` 是独立的复核视图，不和它们并列 ——
里面的条目多数在 `passed` 里（保守放行、等人确认），也可以在 `reject` 里（被拒复议的候选）。
任何一个已勾选的模块对某条 episode 是 `error`，这一条进 `held`（待补跑），既不算通过也不算拒绝，见 06 篇 §3。

### 3.7 `curation export` — 导出交付数据集

```bash
curation export --run-dir <dir> --input tos://... --output tos://... \
                [--incremental] [--concurrency N] --json
```

按 `passed.json` 导出 `lerobot_curated/`，**含待裁决条目**（v1 的保守放行），不含 `held` 里待补跑的条目。
任务文本按来源写入：原始标注、自产 caption、人工改标，各带 `instruction_source`。
`--incremental` 时对比上一次的产物清单，只处理变动部分，详见 `06-delivery-and-report.md` §4。

### 3.8 `curation report` — 生成报告

```bash
curation report --run-dir <dir> [--format md,json] --json
```

产出 `report.md` + `report.json` + 性能剖析，写进 `--revision` 指定的结果版本目录
`<run-dir>/revisions/r<NNNN>/`（`aggregate --phase final` 的清单也写在这里），不覆盖旧版本。
目录里最后写 `commit.json`，列明这一版用了每个模块的哪些 part、应用了哪些裁决。
哪个版本生效由 Daemon 在它完整上传并核验之后切换（D25）。报告结构见 06 篇。
明细表一律写成 Parquet（按 episode 下标排序），供报告页分页读取，见 03 篇 §6。
延迟明细是追加式的（v1 的 rejudge 已如此），子任务跑过之后重新生成，性能剖析自然包含历次调用。

### 3.9 `curation adjudicate-apply` — 执行人工裁决

```bash
curation adjudicate-apply --run-dir <dir> --decisions decisions.json --json
```

v1 `rejudge` 拆出来的第一步：把裁决落到判决上，**不调模型、不导出数据集**。
`decisions.json` 由 Daemon 从库里导出，只含**本任务**尚未执行的裁决；裁决不跨任务（D32）。三条裁决线的优先级规则原样保留
（「整条弃用」压过成败裁决等，见 06 篇 §5）。输出里带两份名单，交给后续步骤：

```jsonc
{"applied": 14, "skipped_already_applied": 0,
 "rerun_task_success": [17, 29],     // 改了标、且没有人工成败结论的条目，要按新标注重跑
 "profile_resync": [14, 17, 29, 31]} // 技能画像里要重新归位或移除的条目
```

Daemon 据此接着调 `check --modules task_success --episodes 17,29` →
`aggregate` → `check --modules skill_profile --incremental` → `report`。

### 3.10 `curation verify` — 交付核验

```bash
curation verify --run-dir <dir> --output tos://... --json
```

逐个回读交付目录里的关键文件：存在、大小对、能解析（JSON / parquet 头 / mp4 的 moov / JPEG 魔数）。
搬 v1 的 `_verify_delivery_visible`：写成功不等于读得到，读回来全零的文件 v1 见过六次。
核验通过才写 `_COMPLETE`。

### 3.11 客户端命令：`curation task …`

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

### 3.12 辅助命令

| 命令 | 作用 | 来源 |
|---|---|---|
| `curation datasets list [--source public]` | 列数据集（私有 TOS 前缀 / HuggingFace 缓存桶） | 搬 v1 的 `public` + 目录扫描 |
| `curation datasets episodes` | 分页列 episode：下标、时长、任务文本、各机位视频位置 | 新增，供新建页的预览 |
| `curation fetch` | 从数据来源站把公开数据集拉到自己的桶（调外部 `oniond`，长任务） | 原样搬运，CLI-only |
| `curation backends probe` | VLM 端点探活 + 列模型（`GET /models` 能拉就拉；拉不出不算错，退回一次最小 chat 探活） | 搬 v1 的 `backends` |
| `curation creds verify` | 校验 TOS 访问密钥 / VLM API Key | 新增 |
| `curation clips` | 为裁决页生成视频片段（v3 源数据是多条拼接的 mp4 时需要） | 从 v1 `review-page` 拆出 |
| `curation ls` | 列一层目录或 `tos://` 前缀 | 原样搬运 |
| `curation prune` | 清理旧批次（默认只列不删，不碰裁决记录） | 原样搬运 |

**删除**：`curation ui`（Gradio 整体退役）、内嵌终端相关一切、`review-page` 的静态站生成部分。
v1 的 `run` / `rejudge` 这两个「一口气跑完」的命令不再保留，它们的职责由 Daemon 编排原子命令承担；
对账基线用 `release_v1` 分支上的原版来跑。

## 4. 退出码与信号

| 码 | 含义 | Daemon 的处理 |
|---|---|---|
| 0 | 成功（含部分 episode 出错） | 正常 |
| 2 | 参数错误 | 任务 `failed`，属于 bug，告警 |
| 3 | 输入不可达（数据集读不了、凭证失效） | 任务 `failed`，原因回显给用户 |
| 4 | 模块整体失败（VLM 端点不可达等） | 该模块 `failed`，其余模块继续 |
| 5 | 收到 SIGTERM，已收尾退出 | 按 Daemon 自己的意图置 `paused` / `stopped` |
| 6 | 源数据与 `--source-manifest` 对不上 | 任务（或子任务）`failed`，原因 `source_changed`，提示重新预检后另建任务 |
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
 "module": "task_success", "call_kind": "endstate", "requests": 1,
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
     "url": "https://<host>/curation/tasks/task_01H.../report"},
    {"rel": "adjudication", "title": "3 episodes need human judgement",
     "url": "https://<host>/curation/tasks/task_01H.../adjudication?status=pending"}
  ]
}
```

URL = `CURATOR_PUBLIC_BASE_URL` + 挂载前缀 + 路由。没配 `CURATOR_PUBLIC_BASE_URL` 时退回相对路径，
并带 `"absolute": false`，不拼一个错的 host 出去。
