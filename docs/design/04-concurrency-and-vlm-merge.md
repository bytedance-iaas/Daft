# 04 并发编排与 VLM 请求合并

> 2026-09-24 更新：生产 VLM 调用已改为连续视频输入，当前调用图、判定规则与画像输入见
> [13 连续视频输入与判定](13-video-native-vlm.md)。本篇涉及图片探针、逐帧打分、旧请求次数的描述
> 保留为 v1 基线，不再代表当前生产视频协议；并发上限与传输重试框架继续复用。

> 这一篇是后端最核心的新增设计。需求原话：「CPU 和 VLM 是可以并发的，CPU 部分和 VLM 部分的
> 内部也是可以并发的」「尽量合并多个模块的检测在同一个 request 里」「框架上得做到足够灵活」。
> 需求方在评审时重申：所有提交都走标准的 `run_modules()`，后端先用 helper 把能合并的 VLM 请求合并，
> 再用最合适的并发方式去跑。调用方看不到、也不需要看到这一层；能设的只有并发、超时、重试的**上限**（D31）。

## 1. 为什么这件事值得单独设计

v1 的实测数据（写在 `pipeline/default.yaml` 和 `funnel.py` 的注释里，是真实基准，不是估计）：

- VLM 段占端到端耗时 **约 97%**：10 条 DROID 关掉 VLM 只要 12 秒。
- 并发是唯一有效的提速手段：10 条 DROID，episode 并发 1 → 447s，8 → 84s，**5.3 倍**，
  平均延迟恒定在 21s 左右（服务端零排队）。
- 2026-09-07 的吞吐诊断（DROID 50 条，episode 并发 32）：打分闸门 16 → 64，判定段 13.8 → 7.7 分钟，
  方舟实测在飞 117 个请求，**零 429、零排队**，平均延迟 22.6 → 22.8s。
  （更早那条「32×8=256 在飞」的说法已被这次诊断推翻：几类请求的上限各自独立，不是两层相乘。）

所以：CPU 段怎么排都行，**VLM 段的并发和合并决定产品体验和成本**。

## 2. 分档执行模型

档的划分与顺序**照搬 v1 的漏斗**（D18），不是重新设计的：

```
  autolabel   无标注条目补任务描述（VLM；漏斗之前，对无标注条目全量）
      │
  数值档      timestamp_check · kinematic_limits · motion_quality        只读 parquet，秒级
      │ 硬门①  时间戳、运动学
  帧档        visual_quality · video_action_sync                         同进程共享一次全帧率解码
      │ 硬门②  视频-动作同步
  VLM 档      task_success                                               只跑幸存者
      │
  判决        六项检查合成 keep / drop
      │
  判决之后    dedup → skill_profile                                       只对 keep 集合，数据集级
```

**档内并发，档间串行**。一档是一个 CLI 进程，吃完上一档的全部幸存者才轮到下一档 —— v1 就是这样。
评审前的版本设想过「档间流水」（CPU 档处理完一批就推给 VLM 档），评审时拿掉了：
帧档在 8 并发下只占总时长的 4% 左右，流水最多省这 4%，代价却是按批切分带来的批边界等待
（一批里最慢的那条拖住整批，闸门空转），以及多进程并发写结果的复杂度。不值。

暂停与恢复也因此很简单：SIGTERM 让进程收尾退出，恢复时同一条命令加 `--resume` 再跑，
已有结果的 episode 自动跳过（02 篇 §3.5、§4）。

### 2.1 CPU 档并发度

> 2026-09-25 起（D54）：容器里能看到的核都可以用，原来的 `min(8, 核数/4)` 与站点的 `concurrency.cpu` / `cpuMax` 取消。

目标节点 32 核 128G（dataverse 给质检台的 `limits.cpu` 缺省 32）。Daemon 的 planner 默认值：

| 档 | 默认 | 说明 |
|---|---|---|
| 数值档、帧档 | 容器 CPU 配额 − 2 = 30（至少 1） | 每个 worker 占一个核；两档流水交叠时数值档占 1/4、帧档拿其余 |
| 留给 Daemon 与网页 | 2 核 | 不给 worker |
| 内存护栏 | RSS 超过物理内存 80% | 暂停派发新 episode，不杀进程 |

