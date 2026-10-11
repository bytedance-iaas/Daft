# planner：执行计划与 VLM 请求合并框架（W6 / F2.4）

Daemon 与 `curation plan` 共用的纯计算库（设计 02 §3.2），不联网。
对应设计 04 篇全篇、05 篇 §7、01 篇 §2.6、10 篇 §3.4；决策 D5、D18、D23、D31，取值 P1、P2、P4。

`check` 已经用上其中的闸门、外层重试和用量两本账；合并执行器和自适应降并发还没接（见文末「接入现状」）。现有模块都不声明
`merge_units`，计划里恒为 `"merge": {"strategy": "none"}`（D23），线上不会发生任何合并。

## 文件

| 文件 | 内容 |
|---|---|
| `limits.py` | 上限取交集（D31）：CPU 并发 = min(核数 − 2（至少 1），任务上限)，是一个任务最多用多少，核本身由 Daemon 的全局 CPU 池在任务之间分（P4、D54）；VLM 并行度 N = min(任务、模型、后端、站点上限，模型和后端都没配时再加站点默认或 64)，多任务同跑时按任务数均分（P1）；计划里记下是哪一层卡住的（`bound_by`） |
| `gates.py` | 一个 N 推导 VLM 段的两把闸门（04 §2.2）：`probe`（在飞的模型请求）是 N，`episode`（在飞的 episode）跟着 `probe` 走，一条一次请求（D71）；站点可逐把覆盖，按 N 等比缩放。v1 的 `endstate`、`arbitration`、`guard_caption`、`caption`、`llm`、`audit` 随 v1 删掉（D89），站点配置 `vlm.gates` 里还写着的读到后忽略并告警（`limits.py` 的 `RETIRED_GATES`） |
| `plan.py` | `build_plan()`：两块的段、聚合档、重复检测的 `duplicate` 段（逐条段，并发恒为 1，D87）、合并提案、估算（没有任务标注的条目不判成败，只算一次画面缺陷请求，D72、D73）；输出符合 `docs/contracts/cli/plan.schema.json` |
| `estimates.py` | 估算用的常数全部来自 v1 的实测与出厂配置，逐条注明出处 |
| `merge.py` | `FramePolicy`、`MergeUnit`、`MergeGroup`、`MergeLimits`、`none` 与 `per_episode_multi_module` 两个策略、合并请求的拼装与按 key 拆回、`vlm.merge.enabled` 开关 |
| `executor.py` | `MergeExecutor`：注入 `send(request)`，按组发送、拆回交给各模块自己的解析函数、单项解析失败只降级那一项、超限拆包、回执（`check --json` 的 `merge` 块）；`chat_payload()` 把请求拼成 OpenAI 兼容的请求体（只有 model / temperature / max_tokens / messages） |
| `usage.py` | 解析 OpenAI 兼容的 `usage`；实际调用账与分摊账两本账，分摊账逐项等于实际账（最大余数法，整数精确）；拿不到 usage 的只计数不估算；产出 C3 的 `usage` 行（增量） |
| `retry.py` | 外层 `vlm_retry`（04 §6）：只重试超时、连接错、5xx、429，间隔 1/2/4 秒，429 至少等 `Retry-After` |
| `throttle.py` | 自适应降并发（04 §7）：30 秒窗口内 429/5xx 占比超过阈值，各闸门一起减半，干净的窗口逐步加回；每次调整产出 C3 的 `throttle` 行；`ResizableGate` 是可在运行中改容量的信号量 |
| `consistency.py` | 合并与单发的判决一致性对账（10 §3.4），门槛 98% |

测试在 `backend/tests/planner/`。两个「抽 8 帧、问一次」的示例模块和确定性的假模型只存在于测试里（`examples.py`）。

## 给调用方

**Daemon（W4/W5）**：任务启动时调用一次，结果存为 `plan.json`，按它调度。

