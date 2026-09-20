# 00 总体架构

> Curator v2 工程设计文档。本册共 12 篇，本篇是入口：讲清楚分层、边界、数据流和本期范围。
> 需求来源：《Physical AI Kit - Curator 设计文档Prompt》+ 两轮澄清（结论见 §7）。

## 1. 一句话

把现有单体（Gradio UI + Python CLI，8 个质检模块跑通）重构成**三层解耦的线上产品**：
原子 CLI → REST API Daemon → 火山风格中文前端，交付形式是 Helm Chart，跑在 VKE 上。

**质检算法一行不改。** 本期做的是骨架、契约和产品化能力，不是算法演进。

## 2. 分层

```
┌───────────────────────────────────────────────────────────┐
│  前端  React 18 + Arco Design + TypeScript                 │
│  纯静态资源，零业务状态，所有状态读自 Daemon                   │
└────────────────────────┬──────────────────────────────────┘
                         │  HTTP/JSON  +  SSE(进度/日志)
┌────────────────────────▼──────────────────────────────────┐
│  API Daemon  FastAPI + uvicorn（单副本，进程内 worker 池）    │
│    routes/      粗粒度语义 API，对外唯一入口                   │
│    orchestr/    任务状态机、worker 池、子任务、断点            │
│    planner/     并发计划 + VLM 请求合并（必经，不可绕过）       │
│    repo/        Repository 接口 → SQLite 实现（可换 RDS）      │
│    exec/        CLI 执行器：subprocess + JSON 解析 + 信号      │
└────────────────────────┬──────────────────────────────────┘
                         │  subprocess，argv 传参，stdout 出 JSON
┌────────────────────────▼──────────────────────────────────┐
│  CLI  curation                                             │
│  原子操作，英文输出，每条命令 --json 给机器、默认给人           │
└────────────────────────┬──────────────────────────────────┘
┌────────────────────────▼──────────────────────────────────┐
│  内核  curation.core / curation.pipeline                    │
│  8 个质检模块 + 读取器 + 导出器。**从 release_v1 原样搬运**     │
└───────────────────────────────────────────────────────────┘
```

### 2.1 为什么 Daemon 用 subprocess 调 CLI，而不是进程内 import

三个理由，都不是洁癖：

1. **故障隔离**：质检要解码视频、跑光流、吃几十 GB 内存。跑挂一个任务不能带走 Daemon，
   否则整个平台的任务列表、密钥管理一起下线。子进程 OOM 只是一个任务变「错误」。
2. **CLI 不会退化**：CLI 是产品的一等入口（Agent 场景要用）。如果 Daemon 走进程内调用，
   CLI 会慢慢变成没人跑的二等公民，参数漂移、输出格式腐烂。让 Daemon 成为 CLI 的最大用户，
   契约就永远是活的。
3. **停止语义干净**：任务停止 = 给进程组发信号，内核代码不需要到处埋 `should_stop` 检查点。

代价是进程启动开销（~1.5s，import daft/numpy/av）和中间结果要落盘。对于分钟到小时级的
质检任务，这个代价可以忽略；落盘反而是子任务重试的前提（见 §4）。

### 2.2 内核的边界

`curation/core/` 保持现状的纪律：**纯函数，不 import daft，不碰 I/O**。
所有检查算法住在这里，搬运时连注释一起原样保留（见 `10-parity-and-migration.md`）。

`curation/pipeline/` 是编排壳，允许 import daft。本期它被拆薄：原来 `run.py` 一口气跑完的
端到端流程，拆成若干可独立调用的 stage，由 Daemon 而不是 DataFrame 链来串。

## 3. 部署拓扑