- **核数**：Daemon 读容器的 CPU 配额（cgroup v2 的 `cpu.max`，读不到再看 v1 的 `cpu.cfs_quota_us`），没有配额时用本机核数；
  `CURATOR_CPU_CORES` 可以直接指定。直接跑 `curation plan` 时是 `os.cpu_count()`，也可以用 `--cpu-cores` 给。
- **一个 worker 一个核**：CLI 在一个进程里用线程池并发（`--concurrency N` 个线程），OpenCV、numpy 的原生库各自还会再开一池线程。
  30 个 worker 每个再开一池会互相抢核，所以 Daemon 起的 CLI 子进程一律单线程：环境里设 `OMP_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`
  一类的变量（Daemon 自己的环境里设了别的值时以它为准），`curation check` 开始时把同一个数交给 OpenCV（`cv2.setNumThreads`）。
  不经 Daemon、手动跑的命令不设这些变量，OpenCV 保持它的缺省。
- **上限**：计划里 CPU 档的 `concurrency` 是「这个任务最多用多少」= min(可用总数, 任务上限 `params.limits.cpu_concurrency`)；
  任务上限只能往下压。实际能同时跑几条，看 Daemon 的全局 CPU 池里还剩多少（§2.3）。
- **数据完整性档**主要耗 I/O，宽度取计划的并发、不从 CPU 池拿名额；开了逐帧解码（L3）时它也吃 CPU，才和数值档、帧档一样每条占一个名额（设计 14 §2.2）。

需求说「CPU 部分相对耗时较少，这里并行度可以默认用 1」，v1 也是串行。
所以 **CLI 的默认就是 1**，符合 CLI 的原子/无并发纪律；Daemon 的 planner 在 32 核节点上显式传
`--concurrency 30` —— 这正是「CLI 默认不并发，并发作为可选参数」和「Daemon 负责优化」两条要求的结合点。
640 条 episode 的帧档，串行约 31 分钟，8 并发约 4 分钟（30 并发的实测待补，F9.5）。
任务可以给一个更低的上限（`params.limits.cpu_concurrency`），planner 取两者中小的那个。

各 episode 之间没有共享状态，并发和线程数都不改变任何一条的计算结果；这一点由黄金对账的逐位一致来验证
（单线程下另跑过一遍回放，逐位一致）。

### 2.2 VLM 档并发度：一个并行度 N，八把闸门

v1 里 VLM 的在飞上限**不是一个数**，是八把各自独立的闸门（2026-09-07 吞吐诊断实锤）：

| 闸门 | 管什么 | v1 配置键 | v1 默认 | 由 N 推导 |
|---|---|---|---|---|
| episode 并发 | VLM 档同时处理几条 episode | `pipeline.vlm_episode_concurrency` | 32 | N/2 |
| probe | 打分请求，进程级一把，所有 episode 共用 | `checks.task_success.vlm.max_concurrency` | 64 | N |
| endstate | 逐机位复核 | episode 并发 × 2 | 64 | N |
| arbitration | 取证仲裁链（四个工厂共用一把） | = episode 并发 | 32 | N/2 |
| 护栏 caption | 判废护栏里的 caption | = episode 并发 | 32 | N/2 |
| caption | 技能画像与 autolabel 的打标 | `skill_profile.caption_concurrency` | 32 | N/2 |
| llm | 技能归纳的纯文本调用 | `skill_profile.llm_concurrency` | 16 | N/4 |
| audit | 标注分歧的配对判断 | `skill_profile.audit_concurrency` | 16 | N/4 |

产品上只让用户配**一个数**：模型（或后端）的并行度 N，默认 64。planner 按上表推导八把闸门，
N=64 时与 v1 的出厂默认逐项相等。站点配置仍可以逐把覆盖（调优任务会用到）。

N 的实际取值是一串上限里最小的那个（D31）：

```
N = min( 任务上限 params.limits.vlm_parallelism,   ← 用户在 API / CLI / UI 上给的，可不给
         模型上配的并行度, 后端上配的并行度,        ← 密钥与资源管理页
         站点上限 )                                ← values.yaml
```

用户能给的只有这个上限；闸门怎么配比、各档怎么排，不接受外部指定。

