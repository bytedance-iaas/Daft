# 10 搬运计划与黄金对账

> 这一篇管一件事：**怎么保证重构之后，八个模块的判决和 v1 一模一样。**
> 需求原话：「在 port 的过程中，一定要保证现有实现的完整一致性。实现的思路和算法不能有任何改动。」

## 1. Baseline 清理

分支：`feat/curator-v2`（已从 `release_v1` 创建，尚未做任何删除）。

清理步骤，**每步一个独立 commit，可回滚**：

```
① 删除上游 daft 源码与配套    src/ daft/ tests/ benchmarking/ docs/ examples/
                              Cargo.* rust-toolchain.toml .cargo/ tools/ k8s/ mkdocs.yml ...
   保留：LICENSE、.gitignore（改写）、.github（重建 CI）
② 目录重组                   robot-curation/curation → backend/curation
                             robot-curation/Dockerfile → deploy/Dockerfile
                             robot-curation/docs/design → docs/design
③ 删除退役代码                backend/curation/ui/ 整个（Gradio + terminal + xterm 资产）
④ 依赖清理                   requirements.txt 去掉 gradio 及其独占依赖
⑤ 冒烟                       CLI 能跑、测试能过、镜像能构建
```

**为什么从 `release_v1` 派生而不是空分支**：保留 git blame。搬运算法时，
每个阈值都能追回它是哪次实测定的（代码注释里全是这类依据）。真出了对账不一致，
blame 是最快的排查路径。最终产物一样：一个不含上游 daft 的分支。

⚠️ 第 ① 步是破坏性操作，**执行前需要你点头**。在此之前分支上不做任何删除。

## 2. 三类代码的搬运策略

| 类别 | 范围 | 策略 |
|---|---|---|
| **A 原样搬运** | `core/checks/*`、`core/contract.py`、`registry/*`、`ingest/*`、`export/lerobot_writer.py`、`pipeline/verdict.py`、`dataset_level/*` | **逐字复制，连中文注释一起**。只允许改 import 路径。任何其他改动都要在 PR 里单独说明理由 |
| **B 改造搬运** | `pipeline/run.py`、`pipeline/funnel.py`、`adapters/vlm_client.py`、`pipeline/rejudge.py`、`export/publish.py` | 拆成 stage / 加 usage 采集 / 加增量导出。**算法调用顺序和参数不变**，只改编排外壳 |
| **C 全新编写** | `cli/`、`daemon/`、`frontend/`、`deploy/charts/` | 英文注释，按本册契约实现 |

### 2.1 A 类的保护机制

在 CI 加一条检查：A 类文件的内容哈希如果变化，必须在 PR 描述里带 `parity-change:` 标记说明。
防止重构过程中「顺手优化」掉一行算法。

### 2.2 B 类里最危险的三处

1. **`funnel.py` 的并发闸门**：必须继续用自建信号量，**不能换成 daft 的 `max_concurrency`**
   （对 async 行级 UDF 静默失效）。
2. **`vlm_client.py` 的对冲补发**：hedging 逻辑和延迟统计口径（7 元组行、epoch 时间戳）
   原样保留，只**新增** usage 采集。
3. **`rejudge.py` 的裁决优先级**：「整条弃用压过成败裁决」这类规则藏在代码顺序里，
   拆分时极易丢失。拆之前先把现有单测跑通，拆之后必须全绿。

## 3. 黄金对账

### 3.1 基线

- 数据集：**`umi_640_notask`**（现有部署里就有）。
- 基线产物：用 v1（`release_v1`）跑一遍完整质检，把交付目录整个存档为 `golden/v1/`。
  这一步在动任何重构代码之前做，**基线必须由 v1 的代码生成**。
- 规模：先 64 条跑通流程，再全量 640 条做正式对账。

### 3.2 对账口径

