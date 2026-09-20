# 02 CLI 契约

## 1. 设计原则

1. **一条命令一件事**。命令之间通过文件系统/对象存储传递中间产物，不在一条命令里串流程。
   组合由 Daemon 或用户脚本完成。
2. **默认输出给人看（英文），`--json` 给机器看**。两种输出的信息量可以不同，
   但 `--json` 的 schema 是冻结契约，改动需要走版本号。
3. **零隐式状态**。不读用户 home 下的配置、不写隐藏文件；一切从参数和显式指定的配置文件来。
4. **不重试、不并发**，除非显式要求。默认参数下就是一次直来直去的执行；
   `--concurrency` / `--retry` 是可选行为开关。这条是需求的硬要求。

## 2. 全局参数

| 参数 | 说明 |
|---|---|
| `--json` | 机器可读输出到 stdout，人类日志一律走 stderr |
| `--config <path>` | 流水线 YAML，叠加在出厂默认之上 |
| `--set k=v` | 单值覆盖，可重复 |
| `--log-level` | `error`/`warn`/`info`/`debug`，默认 `info` |
| `--region` / `--input-region` / `--output-region` | TOS 地区 |

**凭证只走环境变量**，不进 argv：`TOS_ACCESS_KEY` / `TOS_SECRET_KEY` / `ARK_API_KEY` /
`CURATION_VLM_API_KEY_ENV`。理由：argv 在 `ps` 里全局可见，容器里同样。

## 3. 命令清单

### 3.1 `curation preflight` — 预检

```bash
curation preflight --input tos://bucket/datasets/umi_640_notask \
                   --input-region cn-beijing --json
```

读数据集 metadata，**不读样本数据**，秒级返回。输出：

```jsonc
{
  "schema_version": "1.0",
  "format": {
    "kind": "lerobot",            // lerobot | mcap | lancedb | rrd | unknown
    "version": "v2",              // v2 | v3 | null
    "supported": true,            // false ⇒ 全部模块标灰
    "detail": "LeRobot v2, 640 episodes, 3 cameras"
  },
  "dataset": {
    "episode_count": 640,
    "episode_ids": ["000000", "000001"],   // 超过 2000 条时截断并置 truncated
    "truncated": false,
    "cameras": ["exterior_1", "exterior_2", "wrist"],
    "fps": 30.0,
    "robot_type": "umi",          // 读不到为 null
    "total_frames": 512000
  },
  "modules": [
    {"id": "timestamp_check", "availability": "available"},
    {"id": "kinematic_limits", "availability": "needs_input",
     "reason": "robot_type not found in info.json; pick a model or skip this module",
     "input_hint": {"field": "embodiment_id", "options": ["agibot", "aloha", "franka", "..."]}},
    {"id": "task_success", "availability": "available"}
  ],
  "warnings": ["episodes_stats.json missing; per-episode stats will be recomputed"]
}
```

三态 `availability` 的判定规则见 `05-modules-and-preflight.md` §3。

### 3.2 `curation plan` — 并发与合并计划

```bash
curation plan --preflight preflight.json --modules timestamp_check,visual_quality,task_success \
              --episodes @episodes.txt --cpu-budget 32 --vlm-concurrency 32 --json
```

纯计算，不碰网络。输出执行计划（分档、每档并发度、VLM 请求合并分组）。
Daemon 内部直接调用同一个 planner 库函数；CLI 形态是为了可调试、可复现。
计划 schema 见 `04-concurrency-and-vlm-merge.md` §3。

### 3.3 `curation decode` — 抽帧

```bash
curation decode --input tos://... --episodes 0-99 --run-dir <dir> \
                --interval-s 0.5 --max-side 448 --concurrency 8 --json
```

把抽帧结果写进 `<run-dir>/frames/<episode_id>/`，供视觉质量、视频-动作同步、
VLM 三方复用。**解码是全流程最贵的一步，只做一次**（v1 里视觉质量与同步已经共用一次解码，
v2 把它提升为显式的独立 stage）。

输出：每条 episode 的帧数、耗时、失败原因。

### 3.4 `curation check` — 跑一个模块

```bash
curation check --module visual_quality --input tos://... --run-dir <dir> \
               --episodes @survivors.txt [--vlm-backend <name>] [--concurrency N] [--retry N] --json
```

**这是整个 CLI 最重要的命令。** 一次只跑一个模块，结果落
`<run-dir>/checks/<module>/results.jsonl`（一行一条 episode）。

```jsonc
// results.jsonl 每行
{"episode_id": "000034", "verdict": "pass",        // pass | fail | abstain | error
 "score": 0.82, "gate": "soft",
 "details": { /* 模块自定义，原样保留 v1 的字段名 */ },
 "evidence": ["details/frames/000034_t12.jpg"],
 "elapsed_s": 3.4, "error": null}
```

- `abstain`（弃权）是一等公民：证据不足不判废，进人工裁决队列 —— 这是 v1 的核心纪律，
  搬运时不得简化成二值。