两点要说清楚：

- **N 不是硬上限。** 八把闸门互相独立，同一时刻的在飞请求可以超过 N（v1 在 N=64 的配置下实测到 117）。
  本期不加全局硬上限 —— 那会改变 v1 已经验证过的吞吐形态；超出服务端配额的情况交给 §7 的自适应降并发。
- ⚠️ 闸门必须是**自建信号量**，不能用 daft 的 `max_concurrency` —— 后者对 async 行级 UDF
  静默失效（v1 `funnel.py` 里踩过，注释有记录）。搬运时这条纪律不得丢。
  `funnel.py:867` 那句「总并发 = 两层相乘」的注释已经过时，搬运时一并改掉。
- **有三把闸门跟着 episode 闸门走**（W6 按 v1 `funnel.py` 核实）：endstate = 2 × episode，arbitration、guard_caption
  = episode。N 为偶数时它们就是上表的 N、N/2、N/2；N 为奇数时 endstate 比表里少 1。

### 2.3 多个任务同时跑

Daemon 用 subprocess 调 CLI，闸门是 CLI **进程内**的信号量，管不到别的进程。
两个任务同时跑，每个都以为自己独占 N。

默认做法：**同一时间运行 3 个任务**（`CURATOR_MAX_RUNNING_TASKS`，D54；原来是 1），其余排队。
子任务和主流程一样占这个名额：任务 A 的重试和任务 B 的主流程算两个。
对冲补发和重试不另开口子 —— 它们在闸门之内排队，输掉的那一发在返回前一直占着闸门的名额（v1 的既有行为）。

**VLM**：planner 在任务启动那一刻把 N 按「正在运行的任务数」均分（3 个同时跑时每个约 64 / 3 = 21），运行中不再动态调整。
跨进程的全局令牌桶能做得更精确，但要给 CLI 加一条到 Daemon 的反向通道，本期不做。

**CPU：Daemon 管一个全局 CPU 池**（D54，`daemon/orchestr/cpupool.py`）。池的大小就是 §2.1 的可用 worker 总数（32 核是 30）。

- 流水线（`episode_pipeline.run_episodes`）派发一条 CPU episode 之前先从池里拿一个名额，这一条做完（成功、出错、被暂停或停止）后归还。
  挂钩点在 Daemon 的派发循环里，和每层的宽度、帧档的内存准入放在一起：帧档先过内存准入，再拿名额。CLI 进程内不用改，
  `--concurrency` 仍按计划给，作为这一档在途条数的上限。
- 名额跟着在途表走：派发循环每一轮按各 CPU 档实际在途的条数对一次账，子进程崩溃时它丢下的条目回到待派发，名额随之收回；
  暂停、停止、系统暂停时，等在途的条目做完落盘、子进程收尾之后，这个任务才整体退出池子。
- 整档执行（重试、流水线之前的旧任务）不逐条派发：开始时按块拿名额（至少 1 个，等到够它的公平份额或它要的数），
  把拿到的数作为 `--concurrency` 传给 CLI，结束时归还。
- **公平**：一个任务要名额没拿够，就记为「在等」。有任务在等、而且它手上的少于公平份额（池大小 ÷ 正占着或正在等的任务数）时，
  已经达到份额的任务拿不到空出来的名额；已经多占的不收回，随它的条目做完慢慢降到份额。没人等时，一个任务可以用满整个池。
  这样先开跑的任务占满池子之后，后开跑的任务在几条 episode 之内就能拿到自己那一份。
- 外部 CLI 的批处理兼容路径（设了 `CURATOR_CLI` 时的 `run_batches`）不接池子，按计划的并发跑。

内存与临时盘：3 个任务共用同一份内存和同一块临时盘（dataverse 缺省 500Gi）。帧档每个 worker 峰值约 1.5GB，
帧档的内存准入兜底；TOS 上的 Lance 数据集要整表拷到临时盘，3 个大数据集同时跑时要算一下够不够。

## 3. 执行计划（Plan）

planner 的输出，也是 `curation plan --json` 的 schema：

