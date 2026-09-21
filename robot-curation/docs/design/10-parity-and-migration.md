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

⚠️ 第 ① 步是破坏性操作，**执行前需要需求方点头**。在此之前分支上不做任何删除。

需求原文写的是「在 main 下新建一个 branch」。实际从 `release_v1` 派生，是因为 v1 的代码只在那条分支上，
`main` 上没有。这是与需求原文的一处出入，记在这里备查。

## 2. 三类代码的搬运策略

| 类别 | 范围 | 策略 |
|---|---|---|
| **A 原样搬运** | `core/*`（含 `checks/`、`contract.py`、`task_type.py`）、`registry/*`、`ingest/*`（含数据集语义 profile、动作语义预检、schema 校验、公共数据集目录）、`export/lerobot_writer.py`、`export/safe_write.py`、`pipeline/verdict.py`、`dataset_level/*`、`episode_select.py`、`vlm_call_kinds.py` | **逐字复制，连中文注释一起**。只允许改 import 路径。任何其他改动都要在 PR 里单独说明理由 |
| **B 改造搬运** | `pipeline/run.py`、`pipeline/funnel.py`、`pipeline/rejudge.py`、`pipeline/reprofile.py`、`adapters/vlm_client.py`、`adapters/decode.py`、`export/publish.py`、`export/report.py`、`tos_store.py`、`delivery.py`、`fetch.py`；以及 `ui/` 里要留下的三样：`auth.py`（鉴权中间件）、`runner.py` 的深链与地址解析函数、`manifest.py` 的数据整形函数 | 拆成 stage / 加 usage 采集 / 加增量导出 / 从 Gradio 里剥出来。**算法调用顺序和参数不变**，只改编排外壳 |
| **C 全新编写** | `cli/`、`daemon/`、`frontend/`、`deploy/charts/` | 英文注释，按本册契约实现 |

### 2.1 A 类的保护机制

在 CI 加一条检查：A 类文件的内容哈希如果变化，必须在 PR 描述里带 `parity-change:` 标记说明。
防止重构过程中「顺手优化」掉一行算法。

### 2.2 B 类里最危险的三处

1. **`funnel.py` 的并发闸门**：必须继续用自建信号量，**不能换成 daft 的 `max_concurrency`**
   （对 async 行级 UDF 静默失效）。
2. **`vlm_client.py` 的对冲补发**：hedging 逻辑和延迟统计口径（7 元组行、epoch 时间戳）
   原样保留，只**新增** usage 采集。
3. **`rejudge.py` 的裁决优先级**：「整条弃用压过成败裁决」「改了标但已有人工成败结论的不重跑模型」
   这类规则藏在代码顺序里，拆分时极易丢失。拆之前先把现有单测跑通，拆之后必须全绿。
4. **`run.py` 里的先后顺序**：无标注补 caption 在漏斗之前、对全部输入；去重和技能画像在判决之后、
   只对 keep 集合；弃权条目算 keep。这些顺序决定了每个模块「看到的是哪些条目」，
   拆成 stage 时顺序一变，去重结果和技能体系就跟着变，对账必然不过。
5. **`funnel.py` 帧档的一次解码两用**：同步检查吃全帧率、视觉质量从同一批帧抽稀、逐机位串行、用完即释放。
   拆成 `check --modules visual_quality,video_action_sync` 时要保住这个结构，不能改成先落盘再各读各的。

### 2.3 v1 能力去向表

v1 的 CLI 和界面上的每一项能力，在 v2 里去哪了。原则：需求说「原 CLI 可以作为功能参考，不一定需要完整对齐」，
所以不是都要留，但每一项都要有交代。

