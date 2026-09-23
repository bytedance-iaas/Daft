# 10 搬运计划与黄金对账

> 这一篇管一件事：**怎么保证重构之后，八个模块的判决和 v1 一模一样。**
> 需求原话：「在 port 的过程中，一定要保证现有实现的完整一致性。实现的思路和算法不能有任何改动。」

## 1. Baseline 清理

分支：`feat/curator-v2`（从 `release_v1` 的 `45bdf9292` 创建）。

**冻结点**（D34）：搬运与黄金基线都以 `45bdf9292`（2026-09-19，#157 合入后的头部）为准。
之后 release_v1 上的改动分批同步：A 类文件按 diff 原样同步进 v2，并重新生成受影响模块的基线；
B 类文件按意图移植到对应的 stage。新的输入格式（如开发中的 mcap / lance 读取）按 `.rrd` 的办法处理：
代码照搬、默认关闭、预检判为不支持（D6）。每次同步在 `claude-progress.txt` 里记下同步到了哪个提交。

**冻结点前移**（D44，2026-09-23）：冻结点改为 `dev` 的 `eb637ba40`（2026-09-22，PR #155「mcap 与 lance 格式的质检」
合入后的头部；内容提交 `0b89bcb45`）。`dev` 比 `45bdf9292` 只多这一个补丁，`release_v1` 此后没有新提交。
同步方式（F6.5）：A 类文件与 v2 没动过的文件逐字取新版（`ingest/mcap_reader.py`、`ingest/lance_reader.py` 两个新 A 类文件、
`ingest/lerobot_reader.py`、`export/mcap_writer.py`、`export/report.py`、`pipeline/default.yaml`、`ui/runner.py`、v1 的三份测试）；
v2 改过的 B 类文件三方合并、v2 的改动一处不丢（`pipeline/run.py`、`pipeline/rejudge.py`、`cli/legacy.py`）。
mcap / lance 不再照 `.rrd` 的办法处理，而是全量接入 v2（预检、快照、质检、交付，含 TOS 上的数据，见 02 篇 §2 与 §3.1、05 篇 §3、06 篇 §1.1）；
`.rrd` 维持原样。补丁对 LeRobot 数据集的判决没有影响，合成数据集上新冻结点的 v1 与 v2 逐位对账照样全过。
`tools/parity/manifest.py` 的 `DEFAULT_COMMIT` 与 `v1_manifest.json` 随之更新，A 类清单多了两个文件（共 59 个）。

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

执行情况（2026-09-20）：①② 已完成，实际落点：`backend/curation`（v1 的测试仍在包内 `backend/curation/tests`，免得改几百处 import）、`backend/pyproject.toml`、`backend/requirements.txt`、`backend/scripts`、`deploy/Dockerfile`（构建上下文改为仓库根）、`docs/design`、`docs/contracts`、`frontend/`、`tools/parity`；v1 的使用文档与发布说明归档到 `docs/v1/`。
③ 与原计划有一处出入：删掉的是 Gradio 界面（`ui/app.py`）、内嵌终端（`ui/terminal.py` 与 xterm 资产）和 `curation ui` 入口；`ui/` 里不依赖 Gradio 的 `auth` / `runner` / `manifest` / `episode_detail` / `reaper` 暂留，作为 W4、W10 的移植参考，连同它们的逻辑测试一起保留，移植完再整包删除。依赖界面的 134 条测试随之下线，逐条去向见 `docs/v1/retired-ui-tests.md`。改完用对账工具验证：工作区的 v1 回放冻结点的录制带，九项逐位一致。

**为什么从 `release_v1` 派生而不是空分支**：保留 git blame。搬运算法时，
每个阈值都能追回它是哪次实测定的（代码注释里全是这类依据）。真出了对账不一致，
blame 是最快的排查路径。最终产物一样：一个不含上游 daft 的分支。

⚠️ 第 ① 步是破坏性操作，执行前需要需求方点头 —— 已于 2026-09-20 同意：在新分支上开发，
上游 daft 源码与不再需要的代码都可以删。

需求原文写的是「在 main 下新建一个 branch」。实际从 `release_v1` 派生，是因为 v1 的代码只在那条分支上，
`main` 上没有。这是与需求原文的一处出入，记在这里备查。

## 2. 三类代码的搬运策略