```jsonc
{
  "schema_version": "1.0",
  "vlm_parallelism": 64,
  "limits": {"cpu_concurrency": {"value": 30, "bound_by": "planner"},    // 取了哪个上限、卡在哪一层（D31）
             "vlm_parallelism": {"value": 64, "bound_by": "model"}},
  "stages": [
    {"id": "autolabel", "kind": "vlm", "command": "autolabel",
     "episodes": "unlabeled", "gates": {"caption": 32}},
    {"id": "numeric", "kind": "cpu", "command": "check", "concurrency": 30,
     "modules": ["timestamp_check", "kinematic_limits", "motion_quality"],
     "episodes": "selected", "hard_gates": ["timestamp_check", "kinematic_limits"]},
    {"id": "frame", "kind": "cpu", "command": "check", "concurrency": 30,
     "modules": ["visual_quality", "video_action_sync"],
     "episodes": "survivors:numeric", "hard_gates": ["video_action_sync"]},
    {"id": "vlm", "kind": "vlm", "command": "check",
     "modules": ["task_success"], "episodes": "survivors:frame",
     "gates": {"episode": 32, "probe": 64, "endstate": 64, "arbitration": 32, "guard_caption": 32},
     "merge": {"strategy": "none", "groups": []}},
    {"id": "verdict", "kind": "aggregate", "command": "aggregate", "phase": "funnel"},
    {"id": "dedup", "kind": "cpu", "command": "check", "concurrency": 1, "modules": ["dedup"], "episodes": "keep"},
    {"id": "profile", "kind": "vlm", "command": "check", "modules": ["skill_profile"],
     "episodes": "keep-minus-duplicates", "gates": {"caption": 32, "llm": 16, "audit": 16}},
    {"id": "final", "kind": "aggregate", "command": "aggregate", "phase": "final"}
  ],
  "estimates": {"vlm_requests": 735, "wall_clock_s": 1100, "notes": ["..."]}
}
```

没勾选的模块不出现在计划里；某一档一个模块都没有，这一档就整个省掉。
`kinematic_limits` 这类在预检时被判「不支持」的模块同样不进计划。

计划是**数据**，不是代码路径：Daemon 生成它、随任务存档（`plan.json`）、按它调度，
并可经 `GET /tasks/{id}/plan` 只读查看。调优时只改 planner 和站点配置，执行器一行不动。
这是需求要的「框架灵活性」；它不对外提供提交自定义计划的入口（D5）。

## 4. VLM 请求合并

### 4.1 先看清 v1 到底怎么调 VLM

合并设计必须建在真实的调用图上。v1 一共 3 个使用方、5 种调用标签
（标签是 `details/vlm_latency.csv` 的数据契约，见 `vlm_call_kinds.py`）：

| 调用 | 解码 | 选帧 | 每次请求的图 | 每条请求数 | 什么时候跑 |
|---|---|---|---|---|---|
| `probe` 任务完成度打分 | 0.5s 间隔、最长边 448、≤4 机位 | 各机位按下标对齐成「时刻」，linspace 含首尾取 8 个时刻，**按时间顺序**提交 | 2×机位数：各机位第 0 帧作参考 + 该时刻各机位画面；模型回 0–100 一个整数 | 8 | 过了两道硬门的每一条 |
| `endstate` 逐机位复核 | 复用 probe 已解码的帧 | 每机位 linspace 含首尾取 8 帧，前 4 为「早期」后 4 为「后期」 | 8 张，**单机位** | 机位数 × 2（「做成了吗」「失败了吗」分开问） | 每一条，不看 probe 的结果 |
| 判废护栏（`caption` + `llm`） | 复用已解码的帧 | 每机位 linspace 取 N 帧 | 机位数 × N，外加 1 次纯文本比对 | 0–2 | 仅当已判废且任务文本来自原始标注 |
| `arbitration` 取证仲裁 | 复用已解码的帧 | 外部机位：夹爪开合事件定锚点，挑最清晰的；腕部机位：事件前后 −0.5 / 0 / +1.0 / +2.5 秒 | 定位 1 张整帧；合议 2 张（整帧 + 目标框外扩 15% 后放大 3 倍）或腕部 4 张放大 2 倍 | 出题 1 + 每锚点定位 1 + 每线 3 票 | 仅当打分与复核之后仍弃权 |
| `arbitration` 任务类型判别 | — | — | 纯文本 | 0–1 | 关键词规则没命中时 |
| `caption` 技能打标 / autolabel | **另一次全帧率解码**、448、≤4 机位 | 每机位 linspace 含首尾取 8 帧 | ≤32 张，多机位分段带标签 | 1 | 画像：判决通过且去重后的条目；autolabel：漏斗前的无标注条目，结果被画像复用 |
| `llm` 技能归纳 / 标注分歧 | — | — | 纯文本 | 数据集级，几次到几十次 | 画像阶段 |