```
                     ┌──────────────── VKE Pod ────────────────┐
  浏览器 ──HTTPS──▶  │  nginx? 不需要。Daemon 自己 serve 静态资源  │
                     │                                          │
                     │   uvicorn  ┌─ /api/**     REST           │
                     │            ├─ /events/**  SSE            │
                     │            ├─ /healthz    探针            │
                     │            └─ /*          前端静态资源      │
                     │                │                         │
                     │                ├─ worker 池 → subprocess  │
                     │                │      curation check ...  │
                     │                └─ SQLite @ PVC            │
                     └──────────────────┬───────────────────────┘
                                        │
                    ┌───────────────────┼────────────────────┐
                    ▼                   ▼                    ▼
              火山 TOS            方舟 MaaS / 自托管 vLLM   HF 镜像桶
           （数据集 + 交付）          （VLM 推理）          （公共数据集）
```

- **单副本**。任务在 Daemon 进程内的 worker 池里排队执行，1000 条还是 10000 条 episode
  都打到同一个节点上排队。
- **扩展路径**（本期不做，只留接口）：请求层加 LB + 多 StatefulSet 实例分流；
  存储从 SQLite 换火山 RDS。全部收在 Repository 接口后面，业务代码不感知。
- **目标节点规格**：32 核 128G。并发默认值按此标定，见 `04-concurrency-and-vlm-merge.md`。
- 前端由 Daemon 同进程 serve：一个镜像、一个进程、一个端口，Helm Chart 最简单。

## 4. 核心数据流：一次质检任务

这是全文最重要的一张图。它同时解释了「原子 CLI」「子任务重试」「增量重导出」三件事
为什么是同一个设计的三个面。

```
用户新建任务
    │
    ├─▶ ① preflight        curation preflight --input tos://...
    │     读 metadata，出：数据格式、episode 清单、robot_type、模块可用性矩阵
    │     ↓ 前端据此渲染「可跑/标灰/需补充」三态
    │
    ├─▶ ② plan             Daemon 内部 planner（也有 CLI 形态便于调试）
    │     出：CPU 段并发度、VLM 段并发度、请求合并分组
    │
    ├─▶ ③ decode           curation decode --episodes ...
    │     抽帧落本地缓存，供视觉质量/同步/VLM 三方复用（解码是最贵的一步，只做一次）
    │
    ├─▶ ④ check × N        curation check --module <id> --episodes ...
    │     每个模块独立跑、独立落盘：runs/<run_id>/checks/<module>/results.jsonl
    │     ★ 这一步的产物是「模块级、episode 级」的，所以单个模块可以被单独重跑覆盖
    │
    ├─▶ ⑤ aggregate        curation aggregate --run <run_id>
    │     把 N 个模块的结果合成判决：passed / reject / review（待裁决）
    │
    ├─▶ ⑥ export           curation export --run <run_id> [--incremental]
    │     按 passed 清单导出 lerobot_curated
    │
    └─▶ ⑦ report           curation report --run <run_id>
          报告 + 性能剖析
```

漏斗优化（硬门先跑、VLM 垫底只跑幸存者）在 v1 里是 DataFrame 链的隐式行为，
v2 里变成 **planner 的显式决策**：planner 按成本排序模块，前一档的 reject 名单
作为后一档的 `--episodes` 输入。省下来的 VLM 调用一分不少，但现在是可观测、可干预的。

### 4.1 子任务

任务跑完，某个模块失败 → 用户点「重试」→ 建一个**子任务**，只重跑 ④ 里那一个模块：

```
子任务 = check(失败模块) → aggregate(整 run 重算) → report(重生成)
                                                  └─▶ 提示「交付数据集已过期，需重新导出」
```

模块结果是按 `<run_id>/checks/<module>/` 分文件存的，所以覆盖是文件级替换，
不影响其他模块的产物。aggregate 是纯计算、秒级，每次重算全量即可。

**交付数据集不自动重建**（用户明确要求）：页面显式提示，由用户点「重新导出」，
且导出走增量路径，只处理变动的 episode。详见 `06-delivery-and-report.md` §4。

## 5. 本期范围

| 做 | 不做 |
|---|---|
| 三层重构 + 契约定稿 | 任何新质检模块的算法实现 |
| 8 个模块原样搬运 + 黄金对账 | 算法调优、阈值调整 |
| 预检（只识别格式，非 LeRobot v2/v3 全标灰） | mcap / LanceDB 的 reader |
| 密钥管理、任务状态机、子任务、分页、Token 计量 | 火山 IAM 对接（只留抽象） |
| VLM 请求合并框架 + 同 episode 多模块合并 | 跨 episode 帧合并（留扩展点）、并发参数调优 |
| Helm Chart、单副本部署 | 多副本、RDS、LB |
| 前端六页 | 样式与火山控制台的像素级对齐（后期整合时做） |