| 类别 | 范围 | 策略 |
|---|---|---|
| **A 原样搬运** | `core/*`（含 `checks/`、`contract.py`、`task_type.py`）、`registry/*`、`ingest/*`（含数据集语义 profile、动作语义预检、schema 校验、公共数据集目录，D44 起还有 mcap / lance 两个读取器）、`export/lerobot_writer.py`、`export/safe_write.py`、`pipeline/verdict.py`、`dataset_level/*`、`episode_select.py`、`vlm_call_kinds.py` | **逐字复制，连中文注释一起**。只允许改 import 路径。任何其他改动都要在 PR 里单独说明理由 |
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
| `rejudge --retry-abstained` | 并入「重试」：调用失败的条目在 v2 里是「执行出错、待补跑」，重试补跑的就是它们 |
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
| 界面并发三个输入框 | 改为高级设置里的两个**上限**（CPU 并发、VLM 并行度，D31）；实际取值和执行计划仍由 planner 决定，模型并行度在密钥管理里配 |
| 同步证据图模式（flagged / all / off）、证据帧模式 | 模块参数 `video_action_sync.sync_plots` / `task_success.evidence_frames`，取值同 v1 |
| 报告页「质检批次」下拉 | 不需要：一个任务就是一个批次；同一交付目录下的其它任务在任务列表里按交付目录筛选 |
| 人工裁决随交付目录跨批次沿用 | **不保留**（D32）。裁决只属于产生它的任务 |
| `latest` =「最近跑的是哪一次」 | 改为「最近一次发布成功的完整版本」（D29） |
| `.rrd` 输入 | 维持 v1 现状：代码原样搬运，默认关闭（`ingest.rrd_enabled: false`），预检按「不支持的格式」处理 |
| mcap / lance 输入（D44） | 全量接入：读取器与 `export/mcap_writer.py` 原样搬运，开关 `ingest.mcap_enabled` / `ingest.lance_enabled` 照 v1 默认开；预检识别、快照、质检、交付都支持，TOS 上的数据先拉到本地缓存再读（02 篇 §2 与 §3.1、05 篇 §3）；交付照 v1：mcap 是 `mcap_curated/` 逐字节拷贝 + `index.json`，lance 是 `episodes_parquet/`（原格式交付未做） |
| 本地路径 / FSX 挂载作输入输出 | 输入保留为 experimental；交付目录只支持 `tos://` |

## 3. 黄金对账

### 3.0 与 v1 的有意差异

「完整一致」说的是质检算法。下面几处是产品层面有意改的，对账时不算差异，但要逐条知道：

| 差异 | v1 | v2 | 对账怎么处理 |
|---|---|---|---|
| 调用失败的条目（D24） | 记成弃权，照常交付并进复核 | 记为执行出错，暂不交付，等补跑 | 逐模块的规范化记录照比；终判清单的比对**排除**两边任何一侧出过错的条目，并单独列出 |
| 降级得出的结论（D33） | 打分失败后复核救回、仲裁失败维持弃权、少一路机位照判，结论照常生效 | 同样算执行出错，整条待补跑 | 同上；黄金基线要求零失败，所以基线里不会有这类条目 |
| 无标注补 caption 失败（D33） | 给空串，成败判定拿空任务文本照跑 | 执行出错，不进后面的档 | 同上 |
| 被去重剔除的条目 | `passed.json` 按漏斗判决生成，没扣掉它（同时也在 `reject.json` 里）；交付数据集里是扣掉的 | 只在 `reject` 里 | 终判清单按交付口径比：passed = 漏斗 keep − 重复项（W0 实测确认） |
| 执行裁决之后（D9） | 顺手重新导出 | 只改判决与报告，导出由用户显式触发 | 裁决对账比的是三份清单与改标结果，不比导出时机 |
| 人工裁决的归属（D32） | 随交付目录跨批次沿用 | 只属于本任务 | 不对账；v1 的沿用逻辑不搬 |
| 完整性标志与 `latest`（D7、D29） | `passed.json` 兼作标志；`latest` = 最近一次跑批 | `_COMPLETE`；`latest` = 最近一次发布成功的完整版本 | 不对账 |
| 思考参数、Token 采集 | 没有 | 有，默认不传思考参数 | 对账固定不传 |
| 改标重判（D39） | `rejudge` 只跑多视角打分 + 逐机位复核两层 | 默认同 v1；执行裁决时可选首轮的完整流程 | 默认口径逐位对账（回放）；选了完整流程的不要求与 v1 一致 |
| 源文件缺失的条目（D40） | 静默剔除，不进任何清单和计数 | 同样剔除；报告列出哪几条、缺什么，汇总给条数 | 三份清单照比；v2 多出的跳过清单不比 |
| 技能画像整个模块失败（D41） | 不挡交付，画像小节标失败 | 全部待补跑，一条都不交付，等重试 | 基线要求零失败，不会出现；不对账 |
| 改标之后又重新提交另一段标注 | 沿用之前给的成败结论，不重判 | 之前顺手给的成败结论作废，按新标注重判（follow-up 的作废规则） | 裁决对账只覆盖改标这一条线；成败结论与复议对清单的影响由 CLI 测试钉住 |
| 重复项的人工处理（D42） | 重复项若有成败弃权，照样进成败裁决，人判成功就写回 `passed`、交付；复议只认任务成败判定的拒绝 | 已被拒绝的条目不再问成败；重复项可在「被拒复议」里恢复为可用 | 裁决对账排除重复项和被恢复的重复项，单独列出 |
| mcap / lance 的语义样本（D44） | 一次跑完，所选的前 100 条 | 每档只读幸存者，Daemon 另传整个任务的所选（`--selection`），同样取前 100 条 | 合成数据的 mcap / lance 两份上 v1 对 v2 回放逐位一致（`tools/parity/tests/test_containers_parity.py`） |
| mcap / lance 交付里自产描述的来源用词（D44） | `自产caption补标` | `自产caption`（与 v2 其他交付一致） | 交付清单不对账 |
| mcap / lance 交付的形态（D44） | 每种格式都另写一份 `episodes_parquet/`；mcap 再交 `mcap_curated/` | mcap 只交 `mcap_curated/`（与 LeRobot 源只交 `lerobot_curated/` 一致）；lance 交 `episodes_parquet/` 与 `videos/` | 不对账 |