图片统一 JPEG 质量 85、base64 内联，`temperature=0`，请求体只有 model / temperature / max_tokens / messages。

判定逻辑的骨架（细节在 `core/checks/task_success.py` 的 docstring 里，原样搬运）：

```
probe 8 个分数 ─▶ 初判：成功候选 / 失败候选 / 灰区 / 冲高回落 / 过程语无伦次
endstate 汇票  ─▶ 决定表：「救人一签就够，杀人必须双签」—— 只有 失败候选 × 复核一致说未完成 才判废
判废护栏       ─▶ 标注和画面不是同一件事 ⇒ 不杀，转人工（疑似标注错）
取证仲裁       ─▶ ≥2 条线说失败才判废；说成功则救回；其余维持弃权 ⇒ 人工裁决
```

几个设计是 v1 用人工真值消融后定下来的，任何合并方案都绕不开：

- probe **一个时刻一次独立请求，按时间顺序提交**：每次只按该时刻的物体状态打分。2026-09-24 按需求取消探针乱序及分数逆置换；并发结果仍与输入时刻一一对应。
- endstate **逐机位独立投票**：多机位混问时好机位的证据会被烂机位稀释；**两问分开**，
  「两问矛盾」本身是一个信号。
- caption 反而是**多机位合问**：单机位时目标被挡，模型会编故事。

一条 3 机位、有标注、不触发仲裁的 episode：8 + 6 + 1 = **15 次请求，约 120 张图**。冗余在三处：

1. probe 的 8 次请求每次都重发同样的 3 张参考帧；
2. endstate 同一机位的两问发了两遍同样的 8 张图；
3. caption 选的帧和 endstate 选的几乎是同一批（时间差不超过 0.25 秒），但一个多机位、一个单机位，请求形状对不上。

前两处在模块内部，合掉就是改算法；第三处是唯一的跨模块重叠，却没有现成的合并形状。
**结论：现有两个 VLM 模块之间没有「零代价」的合并点。** 需求里「5 个模块合一个请求」的场景，
对应的是将来那些「抽 N 帧、问一次」的新模块 —— 它们帧策略一致，合并才是自然的。

有一个不改提示词的省钱点：probe 的请求内容已经是「题面 → 参考帧 → 时刻帧」的顺序，
8 次请求共享同一段前缀，方舟的隐式缓存会命中，`usage.prompt_tokens_details.cached_tokens` 能量出来。
本期把这个字段记下来（01 篇 §2.6），怎么利用交给后续的调优任务。

### 4.2 合并框架

框架回答一个问题：**一个 request 里装哪些（episode, 模块, 帧）**。

```python
@dataclass(frozen=True)
class FramePolicy:       # 帧策略相同，才谈得上共用一次传图
    decode: Literal["interval", "full_rate"]
    interval_s: float | None
    max_side: int
    max_cams: int
    pick: str            # 例："linspace:8"

@dataclass(frozen=True)
class MergeUnit:         # 最小可合并单元 = 某模块对某条 episode 的「一次提问」
    episode_index: int
    module_id: str
    frame_policy: FramePolicy
    prompt_part: str     # 该模块的提示词正文，逐字沿用
    parser: Callable[[str], CheckResult]
    call_kind: str

class MergeStrategy(Protocol):
    def group(self, units: list[MergeUnit], limits: MergeLimits) -> list[MergeGroup]: ...
```

- 一期实现两个策略：`none`（逐个单发）和 `per_episode_multi_module`
  （同一条 episode、`frame_policy` 完全相同的 unit 合成一个 request）。
- `MergeLimits` 承载硬约束：单请求最大图片数、最大 prompt token、模型上下文窗口。超限就拆包。
- 模块通过注册表声明自己有没有 `MergeUnit`（05 篇 §7）。**多步证据链式的模块**
  （task_success 这种前一步结果决定后一步问不问的）不声明，框架不碰它。
