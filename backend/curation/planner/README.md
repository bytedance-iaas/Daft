# planner：执行计划与 VLM 请求合并框架（W6 / F2.4）

Daemon 与 `curation plan` 共用的纯计算库（设计 02 §3.2）：不联网，不碰 v1 的编排代码。
对应设计 04 篇全篇、05 篇 §7、01 篇 §2.6、10 篇 §3.4；决策 D5、D18、D23、D31，取值 P1、P2、P4。

**本包只交付独立的库。** 把合并执行器、usage 采集、外层重试、自适应降并发接进 `check` 和
`adapters/vlm_client.py`，要等两个黄金基线存档之后再做（接入点见文末）。现有两个 VLM 模块不声明
`merge_units`，计划里恒为 `"merge": {"strategy": "none"}`（D23），线上不会发生任何合并。

## 文件

| 文件 | 内容 |
|---|---|
| `limits.py` | 上限取交集（D31）：CPU 并发 = min(站点默认或 `min(8, 核数/4)`，站点上限，任务上限)；VLM 并行度 N = min(任务、模型、后端、站点上限，模型和后端都没配时再加站点默认或 64)，多任务同跑时按任务数均分（P1）；计划里记下是哪一层卡住的（`bound_by`） |
| `gates.py` | 一个 N 推导八把闸门（04 §2.2）；endstate、arbitration、guard_caption 沿用 v1 在 `funnel.py` 里对 episode 闸门的耦合；站点可逐把覆盖，按 N 等比缩放；`v1_set_overrides()` 给出让 v1 代码用上这组闸门的 `--set` |
| `plan.py` | `build_plan()`：分档、幸存者链、硬门、autolabel 条件、两个聚合档、dedup（并发恒为 1）与技能画像、合并提案、估算；输出符合 `docs/contracts/cli/plan.schema.json` |
| `estimates.py` | 估算用的常数全部来自 v1 的实测与出厂配置，逐条注明出处 |
| `merge.py` | `FramePolicy`、`MergeUnit`、`MergeGroup`、`MergeLimits`、`none` 与 `per_episode_multi_module` 两个策略、合并请求的拼装与按 key 拆回、`vlm.merge.enabled` 开关 |
| `executor.py` | `MergeExecutor`：注入 `send(request)`，按组发送、拆回交给各模块自己的解析函数、单项解析失败只降级那一项、超限拆包、回执（`check --json` 的 `merge` 块） |
| `usage.py` | 解析 OpenAI 兼容的 `usage`；实际调用账与分摊账两本账，分摊账逐项等于实际账（最大余数法，整数精确）；拿不到 usage 的只计数不估算；产出 C3 的 `usage` 行（增量） |
| `retry.py` | 外层 `vlm_retry`（04 §6）：只重试超时、连接错、5xx、429，间隔 1/2/4 秒，429 至少等 `Retry-After` |
| `throttle.py` | 自适应降并发（04 §7）：30 秒窗口内 429/5xx 占比超过阈值，八把闸门一起减半，干净的窗口逐步加回；每次调整产出 C3 的 `throttle` 行；`ResizableGate` 是可在运行中改容量的信号量 |
| `consistency.py` | 合并与单发的判决一致性对账（10 §3.4），门槛 98% |

测试在 `backend/tests/planner/`。两个「抽 8 帧、问一次」的示例模块和确定性的假模型只存在于测试里（`examples.py`），
N=64 与 v1 出厂默认的对照直接从 v1 的 `default.yaml` 和代码里读（`v1_source.py`），不抄一份数字。

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
                  SiteConfig.from_mapping(values_yaml_的_concurrency_与_vlm_段),
                  unlabeled_episodes=没有任务标注的条目下标（知道就传，autolabel 档的去留就是精确的）)
```

**CLI（W3）**：`curation plan` 调同一个函数；建议 `--cpu-cores` → `cpu_cores`，`--vlm-parallelism` → `model_parallelism`
（CLI 场景下它就是「所用后端的并行度」），任务上限另给参数。`check` 这边：

- `--concurrency N`（VLM 模块）→ `derive_gates(N)`；拆 `funnel.py` 之前用 `v1_set_overrides(gates)` 经 v1 的 `--set` 生效；
- `--retry N` → `RetryPolicy(max_retries=N)`，默认 0（CLI 默认不重试）；
- `--plan-stage stage.json` → `strategy_for_stage(stage)`，交给 `MergeExecutor`；每条 episode 的回执累加进 `MergeReceipts`，
  最终写进 `check --json` 的 `modules.<id>.merge`；
- usage：`UsageLedger(emit=写一行 JSON 到 stderr)`，每个请求产出一条实际账和对应的分摊账，都是增量。

## 手动验证步骤

在 `backend/` 下执行（Python 用仓库根目录的 `.venv`，下同）。

**1. 测试**（约 3 秒）：

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
                       "video_action_sync", "task_success", "dedup", "skill_profile"],
                  limits={"cpu_cores": 32}, validate=True)
for s in plan["stages"]:
    print(s["id"], s.get("episodes", ""), s.get("concurrency", ""), s.get("gates", ""), s.get("merge", ""))
print(plan["limits"])
print(plan["estimates"])
EOF
```