```python
from curation.planner import PlanLimits, SiteConfig, build_plan

plan = build_plan(preflight_json, task["modules"], episode_indices,
                  PlanLimits(cpu_concurrency=params_limits.get("cpu_concurrency"),
                             vlm_parallelism=params_limits.get("vlm_parallelism"),
                             model_parallelism=model.parallelism, backend_parallelism=backend.parallelism,
                             cpu_cores=容器的 CPU 配额（读不到就不传，默认 os.cpu_count()）,
                             running_tasks=启动时正在运行的任务数),
                  SiteConfig.from_mapping(site_yaml_的_concurrency_与_vlm_段),   # CPU 没有站点设置（D54）
                  unlabeled_episodes=没有任务标注的条目下标（知道就传，不判成败的条数与估算就是精确的）)
```

**CLI（W3）**：`curation plan` 调同一个函数；建议 `--cpu-cores` → `cpu_cores`，`--vlm-parallelism` → `model_parallelism`
（CLI 场景下它就是「所用后端的并行度」），任务上限另给参数。`check` 这边：

- `--concurrency N`（VLM 段）→ `derive_gates(N)`；带了 `--plan-stage` 时以那一段的 `gates` 为准，两样都没有时每把闸门都是 1
  （`cli/runctx.py` 的 `vlm_gates`）；
- `--retry N` → `RetryPolicy(max_retries=N)`，默认 0（CLI 默认不重试）；
- `--plan-stage stage.json` → `strategy_for_stage(stage)`；合并执行器接上之后交给 `MergeExecutor`，每条 episode 的回执累加进
  `MergeReceipts`，最终写进 `check --json` 的 `modules.<id>.merge`；
- usage：`UsageLedger(emit=写一行 JSON 到 stderr)`，每个请求产出一条实际账和对应的分摊账，都是增量。

## 手动验证步骤

在 `backend/` 下执行（Python 用仓库根目录的 `.venv`，下同）。

**1. 测试**（planner 约 4 秒，契约约 20 秒）：

```bash
../.venv/bin/python -m pytest -q tests/planner
../.venv/bin/python -m pytest -q tests/contracts && ../.venv/bin/python -m curation.contracts check
```

应全部通过，`check` 无输出。

**2. 看一份计划**：用契约里的预检示例（200 条、88 条无标注、3 个机位）生成全模块计划并按契约校验。

```bash
../.venv/bin/python - <<'EOF'
import json
from curation.contracts import schemas
from curation.planner import build_plan
pf = json.loads((schemas.contracts_dir() / "examples/preflight.json").read_text())["valid"][0]
plan = build_plan(pf, ["timestamp_check", "kinematic_limits", "motion_quality", "visual_quality",
                       "video_action_sync", "task_success", "duplicate_check"],
                  limits={"cpu_cores": 32}, validate=True)
for s in plan["stages"]:
    print(s["id"], s.get("episodes", ""), s.get("concurrency", ""), s.get("gates", ""), s.get("merge", ""))
print(plan["limits"])
print(plan["estimates"])
EOF
```

核对：五段依次是 numeric、frame、duplicate（CPU 块；重复检测，D87）、vlm（VLM 块）与收尾的 final；CPU 两段并发 30（32 核留 2 核）；
duplicate 并发 1；vlm 段闸门 `{'episode': 64, 'probe': 64}`（一条一次请求，episode 跟着 probe 走，D71），
合并是 `{"strategy": "none", "groups": []}`；上限 `cpu_concurrency 30 / vlm_parallelism 64`，都由 planner 定；
估算 200 次请求（每条 1 次：有标注的判成败，88 条没标注的不判成败、只看画面缺陷，D72、D73）；notes 里依次说明运动学极限缺型号、
88 条没有任务标注、估算用的单次请求耗时。

**3. 上限取交集与闸门推导**：

```bash
../.venv/bin/python - <<'EOF'
from curation.planner import PlanLimits, SiteConfig, derive_gates, effective_cpu_concurrency, effective_vlm_parallelism
site = SiteConfig(vlm_parallelism_max=128)
for kw in ({}, {"model_parallelism": 100}, {"model_parallelism": 100, "vlm_parallelism": 16},
           {"model_parallelism": 64, "running_tasks": 2}, {"vlm_parallelism": 128}):
    n = effective_vlm_parallelism(PlanLimits(**kw), site)
    print(kw, n.to_json(), derive_gates(n.value))
print(effective_cpu_concurrency(PlanLimits(cpu_cores=32)).to_json(),
      effective_cpu_concurrency(PlanLimits(cpu_cores=32, cpu_concurrency=2)).to_json())
EOF
```