- **提案、执行、回执，三步分别在哪**：planner（Daemon 里）只出**提案** —— 这一档里哪些 unit 可以进同一组，
  写进 `plan.json`，经 `--plan-stage` 交给 CLI。**执行**在 `check` 进程里：同一档的多个模块本来就在同一个进程
  （D18），合并执行器在发请求的那一刻，把**此刻都已就绪**且在同一组里的 unit 拼成一个请求；
  有前置依赖没满足的（比如还在等 autolabel 的任务文本）不等它，各发各的。
  **回执**写进结果：每个 unit 记下自己是合并发的、拆包发的还是降级单发的，性能剖析里的合并节省量由回执统计，不靠估。

合并后的请求形如：

```
You are given 8 frames from one robot demonstration episode.
Complete ALL of the following tasks. Return STRICT JSON with exactly
these top-level keys: "task_1", "task_2".

Task 1 (<module A>): ...(模块 A 的 prompt 原文)...
Task 2 (<module B>): ...(模块 B 的 prompt 原文)...
```

**合并纪律（否则一致性会崩）**：

1. 每个子任务的 prompt 正文**逐字沿用**模块自己的版本，只在外层加编号与输出格式声明。
   prompt 是算法的一部分，改一个词就不是「照搬」了。
2. 输出解析**按 key 拆回**，拆出来的每段交给模块原本的解析函数处理。
3. **任一子任务解析失败 → 只对失败的那个子任务降级单发**，不整包重试。
   整包重试会把已成功的部分也重算一遍，白烧 token。
4. 任何模块在「允许被合并」之前必须过一致性对账（`10-parity-and-migration.md` §3.4）：
   同一批 episode，合并模式与单发模式的判决必须一致。**这是合并能否上线的唯一验收标准。**
5. 合并可以经站点配置一键关闭（`vlm.merge.enabled=false`）。合并是优化，不是功能。

### 4.3 一期的合并对象（D23）

**一期交付合并框架；现有两个 VLM 模块不声明 `MergeUnit`，按 v1 的调用图原样跑。**

- 框架本身（`FramePolicy` / `MergeUnit` / 两个策略 / `MergeLimits` / 按 key 拆回 / 单项降级 / usage 摊派 /
  一键关闭）完整交付，是 `run_modules()` 内部 helper 的必经一环。
- 验证方式：契约测试 + 一个只存在于测试里的示例 VLM 模块（两个「抽 8 帧、问一次」的假模块，
  帧策略相同），覆盖合并、超限拆包、单项解析失败降级、usage 摊派四条路径；
  合并模式与单发模式的一致性对账同样用它跑通（10 篇 §3.4）。
- 对现有数据的影响：**没有**。本期线上不会真的发生跨模块合并，planner 给 VLM 档出的计划是
  `"merge": {"strategy": "none"}`；黄金对账因此不引入新的变量。
- 收益从第一个「抽 N 帧、问一次」的新 VLM 模块接入时开始兑现 —— 实现 `merge_units`，其余自动发生。

评审时放弃的两个方向，留个记录免得以后重提：把 caption 并进 task_success 的请求
（caption 的输入会变，技能体系和标注分歧队列与 v1 不再可比，而每条只省约 1/15 的请求）；
合并模块内部的多次调用（直接改掉 v1 消融定稿的问法）。

### 4.4 二期留口：跨 episode 合并

`CrossEpisodeBatch`（N 条 episode × M 帧同包，提示词里按 episode 二次区分）只需新增一个
`MergeStrategy` 实现，执行器不动。它排在后面，是因为让模型在一个上下文里同时看多条轨迹有**串扰风险**
（把 A 的画面记成 B 的），直接威胁判定正确性；需求自己也写了「先实现」同 episode 的那种。

## 5. Token 计量

- VLM 客户端新增 usage 采集：每次响应解析
  `usage.{prompt_tokens, completion_tokens, completion_tokens_details.reasoning_tokens, prompt_tokens_details.cached_tokens}`，
  连同 `module`、`call_kind`、`model` 发一条 `kind=usage` 的 stderr 事件。
  v1 完全没有这个能力，这是对 `vlm_client.py` 为数不多的允许改动之一：只加旁路统计，不碰判定逻辑。