核对：八档依次是 autolabel、numeric、frame、vlm、verdict、dedup、profile、final；frame 读 `survivors:numeric`，vlm 读
`survivors:frame`；CPU 两档并发 8；dedup 并发 1；vlm 档闸门 `episode 32 / probe 64 / endstate 64 / arbitration 32 /
guard_caption 32`，profile 档 `caption 32 / llm 16 / audit 16`；两个 VLM 档都是 `{"strategy": "none", "groups": []}`；
估算 3000 次请求（autolabel 88 + 成败判定 200×(8+2×3) + 画像 112）；notes 里说明运动学极限缺型号、哪些调用没计入。

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
（`running_tasks`）；任务上限 128 抬不高 N，仍是 64。CPU：32 核默认 8（`planner`），任务上限 2 → 2（`task`）。

**4. N=64 与 v1 出厂默认逐项对照**（v1 一侧从 `default.yaml` 和 `funnel.py` / `run.py` / `vlm_client.py` 的代码里现读）：

```bash
../.venv/bin/python - <<'EOF'
from curation.planner import derive_gates
from tests.planner import v1_source
v1, mine = v1_source.factory_gates(), derive_gates(64)
for g in mine:
    print(f"{g:14s} v1={v1[g]:3d} planner={mine[g]:3d}", "OK" if v1[g] == mine[g] else "DIFF")
EOF
```

应为八行 `OK`。

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

应打印一条 `kind=throttle` 的事件：`limit` 32，`reason` 为 `429/5xx rate 25% in last 30s`，八把闸门减半。

## 接入点（基线存档之后）

以下都不改判定逻辑，只加旁路；按 11 篇 §6 由 W3 与本包协调落地。行号以 `release_v1@45bdf9292` 搬来的文件为准。

| 能力 | 接在哪 | 怎么接 |
|---|---|---|
| usage 采集 | `vlm_client.py` 各工厂里 `raise_for_status()` 之后：`make_vlm_completion._post`（683 行）、`make_multiview_completion._post`（822）、`make_endstate_voter._ask`（924）、`_make_arb_post._post`（1080）、`make_llm_ask.llm_ask`（1209 成功分支），以及 `dataset_level/caption.py` 的 `captioner`（162） | `ledger.record_single(module=本进程所跑的模块, call_kind=tag, model=model, response=r.json())`；模块由进程决定（`check --modules task_success` 里仲裁用的 `llm` 标签也算 task_success，`autolabel` 进程记 `autolabel`） |
| 拿不到 usage 的请求 | `hedged_request` 的 `_attempt`（321–352 行，`latency_record` 那个 `finally`） | 除了胜出的那一发，每一发都 `ledger.record(..., usage=None)`；执行器侧对应 `VlmResponse.unknown_usage_requests` |
| 外层重试 | 各工厂里 `hedged_request(...)` 加 `raise_for_status()` 这一段；`llm_ask` 包在 4 次循环（1201–1224）外面 | `call_with_retry(..., RetryPolicy(max_retries=--retry))`；次数与挽回数 `RetryStats.to_json()` 进性能剖析 |
| 自适应降并发 | 闸门在各工厂内部新建（`vlm_client.py` 675、814、913、1069、1186 行，`caption.py` 148 行，`funnel.py` 121 行）；每一发的结局在 `_attempt` 里 | 工厂改为接受注入的闸门，换成 `ResizableGate` 并 `AdaptiveThrottle.bind()`；`_attempt` 按状态码把 `ok / rate_limited / server_error / timeout / connect_error` 交给 `throttle.record()`；事件写 stderr |
| 闸门 | `check --concurrency N` | 拆 `funnel.py` 之前：`v1_set_overrides(derive_gates(N))` 经 `config.apply_overrides` 生效（endstate、arbitration、guard_caption 由 v1 从 episode 闸门推出，单独覆盖要等拆分） |
| 合并执行 | `check` 拆出来的 VLM 档，episode 闸门之内 | 每条 episode：`executor.run(merge_units_for(本档模块, ep, ctx))`；`send` 适配 `hedged_request`，请求体只有 model / temperature / max_tokens / messages（v1 形态）；`frames` 按 `FramePolicy` 解码一次 |

## 已知限制

- C3 的 `usage.call_kind` 只有 v1 的五个标签加 `merged`：新模块单发时只能借用这五个之一（示例模块用 `caption`），
  `MergeUnit` 会拒绝别的值。第一个真实新模块接入时要扩这个枚举（契约变更）。
- 实际账里合并请求的 `module` 记成参与模块排序后用 `+` 连接（如 `example_grasp+example_table`）；C3 对 `module` 只写了
  「模块 id 或 autolabel」，这一条待写进契约说明。`requests` 只数拿到 usage 的请求，没拿到的只进 `requests_unknown_usage`。
- `check --json` 的 `merge.requests` 按模块计：一个合并请求在它携带的每个模块里各记一次；任务级请求总数以实际账为准。
- 合并请求的延迟行还没有标签：`vlm_latency.csv` 的五个标签是数据契约，加 `merged` 要单独定。
- 计划里的八把闸门是 v1 的调用点；新模块的请求暂按 `probe` 估算，真正接入时要定它归哪把闸门。
- 估算是建议值：假定所选条目全部过硬门；仲裁、判废护栏与画像的纯文本调用随数据变化，不计入。