明确**删除**：内嵌终端（`curation/ui/terminal.py` 及 `/ws/term` 路由、xterm 前端资产）。

明确**保留**：HF 镜像缓存桶、深链 GET 参数契约（`?dataset=` / `?url=` / `?region=` / endpoint 键，
逻辑一字不改）、本地挂载路径输入（降级为调试功能，UI 上标注 experimental）。

## 6. 目标仓库布局

重组后（baseline 清理见 `10-parity-and-migration.md` §1）：

```
curator/
├── backend/
│   ├── curation/
│   │   ├── core/          # 纯函数算法区（原样搬运）
│   │   ├── ingest/        # 读取器（原样搬运）
│   │   ├── export/        # 导出器（增量能力为新增）
│   │   ├── pipeline/      # 编排 stage（拆薄）
│   │   ├── registry/      # 机器人规格库（原样搬运）
│   │   └── cli/           # CLI 层（重写）
│   ├── daemon/            # API Daemon（新建）
│   └── tests/
├── frontend/              # React + Arco（新建）
├── deploy/
│   ├── Dockerfile
│   └── charts/curator/    # Helm Chart（新建）
└── docs/design/           # 本册文档
```

## 7. 已冻结的决策

来自两轮需求澄清，后续设计一律以此为准：

| # | 决策 |
|---|---|
| D1 | 存储：SQLite on PVC，全部收在 Repository 接口后；将来直连火山 RDS |
| D2 | Daemon 单副本 + 进程内 worker 池；扩展靠请求层 LB + 多实例分流 |
| D3 | 单实例单租户；数据模型带 owner 字段，鉴权抽象成 Provider，预留火山 IAM |
| D4 | 密钥、任务、子任务、报告索引全部持久化 |
| D5 | 提交接口只有标准模式 `run_modules()`；helper 优化在 Daemon 内部必经，对调用方不可见 |
| D6 | 预检只识别格式；非 LeRobot v2/v3 全模块标灰 |
| D7 | 不做历史兼容，交付目录结构可重新设计 |
| D8 | VLM 后端两类：方舟（拉模型列表）+ 自定义 endpoint（手填模型），都带 thinkingEffort |
| D9 | 子任务改判后不自动重导出，显式提示 + 用户点击，导出走增量 |
| D10 | 人工裁决 = 任务停在「已完成（待裁决 N 条）」，裁决执行是一种子任务 |
| D11 | 数据来源：私有 TOS + HF 镜像；本地挂载保留为 experimental 调试入口 |
| D12 | Token 只显示消耗量，不换算金额（用户有套餐） |
| D13 | 暂停 = episode 级断点，不做帧级中断续传 |
| D14 | 框架代码英文注释；搬运的算法模块连中文注释原样保留 |
| D15 | 对账基线数据集 `umi_640_notask`；CPU 五项逐位一致，VLM 三项比对判决 |

## 8. 本册索引

| 篇 | 内容 | 主要读者 |
|---|---|---|
| 00 | 总体架构（本篇） | 全员先读 |
| 01 | 数据模型、状态机、Repository | 后端 |
| 02 | CLI 契约 | 后端、Agent 接入方 |
| 03 | REST API 契约 | 前后端 |
| 04 | 并发与 VLM 请求合并 | 后端 |
| 05 | 模块注册表与预检 | 后端、前端 |
| 06 | 交付布局、增量重导出、报告结构 | 后端、前端 |
| 07 | 前端设计 | 前端 |
| 08 | 密钥管理与鉴权 | 后端、前端 |
| 09 | 镜像与 Helm Chart | 后端、运维 |
| 10 | 搬运计划与黄金对账 | 全员 |
| 11 | 并行工作包与接口冻结顺序 | 全员先读 |
