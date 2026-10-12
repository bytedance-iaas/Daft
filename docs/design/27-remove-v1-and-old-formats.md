# 27 · 删掉 v1 与旧格式

> 状态：**实施稿 v1.0（2026-10-11）**。D89–D91 登记在 00 §7；账本阶段 17（F17.0–F17.5）。
> 来源：需求方 2026-10-11——「我们现在是开发阶段，不用考虑旧任务。如果可以删就删掉，不要考虑太多历史包袱和兼容性」；阶段 16 收尾时列出的遗留
> （1.0 格式的读法、模拟世界里的旧任务故事、v1 的 `curation run`）——「旧东西都删掉吧」；v1 的子命令里「run / rejudge / review-page / backends / public
> 可以删，prune / ls / fetch 可以留着」；回归样本打分「删，之后补 2.0 基线」。
> 取代：D59（旧任务按旧格式只读打开）；设计 10 §2.3 里 v1 子命令的去向；设计 17 §5.6、§6.1 里为 1.0 留的读法。

## 0. 开工指引

1. **不留兼容**：删掉的东西不留退役标记、不留读旧数据的分支。本机与集群上已有的旧任务（C2 1.0、漏斗计划、注册表 6.0 以前）打开会报错——开发阶段可接受，
   部署说明里写一句「升级后清掉旧任务」。
2. **顺序**（每一步 CI 都能绿）：v1 代码（F17.1）→ 对账与回归工具（F17.2）→ 契约与后端的旧格式（F17.3）→ 前端（F17.4）→ 文档（F17.5）。
3. **不动的**：A 类文件（设计 10 §2）里只剩 v1 在调的函数，文件冻结、函数留着；整份只有 v1 用的 A 类文件删掉，在 `a_class_declared.json` 登记
   `removed`。daft 仍是依赖（v2 的行读取 `pipeline/rows.py` → `ingest/daft_source.py`）。2.0 报告里沿用 1.0 叫法的统计（模块小节的 `counts`、
   明细表的 `verdict` 列、前端把 2.0 记录换算成判定词来画图）是今天的输出，不是兼容，不在这一期。D45 的旧 id 写法、D54 退役的站点配置键也不在这一期。

## 1. v1 代码（D89）

**删**：`cli/legacy.py`（v1 命令行）与它带的 `run`、`rejudge`、`review-page`、`backends`、`public`；只为它们存在的代码——`pipeline/run.py`、
`pipeline/rejudge.py`、`ui/`、`pipeline/streaming.py`、`frame_cache.py`、`checkpoint.py`、`dedup_execution.py`、`isolation.py`、`progress.py`、
`export/review_page.py`、`task_trace.py`、`timeline.py`、`retired.py`、`detail_labels.py`、`episode_stats.py`、`adapters/vlm_server.py`、
`vlm_watchdog.py`、`frames.py`；共享文件里只有 v1 走得到的函数（`pipeline/funnel.py` 的 `run_funnel` 一系与 v2 走不到的帧探针判定路径、
`adapters/vlm_client.py` 的 v1 工厂、`export/report.py` 除 `container_findings` 以外的全部、`export/sync_plots.py` 的出图、
`export/evidence.py` 的审片片段、`tos_store.py` 里 v1 的镜像与暂存）；整份只有 v1 用的 A 类文件（`pipeline/verdict.py`、
`dataset_level/caption.py`、`decisions.py`、`stuck_events.py`、`ingest/schema.py`、`registry/fk.py`，以及帧探针判定的
`core/checks/task_success.py`、`camera_view.py`、`core/task_type.py`）；v1 专用的配置键（`skill_profile.*`、`checks.task_success.arbitration.*`、
`verdict.soft_threshold`、`checks.*.gate / weight`、`export.*`、`dedup`、v1 的超时键）。v1 的单测与 `tests/optimizations/` 一起删，共享部分的
用例挪走。`ingest/rrd_reader.py`（连同 `ingest.rrd_*` 与依赖 `rerun-sdk`）留着：A 类的 `lerobot_reader.py` 靠它认出并拒掉 `.rrd` 输入。

**留下、改成 v2 的运维子命令**：`curation ls`、`curation prune`、`curation fetch`。