| v1 能力 | v2 去向 |
|---|---|
| `curation run` 一口气跑完 | 拆成原子命令，由 Daemon 编排；对账基线用 `release_v1` 上的原版跑 |
| `curation rejudge` | 拆成 `adjudicate-apply` + 重跑 + `aggregate` + 画像同步，由「执行裁决」子任务编排；**不再自动重新导出**（D9） |
| `rejudge --retry-abstained` | 并入「重试」的默认范围（只重跑调用失败的条目） |
| `reprofile`（隐藏命令） | 保留为内部步骤：`check --modules skill_profile --incremental` |
| `--lite` / 界面「快速质检」 | 新建页的「快速质检」预设（只勾不调模型的六项） |
| `--only` / `--skip` / 界面「自选模块」 | 任务的 `modules` 列表 |
| `--max-episodes` / `--episodes` | episode 选择的「前 N 条」/「自选」 |
| `--report-only` | 任务参数 `export=false`，之后可用「导出」补做 |
| `--batch` / 界面多选数据集 | `POST /tasks/batch`，一个数据集一个任务 |
| `--set` / `--config` | CLI 保留；任务级参数走 `params` 和 `modules[].params`，站点级走 ConfigMap |
| `--vlm-backend` 预设（站点配置维护） | 改为用户在「密钥与资源管理」里自己维护的 VLM 后端 |
| `curation backends` | `curation backends probe`；界面上是后端的「校验」与「刷新模型」 |
| `curation public` | `curation datasets list --source public` |
| `curation fetch` | 原样保留，CLI-only。终端下线后经 `kubectl exec` 使用 |
| `curation ls` / `prune` | 原样保留 |
| `curation review-page`（静态审片站）、`/review` 挂载 | 静态站下线，裁决与逐条查看都在新前端里；片段生成拆成 `curation clips` |
| `curation ui`、内嵌终端 | 删除 |
| 界面并发三个输入框 | 删除。并发由 planner 决定（D5），模型并行度在密钥管理里配 |
| 同步证据图模式（flagged / all / none）、证据帧模式 | 模块参数 `sync_plots` / `evidence_frames` |
| 报告页「质检批次」下拉 | 不需要：一个任务就是一个批次；同一交付目录下的其它任务在任务列表里按交付目录筛选 |
| `.rrd` 输入 | 维持 v1 现状：代码原样搬运，默认关闭（`ingest.rrd_enabled: false`），预检按「不支持的格式」处理 |
| 本地路径 / FSX 挂载作输入输出 | 输入保留为 experimental；交付目录只支持 `tos://` |

## 3. 黄金对账

### 3.1 基线

**两个数据集，两个都要过**（D19）：

| 基线 | 规模 | 覆盖到的 | 覆盖不到的 |
|---|---|---|---|
| `umi_640_notask` | 640 条，先 64 条跑通流程 | 大规模下的稳定性；**全无标注**路径（autolabel 全量兜底）；纯腕部相机的仲裁线；umi 的数据集语义 profile（rot6d、夹爪极性反转） | 运动学极限（umi 不在规格库，整项跳过）；原始标注路径、判废护栏、标注分歧；外部机位仲裁线；运动质量的卡死 / 饱和子项（umi 的 action 与 state 同源，自弃权） |
| `droid_lerobot` 前 50 条 | 50 条（使用文档演示的同一批） | 运动学极限（franka 在规格库）；有 / 无标注混合（约 28 / 22）；三机位含外部机位；判废护栏、取证仲裁、标注分歧与三条裁决线 | 大规模 |

基线怎么生成：

- 用 v1（`release_v1` 检出）跑完整质检，交付目录整个存档为 `golden/v1/<数据集>/`。
  这一步在动任何重构代码之前做，**基线必须由 v1 的代码生成**。
- v1 的交付目录里**没有**逐模块逐条的原始结果文件（没有 `results.jsonl`，也没有 `report.json`），
  明细 CSV 还是给人看的、可能取过整。所以另写一个 `tools/parity/dump_v1.py`：在 `release_v1` 检出上
  import v1 的内部函数，把每项检查返回的 `CheckResult`（未经报告格式化）逐条导出成**规范化记录**
  —— 与 v2 `checks/<module>/results.jsonl` 同一个 schema。对账比的是两边的规范化记录。
  这个脚本只读不改 v1 的任何文件。
- **VLM 三项先测 v1 自己的波动**：同一基线用 v1 跑两遍，两遍之间的判决差异就是噪声底。
  模型输出本身不可复现，v2 对 v1 的差异只要落在噪声底附近就不是搬运问题；没有这条参照，2% 的门槛无从解释。
- 固定条件：同一个模型版本（`doubao-seed-2-0-pro-260215`）、**不传思考参数**（v1 从不传）、
  同一份流水线配置、同样开对冲。

### 3.2 对账口径