- **两本账**（01 篇 §2.6）。实际调用账：一次请求一笔，合并请求整笔记为 `call_kind=merged`。
  分摊账：一个 request 承载 N 个模块时，按各子任务 prompt 字符数占比摊 prompt_tokens，
  completion 按输出段长度占比摊，报告里注明「合并请求按比例分摊」。分摊账的合计恒等于实际账，
  **两本账不相加** —— 加了就是双计。
- 任务级汇总只对实际账求和，**含重试和子任务花掉的**，展示在任务详情和报告里，可按子任务展开。
  拿不到 `usage` 的请求（超时、对冲里输掉的那一发、中断时在飞的）不估算，单独计数。**不换算金额**（D12）。

## 6. 重试

v1 的客户端里已经有两套互不相同的机制，新增的 `vlm_retry` 要和它们说清楚谁包谁：

| 层 | 适用 | 策略 | 在报告里的体现 |
|---|---|---|---|
| 内层 · 对冲（v1 `hedged_request`，原样保留） | 带图的请求 | T 时刻未返回就补发一次、先到先用；5xx / 连接错立即串行重发一次；两发都超时再给一次；**4xx 含 429 原样交回，不重发**；一次逻辑调用最坏 ≈ 排队 + 3×超时 | 延迟明细里 `attempt` 分列，失败原因分类（timeout / http_error / connect_error） |
| 内层 · 文本重试（v1 `llm_ask`，原样保留） | 纯文本请求 | 408 / 429 / 5xx / 连接错 / 超时，隔 1s / 2s / 4s，最多 4 次 | 同上 |
| **外层 · `vlm_retry`（新增）** | 所有 VLM 调用 | 内层放弃之后才触发；对超时 / 连接错 / 5xx / 429 再试，间隔 1s / 2s / 4s（429 遵守 `Retry-After`），默认 3 次，任务参数可调 | 性能剖析里单列「外层重试次数 / 挽回次数」 |
| 模块 | — | 不自动重试，失败即 `failed`，由用户点「重试」建子任务 | 报告里该模块显示「错误」+ 重试入口 |
| 任务 | — | 不自动重试；`stopped` / `failed` 可「继续运行」 | — |

CLI 默认 `--retry 0` 且不开 `--hedge`（需求：CLI 默认不重试）；Daemon 按任务参数传
`--retry 3 --hedge`，行为与 v1 的线上形态一致再加一层外层重试。

**为什么模块级不自动重试**：模块失败通常是配置或环境问题（端点不可达、密钥过期），
自动重试只会把同样的错误重复三遍再报给用户，白烧时间和 token。

外层重试用尽仍失败的那条 episode，该模块的结果行是 `verdict=error`。v1 把这种情况记成弃权
（理由以「VLM 调用/解析失败」开头）并照常交付；v2 把它和模型正常的弃权分开：**出错的暂不交付，等补跑**（D24）。
「重试」只补跑这些条目（03 篇 §3.2），对应 v1 的 `--retry-abstained`。

## 7. 背压、容错与资源保护

需求的硬要求：「任务级别的失败不能影响整个流水线」。落到 episode 这一层，是四条规则：

- **一条出错，只停这一条**（D24）。某条 episode 在某一档出错，它不进后面的档、记入待补跑、暂不交付；
  同一档的其它条目、后面的档照常。补跑时从它出错的那一档接着往后跑，该档的模块对它从头重跑。
  已被正常判完的模块确定拒绝的条目例外：聚合时直接拒绝，不进待补跑，也不补跑（D35）。
  「出错」取宽口径（D33）：任何一次模型调用重试用尽、或任一机位解码失败都算，不看 v1 的降级逻辑能否给出结论。
  这样定有两个理由：出错多半是这条数据自己的问题（视频坏了），后面的档大概率也过不去；
  而且下游有的会吃上游的产出（task_success 要 autolabel 给的任务文本），上游没成，下游跑了也是错的。