| 模块 | 口径 | 容差 |
|---|---|---|
| `timestamp_check` | 每条 episode 的 verdict + 所有数值字段 | **逐位一致** |
| `kinematic_limits` | 同上 | **逐位一致** |
| `motion_quality` | 同上（含六个子分） | **逐位一致** |
| `visual_quality` | 同上（逐相机分数） | **逐位一致** |
| `video_action_sync` | 同上（逐相机 lag、相关峰值） | **逐位一致** |
| `dedup` | 重复组划分 | **逐位一致** |
| `task_success` | 每条 episode 的 verdict（pass/fail/abstain） | 允许少量不一致，**逐条列出并人工确认** |
| `skill_profile` | 族数、每条的族归属、标注分歧队列 | 同上 |

「逐位一致」= `results.jsonl` 按 episode_id 排序后，浮点数按 `repr()` 精确比较。
CPU 五项不允许任何差异 —— 它们是确定性计算，有差异就是搬运出了 bug。

VLM 两项有随机性（实测：方舟 temperature=0 同一条打 5 次可得 5 种 caption），
所以口径是**判决级比对 + 人工过目差异条**，不是文本级比对。
经验上判决是稳定的；如果判决差异超过 2%，视为搬运有问题而不是随机性。

### 3.3 对账工具

```bash
curation-parity compare --golden golden/v1/<run_id> --candidate <run_dir> \
                        --strict timestamp_check,kinematic_limits,motion_quality,\
visual_quality,video_action_sync,dedup \
                        --verdict-only task_success,skill_profile --json
```

输出：逐模块一致/不一致计数、差异条目明细、结论。**这个工具是工作包 W0 的一部分，
必须先于重构存在** —— 没有对账工具的重构是在裸奔。

### 3.4 合并优化的对账（单独一关）

VLM 请求合并是本期唯一会改变模型输入形态的改动，必须单独验证：

```
同一批 episode（建议 64 条），同一模型、同一 thinking effort：
   A. 单发模式（每模块一个 request）→ 判决集合 A
   B. 合并模式（同 episode 多模块合并）→ 判决集合 B
验收：A 与 B 的判决一致率 ≥ 98%，且不一致条目人工过目确认无系统性偏差
```

不达标就**默认关闭合并**（配置开关 `vlm.merge.enabled=false`），先上线正确的版本。
合并是优化，不是功能，不能为它牺牲判决正确性。

## 4. 调优基线（本期只留口径，不做调优）

为后续独立的调优任务预先固定量化口径，保证调优前后可比：

| 指标 | 定义 |
|---|---|
| 端到端墙钟 | 任务 started_at → finished_at |
| VLM 段墙钟 | 第一次 VLM 请求发出 → 最后一次返回（**不是次数×均值**） |
| 有效并发度 | Σ 请求耗时 / VLM 段墙钟 |
| 每条 episode token | 任务总 token / 参与 VLM 的 episode 数 |
| 合并节省率 | 1 − 实际请求数 / 未合并估算请求数 |
| 判决稳定性 | 同配置跑 3 次，判决完全一致的 episode 占比 |

## 5. 测试策略

| 层 | 范围 | 要求 |
|---|---|---|
| 单元测试 | A 类算法 | **v1 现有测试全部搬运并保持绿**（`curation/tests/` 约 2 万行，是最宝贵的资产） |
| 契约测试 | CLI `--json` schema、REST schema | schema 变更必须同步改测试，防止契约漂移 |
| 集成测试 | preflight → check → aggregate → export → report 全链路 | 用小数据集（8 条），CI 每次跑 |
| 对账测试 | 黄金对账 | 手动触发（要真 VLM 和真数据），发版前必跑 |
| 前端 | 关键交互（表单校验、状态按钮禁用、深链解析） | 组件测试 + 少量 E2E |

⚠️ v1 的测试里有大量 UI 测试（`test_ui_manifest.py` 4290 行等），它们测的是 Gradio 层，
**随 Gradio 一起退役**。但其中对**数据整形逻辑**的测试要挑出来保留 —— 那部分逻辑
会迁移到 Daemon 的报告聚合里。挑拣工作是工作包 W1 的一部分，不能一删了之。