| 命令 | 做什么 | 改动 |
|---|---|---|
| `ls <地址>` | 列本地目录或 `tos://桶/前缀` 下一层（先目录后文件，带大小），凭证用本实例的 TOS 密钥 | 原样搬进 `cli/ops.py` |
| `prune <交付目录> [--keep-latest N --yes]` | 列一份交付目录下的历次跑批（时间、条数、占用），按需删掉旧的；`latest` 指的那次永远留 | 改认 v2 的交付布局：跑批是 `<run_id>/`，条数取最新结果版本 `report.json` 的总览；本地与 `tos://` 都能列能删 |
| `fetch --source --ref` | 经 oniond 从内网 HuggingFace 缓存桶把公开数据集下载、逐文件校验后拷进本站数据集根 | 原样搬进 `cli/ops.py`，镜像里的 onion-ai-data 层留着 |

三个命令是给人在终端里用的：输出是中文文字、不进 C2、Daemon 不调用；退出码沿用 0 / 1（远端错误）/ 2（输入错误）。
`curation --help` 里它们列在「运维」一组，和流水线的原子命令分开。

## 2. 旧格式与旧任务兼容（D90）

- **C2 只有 2.0**：结果记录、`check` 输出、判决行、终判清单、报告、执行计划、`aggregate` 输出删掉 1.0 分支（`oneOf` 与 `*_1` 定义）与 1.0 示例；
  计划删掉 `hard_gates`、`survivors:` / `keep` / `keep-minus-duplicates`、`autolabel`、`phase: funnel`、`full_set`、`bound_by: site`；
  `aggregate` 只有 `--phase final`；`decisions.json` 与执行裁决删掉 `relabel_rerun`（D71 起两种口径是同一次判定）。
- **C4**：删掉流水线行的 `last_stage` / `next_stage`（漏斗行）、`StageProgress.full_set`、`ModuleRegistry.full_set_stages`、`RegistryStage` 的
  `autolabel` / `profile`、`FindingCode.retired`、退役的上传种类、`EpisodeNote` 的 1.0 原因类型、执行裁决的 `relabel_rerun`；说明里「旧任务」「漏斗」
  的写法一并删。
- **CLI / 内核**：`records.py` 的 1.0 读法（`upgrade_record`、`is_v2`、`passes_funnel`、`two_blocks`）；`check` 的漏斗模式（没有计划的运行目录
  也按两块走：每条判完就交给下一段）；`episode_state.py` 的漏斗表与旧库迁移；`aggregate.py` 的漏斗阶段与 `LegacyRun`；`policy.py` 按注册表版本
  回退的缺省级别与「策略版本 1」；`findings.py` 为旧记录派生发现的分支（D73 以前的标注冲突、D72 以前的自产描述、EEF 5.0 以前的判定）；
  `reporting.py` 里 D69 以前的交付、EEF 5.0 以前的小节；注册表里 `retired` 的 EEF 细码、`gripper_calibration` / `record_mapping` 两个退役参数
  （声明取代了它们，设计 25）与 `FULL_SET_STAGES`。
- **Daemon**：`legacy_task` 检查（旧任务直接按新格式读，读不了就报错）、全量段的派发、`autolabel` 段的跳过、漏斗的批次路径、`live.py` 的漏斗行、
  `revision.py` 的 `label_audit.json`、执行裁决的 `relabel_rerun`、`reexport` 子任务（D69）。
- **前端**：生成的类型只剩 2.0，报告页、Episode 明细、裁决页、任务详情都按 2.0 写；删掉 1.0 报告的门标签与小节、`isReportV2` 分支、
  策略版本 1、漏斗行、执行裁决的口径选项、旧阶段名（补描述、判决、技能画像、导出）；模拟世界的主任务「droid 前 50 条质检」改成 2.0 的故事
  （两块并行的那个任务并进来）。

## 3. 工具（D91）

- **对账工具**：删 `dump-v1`、`v1-manifest`、`v1-src`、`pack`、`archive`、`fetch` 与 `dump_v1.py`、`manifest.py`、`archive.py`、
  `v1_manifest.json`；`compare` 只读 v2 运行目录（删 v1 dump 的读法、v1 重判的黄金、噪声底）；`records.py` 删 1.0 记录；`vlm_tape.py` 删
  `replay-record` 与 v1 的文字调用钩子；`fakevlm.py` 删技能画像的答法。A 类守卫改用自己的清单 `a_class_manifest.json`（从 `v1_manifest.json`
  里摘出 A 类条目的冻结哈希），`a_class_declared.json` 照旧；测试不再从 git 里取冻结时的 v1 源码，CI 也不必拉全历史。