核对：不给任何上限时 N=64（`planner`）；模型 100 → 100（`model`）；再给任务上限 16 → 16（`task`）；两个任务同跑 → 32
（`running_tasks`）；任务上限 128 抬不高 N，仍是 64。每一行的闸门都是 `episode = probe = N`。
CPU：32 核默认 30（`planner`，留 2 核给 Daemon），任务上限 2 → 2（`task`）。

**4. 站点逐把覆盖闸门，v1 的闸门名被忽略**：

```bash
../.venv/bin/python - <<'EOF'
from curation.planner import SiteConfig, derive_gates, retired_site_keys
raw = {"concurrency": {"vlmParallelism": 64}, "vlm": {"gates": {"probe": 48, "arbitration": 16, "caption": 8}}}
site = SiteConfig.from_mapping(raw)
print(site.gate_overrides, retired_site_keys(raw))
for n in (64, 32, 8):
    print(n, derive_gates(n, site.gate_overrides, reference=site.reference_parallelism))
print(derive_gates(64, {"probe": 48, "episode": 16}))
EOF
```

核对：覆盖只剩 `{'probe': 48}`，`arbitration`、`caption` 列在要告警的旧键里（Daemon 与 `curation plan` 读站点配置时打这条告警）；
覆盖值按 N 等比缩放，N 为 64、32、8 时 `probe` 是 48、24、6，`episode` 跟着它；单独覆盖了 `episode` 的，`episode` 不再跟着 `probe`（16 与 48）。

**5. 合并与单发一致性对账**（示例模块、64 条，途中故意制造 3 处单项解析失败和 1 条整包不是 JSON）：

```bash
../.venv/bin/python - <<'EOF'
import json
from curation.planner import run_merge_consistency
from tests.planner import examples as X
fake = X.FakeVlm(garble={(5, "table"), (17, "grasp")}, drop={(33, "table")}, not_json={48})
report, single, merged = run_merge_consistency(X.units_for(range(64)), fake, frames=X.frames, model="fake-vlm")
print(json.dumps(report.to_json()))
print(merged.receipts())
print("merged requests:", sum(r.call_kind == "merged" for r in fake.requests))
EOF
```

核对：`rate` 为 1.0、`passes` 为 true（门槛 0.98）；回执里 `fallback_units` 两个模块合计 5（只有出问题的那几项单发重问），
其余 123 项是 `merged_units`；合并请求 64 次，每条 episode 一次。

**6. 两本账**：

```bash
../.venv/bin/python - <<'EOF'
from curation.planner import MergeExecutor, UsageLedger
from tests.planner import examples as X
ledger = UsageLedger()
fake = X.FakeVlm(garble={(1, "grasp")}, no_usage=lambda r: r.episode_index == 2)
MergeExecutor(fake, frames=X.frames, model="fake-vlm", ledger=ledger).run(X.units_for(range(4)))
for ledger_name in ("actual", "attributed"):
    for row in ledger.rows(ledger_name):
        print(row)
print("任务总量（只算实际账）:", ledger.task_totals())
print("分摊账等于实际账:", ledger.balanced())
EOF
```

核对：实际账里合并请求是一行 `module=example_grasp+example_table, call_kind=merged`，降级单发的那次记在
`example_grasp / caption` 下；分摊账把合并请求拆给两个模块（token 按比例拆成整数；一次请求拆不开，按 prompt 占比
抽给其中一个模块，由请求本身决定，每次跑结果相同）；第 2 条没有 usage，只计进 `requests_unknown_usage`；
最后两行分别是实际账合计与 `True`。

**7. 一键关闭合并**：

```bash
../.venv/bin/python - <<'EOF'
from curation.planner import MergeExecutor, build_plan
from tests.planner import examples as X
pf = X.preflight(8)
on = build_plan(pf, ["example_grasp", "example_table"], registry=X.REGISTRY)
off = build_plan(pf, ["example_grasp", "example_table"], registry=X.REGISTRY,
                 site_config={"vlm": {"merge": {"enabled": False}}})
print(on["stages"][0]["merge"], off["stages"][0]["merge"])
fake = X.FakeVlm()
MergeExecutor(fake, frames=X.frames, enabled=False).run(X.units_for(range(8)))
print(len(fake.requests), {r.call_kind for r in fake.requests})
EOF
```

