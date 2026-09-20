# 04 并发编排与 VLM 请求合并

> 这一篇是后端最核心的新增设计。需求原话：「CPU 和 VLM 是可以并发的，CPU 部分和 VLM 部分的
> 内部也是可以并发的」「尽量合并多个模块的检测在同一个 request 里」「框架上得做到足够灵活」。

## 1. 为什么这件事值得单独设计

v1 的实测数据（写在 `pipeline/default.yaml` 的注释里，是真实基准，不是估计）：

- VLM 段占端到端耗时 **约 97%**：10 条 DROID 关掉 VLM 只要 12 秒。
- 并发是唯一有效的提速手段：10 条 DROID，并发 1 → 447s，并发 8 → 84s，**5.3 倍**。
- 方舟 32×8=256 总在飞请求零 429 零排队，判定质量在三档并发下稳定。

所以：CPU 段怎么排都行（默认串行即可），**VLM 段的并发和合并决定产品体验和成本**。

## 2. 两档执行模型

```
        ┌──────────── CPU 档（并发度默认 = f(核数)）────────────┐
        │  timestamp_check → kinematic_limits → motion_quality │
        │  decode（抽帧，最贵的 I/O）                            │
        │  visual_quality → video_action_sync                  │
        └───────────────────────┬──────────────────────────────┘
                                │ 幸存者名单（硬门过滤）
        ┌───────────────────────▼──────────────────────────────┐
        │  VLM 档（并发度默认 32，按后端能力伸缩）                  │
        │  task_success（多相机取证 + 仲裁）                      │
        │  skill_profile（caption + 归纳 + 标注分歧）             │
        └──────────────────────────────────────────────────────┘
```

**档内并发，档间流水**：CPU 档处理完一批 episode 就把幸存者推给 VLM 档，
不必等 CPU 档全部跑完。这样 VLM 端点在整个任务期间保持喂满。

### 2.1 CPU 档并发度

目标节点 32 核 128G。默认值：

| 参数 | 默认 | 说明 |
|---|---|---|
| `cpu.check_concurrency` | `min(8, cores/4)` = 8 | 纯数值检查，内存友好 |
| `cpu.decode_concurrency` | `min(8, cores/4)` = 8 | 解码吃内存，每路 ~1.5GB 峰值 |
| `cpu.max_rss_guard` | 80% 物理内存 | 超过则暂停派发新 episode |

v1 默认是串行（并发 1），需求也说「CPU 部分相对耗时较少，这里并行度可以默认用 1」。
我们保留 **串行为 CLI 默认**（符合 CLI 的原子/无并发纪律），
由 Daemon 的 planner 在 32 核节点上显式开到 8 —— 这正是「CLI 默认不并发，并发作为可选参数」
和「Daemon 负责优化」两条要求的结合点。

### 2.2 VLM 档并发度

三类请求的在飞上限**各自独立**（v1 已实测的结论，照搬）：

| 请求种类 | 闸门 | v1 默认 |
|---|---|---|
| 打分探针 probe | 进程级信号量，所有 episode 共用 | `vlm.max_concurrency` |
| 终态复核 endstate | `vlm_episode_concurrency × 2` | 64 |
| 仲裁 arbitration | `vlm_episode_concurrency` | 32 |
| caption 画像 | `skill_profile.caption_concurrency` | 独立 |

⚠️ 闸门必须是**自建信号量**，不能用 daft 的 `max_concurrency` —— 后者对 async 行级 UDF
静默失效（v1 `funnel.py` 里踩过，注释有记录）。搬运时这条纪律不得丢。

## 3. 执行计划（Plan）

planner 的输出，也是 `curation plan --json` 的 schema：

```jsonc
{
  "schema_version": "1.0",
  "stages": [
    {"id": "cpu-numeric", "kind": "cpu", "concurrency": 8,
     "modules": ["timestamp_check", "kinematic_limits", "motion_quality"],
     "episodes": "all"},
    {"id": "decode", "kind": "cpu", "concurrency": 8, "episodes": "survivors:cpu-numeric"},
    {"id": "cpu-visual", "kind": "cpu", "concurrency": 8,
     "modules": ["visual_quality", "video_action_sync"], "episodes": "survivors:cpu-numeric"},
    {"id": "vlm", "kind": "vlm", "concurrency": 32,
     "modules": ["task_success", "skill_profile"],
     "episodes": "survivors:cpu-visual",
     "merge": {
       "strategy": "per_episode_multi_module",
       "groups": [
         {"episode": "000034", "modules": ["task_success", "skill_profile"],
          "frames": 8, "estimated_prompt_tokens": 2400}
       ]
     }}
  ],
  "estimates": {"vlm_requests": 1280, "wall_clock_s": 900, "notes": ["..."]}
}
```

计划是**数据**，不是代码路径：Daemon 生成它、存档它、按它调度。
调优时只改 planner，执行器一行不动。这是需求要的「框架灵活性」。

## 4. VLM 请求合并

### 4.1 一期只做：同 episode，多模块合并

同一条 episode 的同一批抽帧，本来要被 task_success、skill_profile 各问一遍。
合并成一个 request：一次传图，提示词里显式声明 N 个任务，要求输出 N 段结果。