### 3.1 基线

**两个数据集，两个都要过**（D19）：

| 基线 | 规模 | 覆盖到的 | 覆盖不到的 |
|---|---|---|---|
| `umi_640_notask` | 640 条，先 64 条跑通流程 | 大规模下的稳定性；**全无标注**路径（autolabel 全量兜底）；纯腕部相机的仲裁线；umi 的数据集语义 profile（rot6d、夹爪极性反转） | 运动学极限（umi 不在规格库，整项跳过）；原始标注路径、判废护栏、标注分歧；外部机位仲裁线；运动质量的卡死 / 饱和子项（umi 的 action 与 state 同源，自弃权） |
| `droid_lerobot` 前 50 条 | 50 条（使用文档演示的同一批） | 运动学极限（franka 在规格库）；有 / 无标注混合（约 28 / 22）；三机位含外部机位；判废护栏、取证仲裁、标注分歧与三条裁决线 | 大规模 |

**已知缺口：v3 格式本期不做对账**（需求方已确认）。两个基线的源格式如果都是 LeRobot v2，
v3 的读取和导出路径（多条拼接的 parquet / mp4、按 chunk 重编码）就只靠搬运过来的 v1 单元测试保障。
增量导出的 v3 分支是新写的代码，风险比 v2 分支高，W7 的验收里单独用一个小的 v3 夹具跑通
「可被官方 loader 无警告加载」，但不和 v1 逐位比。

确定性六项里，去重这一行要先确认基线里真有字节级重复的条目，没有的话这一行是空转 ——
在 CI 的迷你数据集里复制两条 episode 来补。

基线怎么生成：

- **在哪跑**：现网 v1 的 Pod 里（linux/amd64，库版本由镜像钉死，数据与方舟都现成，内网读 TOS）。
  解码时的 RGB 转换（swscale）与缩放（`cv2.resize`）跨 CPU 架构不保证逐位一致，所以基线和 v2 的对账跑批
  必须在同一平台、同一套库版本下进行；在 Mac 上生成的基线只能在 Mac 上用。
  Pod 里的镜像可能比冻结点旧，所以 `dump_v1.py` 自带冻结点的 v1 源码，只借用镜像里的 Python 依赖。
- 用 v1（冻结点 `eb637ba40`，D44；D34 时是 `45bdf9292`，两者对 LeRobot 数据集的判决相同）跑完整质检，交付目录整个存档为 `golden/v1/<数据集>/`。
  这一步在动任何 B 类代码之前做，**基线必须由 v1 的代码生成**。
- v1 的交付目录里**没有**逐模块逐条的原始结果文件（没有 `results.jsonl`，也没有 `report.json`），
  明细 CSV 还是给人看的、可能取过整。所以另写一个 `tools/parity/dump_v1.py`：在同一个进程里跑 v1 原版的
  `run_pipeline`，靠挂钩取数，把每项检查返回的 `CheckResult`（未经报告格式化）逐条导出成**规范化记录**
  —— 与 v2 `checks/<module>/results.jsonl` 同一个 schema。对账比的是两边的规范化记录。
  这个脚本只读不改 v1 的任何文件，启动时校验 v1 源码与冻结点逐文件一致。三处挂钩：
  - 截 `DataFrame.collect`：v1 的逐条结果里，被硬门拦下的条目只剩拦下它的那一项，其余检查的结果在漏斗过滤时就丢了，
    要在过滤之前截下来；
  - 包 `hedged_request` 与 `requests.post`：录下每次逻辑调用的请求与响应（对冲只认赢的那一发）；
  - 截报告装配的入参：拿到去重组、技能体系与标注分歧队列。