| 项 | 口径 | 容差 |
|---|---|---|
| `timestamp_check` | 每条 episode 的 verdict + 所有数值字段 | **逐位一致** |
| `kinematic_limits` | 同上（只在 droid 基线上有意义） | **逐位一致** |
| `motion_quality` | 同上（含各子分与子项适用性） | **逐位一致** |
| `visual_quality` | 同上（逐相机分数） | **逐位一致** |
| `video_action_sync` | 同上（逐相机 lag、相关峰值、相机标注） | **逐位一致** |
| `dedup` | 重复组划分；**输入集合固定为 golden 的 keep 名单**再比 | **逐位一致** |
| 无标注补 caption | 有 / 无 caption、`unclear` 的条数 | 不比文本；条数差异逐条列出 |
| `task_success` | 每条 episode 的终判（pass / fail / abstain）与走到的判据（`detail.rules`） | 差异 < 2% 且不超过 v1 自身噪声底的 1.5 倍，**逐条列出并人工确认** |
| `skill_profile` | 每条有无归族、标注分歧队列的成员（不比族名，族名每次归纳都不同） | 同上 |
| 人工裁决（droid 基线） | 同一组裁决输入，执行后三份清单与改标结果 | **逐位一致**（裁决逻辑不调模型的部分） |

「逐位一致」= 规范化记录按 `episode_index` 排序后，浮点数按 `repr()` 精确比较。
确定性六项不允许任何差异 —— 它们是确定性计算，有差异就是搬运出了 bug。
`dedup` 要固定输入再比，是因为它吃的是判决后的 keep 集合，而 keep 集合受 VLM 判决的随机性影响。

VLM 三项有随机性（实测：方舟 temperature=0 同一条打 5 次可得 5 种 caption），
所以口径是**判决级比对 + 人工过目差异条**，不是文本级比对。

### 3.3 对账工具

```bash
python tools/parity/dump_v1.py --input tos://… --out golden/v1/droid50/     # 在 release_v1 检出上跑

curation-parity compare --golden golden/v1/droid50 --candidate <run_dir> \
                        --strict timestamp_check,kinematic_limits,motion_quality,\
visual_quality,video_action_sync,dedup \
                        --verdict-only autolabel,task_success,skill_profile \
                        --noise-floor golden/v1/droid50-rerun --json
```

输出：逐模块一致/不一致计数、差异条目明细、结论。**这个工具是工作包 W0 的一部分，
必须先于重构存在** —— 没有对账工具的重构是在裸奔。

### 3.4 合并优化的对账（单独一关）

VLM 请求合并会改变模型的输入形态，任何模块在被允许合并之前必须单独验证：

```
同一批 episode（建议 64 条），同一模型、同样不传思考参数：
   A. 单发模式（每模块一个 request）→ 判决集合 A
   B. 合并模式（同 episode 多模块合并）→ 判决集合 B
验收：A 与 B 的判决一致率 ≥ 98%，且不一致条目人工过目确认无系统性偏差
```

本期现有两个 VLM 模块不参与合并（D23），所以这一关用**示例模块**跑通流程、验证工具链：
两个帧策略相同的「抽 8 帧、问一次」的测试模块，走真实的方舟端点。它证明的是框架
（合并、拆回、降级、摊派）工作正常，以及这套对账流程可执行；将来每接入一个要参与合并的新模块，
都要用它自己的数据再过一遍这一关。

不达标就**关闭合并**（配置开关 `vlm.merge.enabled=false`）。
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
| 集成测试 | preflight → autolabel → check → aggregate → export → report → verify 全链路 | CI 每次跑，**不联网、不要任何密钥**：数据用脚本生成的迷你 LeRobot v2 数据集（8 条、合成视频，脚本进仓库、数据不进），VLM 用录制回放的假后端 |
| 对账测试 | 黄金对账 | 手动触发（要真 VLM 和真数据），发版前必跑 |
| 前端 | 关键交互（表单校验、状态按钮禁用、深链解析） | 组件测试 + 少量 E2E |

⚠️ v1 的测试里有大量 UI 测试（`test_ui_manifest.py` 4290 行等），它们测的是 Gradio 层，
**随 Gradio 一起退役**。但其中对**数据整形逻辑**的测试要挑出来保留 —— 那部分逻辑
会迁移到 Daemon 的报告聚合里。挑拣工作是工作包 W1 的一部分，不能一删了之。

### 5.1 测试用的真实凭证

手动对账要用现网的 TOS 访问密钥和方舟 API Key。需求文档里贴着一组明文的，处理原则：

- 只经本地 `.env`（已在 `.gitignore`）或 K8s Secret 注入；**不进仓库、不进任何设计文档、不进 CI 日志**。
- 契约测试和集成测试一律不依赖它们。
- 建议项目收尾后轮换这组密钥 —— 它们在一篇多人可见的文档里出现过。