- 单条 episode 出错不影响其他条：该行 `verdict=error`，命令整体仍返回 0；
  只有「整个模块无法执行」（如 VLM 端点完全不可达）才非零退出。

### 3.5 `curation aggregate` — 聚合判决

```bash
curation aggregate --run-dir <dir> --json
```

读 `checks/*/results.jsonl`，按硬门/软分规则合成三份清单：`passed.json` / `reject.json` /
`review.json`。纯计算，秒级，**每次全量重算**（子任务覆盖某模块后直接重跑它即可）。

判决规则原样搬运 v1 `pipeline/verdict.py`，不得重写。

### 3.6 `curation export` — 导出交付数据集

```bash
curation export --run-dir <dir> --input tos://... --output tos://... \
                [--incremental] [--concurrency N] --json
```

按 `passed.json` 导出 `lerobot_curated/`。`--incremental` 时对比上一次的产物清单，
只处理变动部分，详见 `06-delivery-and-report.md` §4。

### 3.7 `curation report` — 生成报告

```bash
curation report --run-dir <dir> [--format md,json] --json
```

产出 `report.md` + `report.json` + 性能剖析。报告结构见 06 篇。

### 3.8 `curation adjudicate-apply` — 执行人工裁决

```bash
curation adjudicate-apply --run-dir <dir> --decisions decisions.json --input tos://... --json
```

v1 `rejudge` 的原子化版本：只应用裁决、更新判决清单，**不导出数据集**（导出是 `export` 的事）。
三条裁决线的语义原样保留。

### 3.9 辅助命令

| 命令 | 作用 | 来源 |
|---|---|---|
| `curation datasets list` | 列数据集（私有 TOS 前缀 / HF 镜像） | 搬 `public.py` + 目录扫描 |
| `curation fetch` | 从 HF 镜像拉公开数据集到自己的桶 | 原样搬运 |
| `curation backends probe` | VLM 端点探活 + 模型列表 | 搬 `backends` + 新增拉模型 |
| `curation creds verify` | 校验 TOS/方舟凭证可用性 | 新增 |
| `curation table slice` | 对大明细 CSV 做服务端切片分页 | 新增，供报告页 |
| `curation prune` | 清理旧批次（不碰裁决记录） | 原样搬运 |

**删除**：`curation ui`（Gradio 整体退役）、内嵌终端相关一切。

## 4. 退出码

| 码 | 含义 | Daemon 的处理 |
|---|---|---|
| 0 | 成功（含部分 episode 出错） | 正常 |
| 2 | 参数错误 | 任务 `failed`，属于 bug，告警 |
| 3 | 输入不可达（数据集读不了、凭证失效） | 任务 `failed`，原因回显给用户 |
| 4 | 模块整体失败（VLM 端点不可达等） | 该模块 `failed`，其余模块继续 |
| 5 | 被信号中断（停止/暂停） | 按用户意图置 `stopped` / `paused` |
| 130 | SIGINT | 同上 |

## 5. 进度与日志协议

Daemon 要把进度透传给前端 SSE，所以 CLI 的进度必须是结构化的：

- **stdout**：只有 `--json` 的最终结果，一次性输出，不混任何日志。
- **stderr**：逐行 JSON（JSON Lines），每行一个事件：

```jsonc
{"ts": 1758300000123, "level": "info", "kind": "progress",
 "stage": "check:visual_quality", "done": 12, "total": 640, "eta_s": 480}
{"ts": 1758300000456, "level": "warn", "kind": "log",
 "msg": "episode 000018 too short (0.5s); marked as fragment"}
{"ts": 1758300001000, "kind": "usage", "model": "doubao-seed-2-0-pro",
 "call_kind": "endstate", "prompt_tokens": 1820, "completion_tokens": 64, "requests": 1}
```

三种 `kind`：`progress`（驱动进度条）、`log`（驱动日志流）、`usage`（驱动 Token 统计）。
人类可读模式下 stderr 打印的是渲染过的文本行，`--json` 模式下才是 JSON Lines。

这个协议是 CLI 与 Daemon 之间唯一的运行时耦合点，**属于第一批冻结接口**（见 11 篇）。

## 6. 给 Agent 用的交互链接

需求要求：API/CLI 返回的 JSON 里，凡是需要人工介入的地方，给一个能一键打开的网页链接。

约定：任何命令的 `--json` 输出，只要产生了「需要人看」的东西，就带一个 `links` 字段：

```jsonc
{
  "links": [
    {"rel": "report",      "title": "Open QA report",
     "url": "https://<host>/tasks/task_01H.../report"},
    {"rel": "adjudication","title": "3 episodes need human judgement",
     "url": "https://<host>/tasks/task_01H.../adjudication?filter=pending"}
  ]
}
```

host 从 `CURATOR_PUBLIC_BASE_URL` 环境变量取；未配置时 `links` 为空数组而不是拼一个错的 URL。