- **回归样本工具**：`score.py` 只读 2.0 运行目录（删 1.0 的规则引擎与 `verdicts.jsonl` 的读法）；`finding_map.json` 只留对照项与摄入项；
  分类表 1.3 → 2.0 的改号保留（TOS 上的样本集期望值还是 1.3）。平台基线 `baseline/1b30fb224` 是 1.0，作废；2.0 基线之后在集群上对样本集重跑一次再录。

## 4. 验收（F17.5）

1. 代码、契约、测试、文档里不再有 v1 的 `curation run` / `rejudge` / `ui`、C2 1.0、漏斗格式与 §2 列的旧任务兼容（A 类文件里冻结的函数除外）。
2. `curation ls`、`prune`、`fetch` 照手动验证步骤能跑；`prune` 能列、能删一份 v2 交付目录的旧跑批，`latest` 那次不删。
3. 后端各套、契约、前端五项、对账回放、回归样本工具全过；CI 四项全绿。
4. 本机真 Daemon 跑一个任务：详情、报告、Episode 明细、人工裁决、执行裁决照常。

## 5. 落地记录（F17.1–F17.4，2026-10-11）

照 §1–§3 删完之后，顺着删掉的东西又收了几处，记在这里：

- **注册表 7.0**：任务成败判定不再覆盖 TASK-10（中途失误后又完成）和 LABEL-4（标注与画面不符）。前者在一次判定（D71）里没人报，后者随判废护栏
  （D73）没了来源；两项在报告的「检测项覆盖」里标为未覆盖，回归样本工具的平台注记重新生成。
- **闸门**：VLM 段只剩 `episode` 与 `probe`，`episode` 跟着 `probe` 走（一条一次请求）；站点配置里写旧闸门名会被忽略（`planner/limits.py` 的
  `RETIRED_GATES`），04 篇 §2.2 随之改写。调用类型的显示名（`curation/vlm_call_kinds.py`）只剩 `probe`（任务成败判定）和 `eef_review`（EEF 复核），
  C3 的说明去掉了 v1 的五类和 `autolabel`；超时只剩 `timeouts_s.probe`（EEF 复核另有 `timeouts_s.eef_review`）。
- **EEF**：记录比对的来源全部来自数据集声明（`semantics.pose` / `semantics.joints`），预检的 `subitems.record_consistency` 也按声明核对列与 topic；
  手持夹爪的标定只有声明里的 `calibration.handheld` 和内置的 DAS DEMO 两种来处（`derive_mcap.Derived` 去掉了上传标定的分支），控制台的轨迹来源
  写「数据集声明里的夹爪标定」。报告 EEF 一节去掉了 4.x 的判过 / 判废 / 转人工计数，控制台去掉了 5.0 以前记录的结论块。
- **`check --survivors-out`**（2026-10-12 改名 `--done-out`）：没有漏斗之后，写出的是本段每个所选模块都有记录的条（D57）。任务成败的报告统计 `layers` 只剩 `probe`、`endstate`。
- **计划的 `bound_by: site` 留着**：§2 原想删，但站点上限照样能卡住 VLM 并行度（`planner/limits.py` 写的就是 `site`），删了会让合法的计划
  过不了 Schema。预检里没人写的 `drafts`（F5.23 起记录映射不再起草）暂留在 C2 的 Schema 里，下次改预检契约时一起删。
- **站点配置**：Daemon 读站点配置时把旧闸门名连同 D54 的 CPU 键一起忽略并告警（`orchestr/config.py`；原来只带 `vlm.gates` 的旧配置会让读取出错）。
- **测试**：`tests/cli/test_relabel_rerun.py` 改名为 `test_relabel.py`；只测 1.0、漏斗、注册表 4.x 与 `relabel_rerun` 的用例删掉，共享的用例改用 2.0
  记录与两块的计划（`EpisodeState.set_blocks`）。