核对：打开时是 `per_episode_multi_module` 且分组是两个示例模块，关闭后是 `none`；执行器关闭时发 16 次、全部单发（`caption`）。

**8. 降并发事件**：

```bash
../.venv/bin/python - <<'EOF'
from curation.planner import AdaptiveThrottle, derive_gates
t = AdaptiveThrottle(64, derive_gates(64), backend="ark-prod", emit=print)
for i in range(20):
    t.record("rate_limited" if i % 4 == 0 else "ok")
EOF
```

应打印一条 `kind=throttle` 的事件：`limit` 32，`reason` 为 `429/5xx rate 25% in last 30s`，`gates` 为 `{'episode': 32, 'probe': 32}`，两把闸门都减半。

## 接入现状

这些旁路都不改判定逻辑。

| 能力 | 现状 |
|---|---|
| 闸门 | 已接。`check` 的 VLM 段取 `--plan-stage` 那一段的 `gates`，没有计划时用 `derive_gates(--concurrency)`，两样都没有时每把都是 1（`cli/runctx.py` 的 `vlm_gates`）。任务成败判定的请求在 `probe` 闸门下发（`adapters/video_vlm.py`），EEF 复核也用 `probe` 闸门（`cli/eef_check.py`） |
| 外层重试 | 已接。`--retry N` → `RetryPolicy(max_retries=N)`，包在每次模型调用外面，只重试超时、连接错、5xx、429（`pipeline/vlm_policy.py`） |
| usage 采集 | 已接。`vlm_client` 把发出的每个请求交给 `vlm_policy.UsageBooker`，按请求的标签（`probe`、`eef_review`）记到模块名下，写 C3 的 `usage` 行和运行目录的 `usage.jsonl`；拿不到 usage 的只计数 |
| 合并策略 | 只读取。`check` 读计划段的合并提议（`cli/check.py` 的 `_merge_strategy`），现有模块都不声明合并单元，策略总是 `none` |
| 合并执行 | 没接，等第一个声明 `merge_units` 的模块。每条 episode：`executor.run(merge_units_for(本段模块, ep, ctx))`；`send` 用 `chat_payload(request, model=..., image_url=...)` 拼请求体、发出后返回响应 JSON；`frames` 按 `FramePolicy` 解码一次 |
| 自适应降并发 | 没接。闸门在 `adapters/video_vlm.py` 和 `cli/eef_check.py` 里按容量新建；接的时候换成 `ResizableGate` 并 `AdaptiveThrottle.bind()`，每次调用的结局（`ok` 或失败原因）从 `vlm_policy.TransportPolicy.on_outcome` 交给 `throttle.record()`，事件写 stderr |

## 已知限制

- 单发请求的 `call_kind` 是 `probe`、`eef_review`，或新模块自己的种类（写成模块 id 的样子，C3 1.1）；`merged` 只给合并请求。
  示例模块单发时用的是 `caption` 这个名字，它只是一个合规的种类名，和 v1 的补描述无关。
- 实际账里合并请求的 `module` 记成参与模块排序后用 `+` 连接（如 `example_grasp+example_table`，C3 里有说明）。
  `requests` 只数拿到 usage 的请求，没拿到的只进 `requests_unknown_usage`。
- `check --json` 的 `merge.requests` 按模块计：一个合并请求在它携带的每个模块里各记一次；任务级请求总数以实际账为准。
- 合并请求还没有延迟行：执行器没接进 `check`，`vlm_latency.csv` 里也就没有 `merged` 这一类，要加时单独定。
- 新模块的请求按 `probe` 估算；真正接入时要定它在哪把闸门下发（EEF 复核用的是 `probe`）。
- 估算是建议值：假定所选条目都走完两块；数据完整性的解码测试开着时不计入（notes 里写明）。
- 10 篇 §3.4 要求示例模块走真实的方舟端点再跑一遍对账：`run_merge_consistency` 可以直接用，
  缺的是真实的 `send`（上表「合并执行」一行）和按 `FramePolicy` 解码的 `frames`，都属于接入步骤。