```
System: You are given 8 frames from one robot demonstration episode.
        Complete ALL of the following tasks. Return STRICT JSON with exactly
        these top-level keys: "task_1", "task_2".

Task 1 (task_success): ...(原 prompt 原文)...
Task 2 (caption): ...(原 prompt 原文)...

Return format: {"task_1": {...}, "task_2": {...}}
```

**合并纪律（否则一致性会崩）**：

1. 每个子任务的 prompt 正文**逐字沿用 v1**，只在外层加编号与输出格式声明。
   prompt 是算法的一部分，改一个词就不是「照搬」了。
2. 输出解析**按 key 拆回**，拆出来的每段交给 v1 原本的解析函数处理。
3. **任一子任务解析失败 → 只对失败的那个子任务降级单发**，不整包重试。
   整包重试会把已成功的部分也重算一遍，白烧 token。
4. 合并前后必须过一致性对账（见 `10-parity-and-migration.md` §4）：
   同一批 episode，合并模式与单发模式的判决必须一致。**这是合并能否上线的唯一验收标准。**

### 4.2 二期留口：跨 episode 合并

框架上把「一个 request 承载哪些 (episode, module, frames)」抽象成 `MergeGroup`：

```python
@dataclass(frozen=True)
class MergeUnit:      # 最小可合并单元
    episode_id: str
    module_id: str
    frames: list[FrameRef]
    prompt_part: str
    parser: Callable[[dict], CheckResult]

class MergeStrategy(Protocol):
    def group(self, units: list[MergeUnit], limits: MergeLimits) -> list[MergeGroup]: ...
```

一期实现 `PerEpisodeMultiModule`；二期加 `CrossEpisodeBatch`（N 条 episode × M 帧同包，
提示词里按 episode 二次区分）只需新增一个 `MergeStrategy` 实现，执行器不动。

`MergeLimits` 承载硬约束：单请求最大图片数、最大 prompt token、模型上下文窗口。
分组算法必须尊重这些上限，超限就拆包。

### 4.3 为什么不一上来就跨 episode 合并

需求自己写了「先实现这个」。技术上也成立：跨 episode 合并会让模型在一个上下文里
同时看多条轨迹，**串扰风险**（把 A 的画面记成 B 的）直接威胁判定正确性，
而同 episode 多模块合并的上下文本来就是同一批帧，语义上无损。

## 5. Token 计量

- VLM 客户端新增 usage 采集：每次响应解析 `usage.{prompt_tokens, completion_tokens}`，
  连同 `call_kind`、`model` 发一条 `kind=usage` 的 stderr 事件。
- **合并请求的摊派**：一个 request 承载 N 个模块时，按各子任务 prompt 字符数占比
  摊派 prompt_tokens，completion 按输出段长度占比摊派。摊派是估算，报告里注明
  「合并请求按比例摊派」，同时保留未摊派的 `call_kind=merged` 原始行，两个口径都可查。
- 任务级汇总 = 所有行求和，展示在任务详情和报告里。**不换算金额**（D12）。

## 6. 重试

| 层级 | 策略 | 在报告里的体现 |
|---|---|---|
| 单个 VLM 请求 | 指数退避 1s/2s/4s，最多 3 次（`vlm_retry`，任务参数可调） | 性能剖析里记重试次数与失败原因分类 |
| 对冲补发 | 沿用 v1 的 hedging（超时未返回时补发一次，先到先用） | 延迟明细里 `attempt=0/1` 区分 |
| 模块 | 不自动重试，失败即 `failed`，由用户点「重试」建子任务 | 报告里该模块显示「错误」+ 重试入口 |
| 任务 | 不自动重试 | — |

**为什么模块级不自动重试**：模块失败通常是配置或环境问题（端点不可达、凭证过期），
自动重试只会把同样的错误重复三遍再报给用户，白烧时间和 token。

## 7. 背压与资源保护

- **内存**：decode 阶段监控 RSS，超过阈值暂停派发新 episode（不是杀进程）。
- **VLM 端点**：连续 429 或 5xx 达到阈值时，自动把该后端的在飞上限**减半**并告警，
  恢复后逐步回升（加性增乘性减）。这条保护 v1 没有，是线上产品必须有的。
- **磁盘**：帧缓存有配额，按 LRU 清理已完成 episode 的帧；任务结束清空本任务缓存。

## 8. 待调优项（明确不在本期做，但框架已预留）

需求说得很清楚：「合并做到什么程度，效果会不会有问题，到底能省多少时间和成本，
这个需要作为一个单独的任务来 tune」。本期交付的是可调优的框架 + 一组有实测依据的默认值：

| 待调项 | 本期默认 | 调优产物 |
|---|---|---|
| VLM 并发度 | 32（v1 实测值） | 不同后端/模型的推荐值表 |
| 合并粒度 | 同 episode 全模块 | 是否按 token 上限拆包的阈值 |
| 跨 episode 批大小 | 不启用 | 串扰率 vs 成本曲线 |
| thinking effort | 用户在密钥管理里配 | 各模块的推荐档位 |

调优的量化口径在 `10-parity-and-migration.md` §4 一并定义，保证调优前后可比。