- **整个模块失败，是另一条规则**。模块跑不起来通常是环境问题，不是数据问题。这时它的门视为**尚未生效**，
  后面的模块照常跑、照常出自己的报告小节（需求原话：一个模块失败，重试时只跑失败的模块）；
  但所有条目都缺这个模块的结论，所以全部待补跑、暂不交付。模块重试成功后，它判拒的条目在聚合时剔除。
  直接依赖它产出的下游模块除外（autolabel 失败时，task_success 对无标注条目不跑）。
  适用于所有已勾选的模块，包括不判废的去重和技能画像，以及 autolabel：
  补不出任务描述的无标注条目同样待补跑，而不是拿一句空话去判成败。
- **反复搞崩进程的那一条，点名跳过**。解码库的原生崩溃 try/except 接不住，会带走整个 CLI 进程。
  Daemon 看 `inflight.json` 知道出事时手上是哪几条，带 `--resume` 重新拉起；同一条 episode 连续两次出现在
  崩溃现场，就把它记为 `error`（原因：进程崩溃）并跳过。这是 v1 `pipeline/isolation.py`「对折缩围」的简化版 ——
  结果逐条落盘之后，不用再二分。
- **模块级熔断**。一档刚开始，连续 20 条全部因为同一类基础设施错误失败（端点不可达、401、配额耗尽），
  不再逐条耗尽重试，整个模块以退出码 4 失败，其余模块继续。否则一万条数据要白等一万次超时。
- **中断不丢已完成的工作**（D26）。结果逐条落盘；Pod 重启、升级、崩溃之后自动从检查点续跑。
  中断那一刻在飞的请求结果丢了，那几条会重新调用一次，报告里单列「因中断而重做的 episode 数」。

资源保护：

- **内存**：帧档监控 RSS，超过阈值暂停派发新 episode（不是杀进程）。
- **CPU**：所有在跑任务的 CPU 档共用 Daemon 的全局 CPU 池（§2.3），在途的 CPU episode 总数不超过核数 − 2；子进程单线程。
- **VLM 端点**：自适应降并发放在 **CLI 进程内的 VLM 客户端**里 —— 运行中的子进程的闸门，Daemon 够不着。
  30 秒窗口内 429 或 5xx 的比例超过阈值，八把闸门按同一比例**减半**，恢复后逐步回升（加性增、乘性减），
  每次调整发一条 `kind=throttle` 事件，Daemon 记入日志和指标并告警。这条保护 v1 没有，是线上产品必须有的。
- **磁盘**：没有帧缓存（D18），本地占空间的只有两样 —— 任务工作目录（结果、证据帧、日志，MB 到 GB 级）
  和导出时的视频临时文件（v3 源要重编码，必须先写本地再整文件拷贝，见 06 篇 §4.4）。
  导出前检查临时卷的余量，不够就让导出失败并说明原因，不去挤占工作目录。

## 8. 待调优项（明确不在本期做，但框架已预留）

需求说得很清楚：「合并做到什么程度，效果会不会有问题，到底能省多少时间和成本，
这个需要作为一个单独的任务来 tune」。本期交付的是可调优的框架 + 一组有实测依据的默认值：

| 待调项 | 本期默认 | 调优产物 |
|---|---|---|
| 并行度 N | 64（v1 出厂默认的等价值） | 不同后端/模型的推荐值表 |
| VLM 跨任务全局池 | 不做，开跑时按任务数均分（§2.3） | 是否像 CPU 一样由 Daemon 统一分 |
| 八把闸门的配比 | §2.2 的推导表 | 是否需要偏离 v1 配比 |
| 合并粒度 | 现有模块不合并；新模块按帧策略自动合并 | 是否按 token 上限拆包的阈值；现有模块是否值得为合并改问法 |
| 跨 episode 批大小 | 不启用 | 串扰率 vs 成本曲线 |
| 前缀缓存 | 只记录 `cached_tokens` | 命中率，以及是否值得为它调整请求内容的顺序 |
| 思考强度 | 不传（模型默认） | 各类调用的推荐档位；注意 v1 各类调用的 `max_tokens` 和超时是按默认档定的 |

调优的量化口径在 `10-parity-and-migration.md` §4 一并定义，保证调优前后可比。
调优通过站点配置和 planner 进行，不需要、也没有对外的自定义提交入口；任务级的上限（D31）只能把并发往下压，不能改形态。