- **零失败**：基线跑批中只要有一次模型调用在重试后仍失败，这份基线就不收 —— 按 D24、D33，v2 会把这些条目
  挡在后面的档之外，下游的请求随之变化，回放必然对不上。不整批重跑，而是「回放 + 补录」再跑一遍：
  录过的请求直接回放，只对失败的和因此新出现的请求真调模型，直到一次失败都没有。
- **先在 v1 自己身上验证回放**：基线录好后，接回放后端把 v1 再跑一遍，要求请求 100% 命中、全部规范化记录逐位一致。
  这一步不花 token，在动任何重构之前就证明「回放对账」这条路走得通。
- **VLM 三项先测 v1 自己的波动**：同一基线用 v1 真调模型跑两遍，两遍之间的判决差异就是噪声底。
  模型输出本身不可复现，v2 对 v1 的差异只要落在噪声底附近就不是搬运问题；没有这条参照，2% 的门槛无从解释。
  为省 token：droid 前 50 条跑两遍；umi 640 条全量跑一遍作基线，噪声底用前 64 条另跑两遍。
- 固定条件：同一个模型版本（`doubao-seed-2-0-pro-260215`）、**不传思考参数**（v1 从不传）、
  同一份流水线配置（现网的站点配置）、同样开对冲。
- **存放**：存档放 TOS，仓库里只放文件清单和 sha256。录制只存提示词全文、每张图的 sha256、模型参数和响应，不存图本身。

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

**VLM 三项先做回放对账，再做真模型比对。** 模型输出不可复现，拿两次真实调用的结果去比，只能比到「差不多」。
要验证的其实是另一件事：v2 发给模型的东西和 v1 是不是一模一样。所以：

1. `dump_v1.py` 跑基线时，把 v1 的每一次 VLM 请求（请求体的规范化哈希：提示词全文 + 每张图的字节哈希 + 模型参数）
   和对应的响应录下来。
2. v2 接一个**回放后端**跑同一基线：请求哈希命中就回放当时的响应，没命中就记一条「请求不一致」并点名是哪个模块、
   哪条 episode、哪类调用。
3. 验收：请求 100% 命中；在回放的响应下，`task_success` 与 `skill_profile` 的规范化记录与 v1 **逐位一致**。
   提示词动了一个字、抽帧差了一帧、JPEG 参数变了，都会在第 2 步现形。

回放过了，再用真模型跑一遍做统计比对（下表的容差），确认端到端没有别的问题。
前提是两边的解码与 JPEG 编码库版本一致（镜像里钉死）；不一致时图的字节会变，那种情况下回放对账退化为只比提示词。

「逐位一致」= 规范化记录按 `episode_index` 排序后，浮点数按 `repr()` 精确比较。
确定性六项不允许任何差异 —— 它们是确定性计算，有差异就是搬运出了 bug。
`dedup` 要固定输入再比，是因为它吃的是判决后的 keep 集合，而 keep 集合受 VLM 判决的随机性影响。

VLM 三项有随机性（实测：方舟 temperature=0 同一条打 5 次可得 5 种 caption），
所以口径是**判决级比对 + 人工过目差异条**，不是文本级比对。

### 3.3 对账工具

```bash
# 在现网 v1 Pod 里跑（步骤见 tools/parity/README.md）；`--` 之后就是 v1 的 `curation run` 参数
python -m parity dump-v1 --out golden/v1/droid50-a -- run --input tos://… --output tos://… --max-episodes 50

# 回放对账：九项全部逐位一致、请求 100% 命中
python -m parity compare --golden golden/v1/droid50-a --candidate <回放导出> --all-strict

# 真模型对账：确定性六项逐位，VLM 三项判决级，带 v1 自身的噪声底
python -m parity compare --golden golden/v1/droid50-a --candidate <导出> \
                         --noise-floor golden/v1/droid50-b --json
```

默认 `--strict` 为确定性六项、`--verdict-only` 为 VLM 三项；终判清单比对时，两边任一侧 `error` 的条目排除并单列。
工具在 `tools/parity/`，
录制带的格式见 `docs/contracts/parity/vlm-tape-entry.schema.json`，v2 的回放后端读同一种格式、用同一个请求哈希函数。

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
