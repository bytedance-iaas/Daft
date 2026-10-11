# 结果读取（W5b）

读取任务运行目录里的已提交结果版本，并从 SQLite 读取运行中的逐条结果。
设计依据：`docs/design/06-delivery-and-report.md` §1、§3、§5、§6，`03-rest-api.md` §6、§7，`07-frontend.md` §5–§6，
`01-data-model.md` §2.7；决策 D16、D21、D22、D25、D29、D32、D35、D40、D42、D43。契约：C4 `openapi.yaml`（1.7.0）、C1 注册表（1.2 的 `REVIEW_LINES`）、
C2 `report` / `final-list` / `result-record` / `commit` / `decisions` / `source-manifest`。

## 文件

| 文件 | 内容 |
|---|---|
| `store.py` | `ResultStore`（每个 Daemon 一个，`store_of(runtime)`）：任务运行目录 `CURATOR_WORK_DIR/<task_id>/`、读哪个结果版本、缓存、两个钩子（`backfill`、`open_input`） |
| `revision.py` | 一个已提交的结果版本：`commit.json`、报告、四份清单、按 `commit.json` 的分片读模块结果 |
| `records.py` | 模块结果的行索引：只认该版本用过的分片，高编号的分片、靠后的行优先（与 CLI 的 `load_parts` 一致） |
| `tables.py` | 明细表：pyarrow 读 Parquet 的行组，排序只认 C1 `TableSpec.sortable`，游标带结果版本 |
| `episode.py` / `videos.py` | 单条 episode 的全模块视图；各机位视频从哪里放（运行目录里有剪好的片段就用片段，否则用源数据集；mcap 的相机由 Daemon 按需转封装） |
| `live.py` | 运行中的 episode 进度、模块记录与即时判定；不要求结果版本已提交，跑完后给当前结果版本的判决 |
| `episode_list.py` | 报告「Episode 明细」页签的 episode 列表（F6.2）：所在清单、归因模块、是否待裁，按清单 / 待裁 / 编号筛选，游标带结果版本 |
| `sync_curves.py` | 单条 episode 的同步曲线（F6.2）：逐相机的画面运动与机械臂运动、互相关曲线与这一版的读数 |
| `perf.py` | 性能剖析：全部 / 仅主流程 / 某次子任务 |
| `catalog.py` | 裁决线目录，取自 C1 注册表的 `REVIEW_LINES`（D43）：有哪些线、`review.json` 的哪种条目问哪条线、在哪个页签（`applies_to: reject` 的在复议页签）、算不算待裁（`counts_as_pending`）、收哪些结论及按钮名；结论的含义（弃用、拿不准、判成败）作为标记叠在上面，判成败的答案可以带改写的任务文本（`new_label`） |
| `adjudication.py` | 裁决队列：问题、卡片、状态、计数、逐线校验（`Queue.answerable` 一处）、追加记录、CSV 副本、`summary.pending_adjudication` |
| `files.py` | 按文件身份（mtime、大小）缓存的 JSON、有界 LRU、把 NaN 之类转成合法 JSON |
| `../routes/results.py`、`../routes/adjudication.py` | 路由 |

## 行为要点

- **即时逐条结果**：`GET /tasks/{id}/pipeline/episodes` 分页读取已完成至少一层的 episode；
  `GET /tasks/{id}/pipeline/episodes/{index}` 读取这条的当前模块结果。每条给两块里各逐条段的状态与临时判决
  （任务策略作用在已有的发现上），第一页还带 `modules`：每个逐条段模块到目前为止判过、出错、有发现的条数（episode 状态库的
  `findings` 列，C4 2.3.0，任务详情的模块卡在没有报告时用）。
  人工裁决只在终判里算（重复检测 D87 起只报告，不再改去留），最终通过 / 拒绝 / 待补跑以提交后的结果版本为准：任务到终态、有结果版本、没有子任务在跑时，每一行改给当前结果版本的判决
  （`provisional: false`，原因取清单的 `reasons[].text`，C4 5.3.2）；有重试或执行裁决在跑时重新回到临时判决。
- **读哪个版本**：缺省读 `task.result_rev`；`?rev=N` 可读 1 到 `result_rev` 之间任一已提交的版本。
  没有 `commit.json` 的版本不认；`result_rev` 之后已提交、还没切换过去的版本也不给看（D25）。
  任务还没有结果、版本号越界、本地文件不全，一律 404 `not_found`，`details.reason` 说明是
  `run_dir_missing`（本地工作目录已清理）还是 `revision_missing`。本地目录不在时先调 `backfill` 钩子，钩子不在或失败就报这个缺口。
- **报告**：`report.json` 原样返回（含 1.4 的 `overview.counts.skipped`、`integrity.skipped_episodes`），外加 `links`：
  任务、报告（旧版本带 `?rev=N`），当前版本还按来源模块给出有待裁条目的裁决页链接（D22）。
- **明细表**：一页默认 100 行、最多 500 行；`sort` 不在白名单是 400，表不存在或这一版没有这张表是 404。
  排序时缺失值（null、NaN）两个方向都排在最后，同值保持文件顺序。游标里是「结果版本 + 最后一行的位置」，
  筛选条件绑在游标上：换了表或排序是 400，结果版本变了是 409 `result_changed`（前端回第一页）。
- **单条 episode**：所在清单与原因、复核项（`{module, kind, text}`，能在裁决页回答的排在前面）、
  判定与交付用的任务文本、该版本看到的每个模块的结果行（C2 `result-record`）、证据帧路径（签名用 `scope=delivery`）、
  各机位视频。LeRobot v3 的多条拼在一个 mp4 里，视频项带 `from_ts` / `to_ts`（取自源数据集的 episode 表），
  签名接口不算时间。缺源文件被剔除的 episode（D40）返回 404，说明缺了哪些文件。`findings` 是
  清单条目里每条发现与它在这一版的级别（C2 final-list 2.0 的 `findings`），发现本身按条目给的位置取自这一版的模块记录，
  人的结论用它自己的一句话（C4 2.3.0 `EpisodeFinding`）。
- **episode 列表**（F6.2）：按下标列出这一版三份清单里的全部条目，带所在清单、`reason_modules`（拒绝：判它不过的模块；
  待补跑：出错的模块）、`review`（当前版本：裁决页上待裁或拿不准的卡片；历史版本：那一版提过的问题）。
  `q` 按下标子串匹配，去掉 `ep` 前缀和前导零（`12`、`ep12`、`ep 12`、`ep000012` 都找得到 ep 12），不是编号的是 400；
  游标里是「结果版本 + 最后一条下标」，换了筛选条件是 400，版本变了是 409 `result_changed`；`total` 是筛选后的条数，`counts` 是整版的。
  每条带 `items` / `levels`（取自清单条目的 `findings`），`level`、`item` 两个筛选按它们过滤，项编号格式不对是 400。
- **裁决问题的细码**（C4 2.3.0）：`review.json` 的复核项带 `codes` / `items`，队列的问题照样带出，裁决页按细码写明问的是什么。
- **同步曲线**（F6.2）：读 `frame` 段留下的 `checks/video_action_sync/curves/ep<NNNNNN>.json`，互相关按正式判定的预处理重算
  （`export.sync_plots._xcorr_curve`，剔首尾静止段），只取 ±2 秒扫描窗；读数（滞后、峰值、代码、是否可信）取这一版的结果记录，
  点位按读数的横坐标在曲线上插值。每条序列最多 600 点。没勾选同步是 404 `module_not_run`，这一条没走到或出错是 `no_record`，
  没保存曲线（默认只存值得留意的条目）是 `no_curves`。
- **源数据集的元数据**用任务的输入密钥读（`open_input` 钩子，缺省走 W8 的 `svc.tos`；HuggingFace 缓存桶匿名读；本地路径直接读），
  每个任务读一次、按源清单核对大小；读不到（密钥被删、TOS 不通、元数据变了）就不给源数据集这一路，一分钟后再试，不让请求失败。
- **性能剖析**：`scope=all` 是该版本提交时的 `perf.json`；`main` / `subtask` 按运行窗口切分 `details/vlm_latency.csv`
  （CSV 里没有子任务列，子任务串行执行，所以请求开始时刻落在哪个子任务的起止之间就归它，其余归主流程），
  并去掉该版本提交之后才发出的请求。延迟按调用种类分桶（次数 = 发起次数，失败 = 补发、重试后仍没拿到结果的调用，
  分位数只算成功的请求，墙钟 = 忙碌区间的并集）。stage 墙钟取自库里的分档进度，合并请求数取自实际调用账；
  外层重试次数没有落盘，`retries` 不给；`container` 是本容器的 cgroup 配额（CLI 与 Daemon 同一个容器）。
- **裁决队列**：问题就是当前版本 `review.json` 的条目，原样读，不重新推导（C2 1.5、D42、D43）；条目带 `line` 就用它，没有就按 `kind` 查目录：
  `task_verdict` → 判成败（任务成败拿不准的条），`eef_consistency` → EEF 与画面核对（`eef_check`，EEF 两个渠道结论相反的条），
  `integrity_suspect` → 完整性存疑（`integrity_check`），`reject_appeal` → 复议页签（任务成败判定的拒绝；重复检测 D87 起只报告，
  EEF 自设计 25 起不判废，现在只剩这一种）；目录里没有的种类不问。裁决表的 `line` 不限定取值（第 5 步迁移），按目录校验。
  问题上的原始标注（`annotation`）是这一版清单条目里的任务文本，没有时取任务成败判定记录里的 `task_desc`。答过的问题在后来的版本里不再出现时，
  从最近一个问过它的版本取回，所以「已裁 / 已应用」的卡片一直在，还能改。一条 episode 一张卡片，按 episode 下标排，游标同样带结果版本。
  - 状态：任一问题「整条弃用」→ 已裁（执行后为已应用），压过一切成败结论（规则 1）；否则有「拿不准」→ `unsure`，
    仍算待裁、仍在队列里（规则 3）；否则全部答了 → 已裁 / 已应用；答案带了新描述、还没执行 → 已裁（执行时按新描述重判，规则 4）；其余待裁。
  - 计数：待裁、已裁只数有 `counts_as_pending` 线上问题的卡片（复议候选不是必做的事），尚未应用数两个页签都算；这就是 `summary.pending_adjudication`。
  - 改写标注（D68）：一条问题线只有一个答案，改标不是单独的一问，判成败的答案可以带 `new_label`。
    带着成败结论给的改写直接采信、不再重判；成败答「拿不准」、只给改写的按新描述重判，这一笔照样要执行
    （「拿不准」平时不算要执行的事，带了新描述就算）。
  - 重复的条目不进队列（D87：重复检测只报告，不拒、不问），问题里也不再有 `duplicate_of`。
- **提交裁决**：只记录（追加一行，后写者胜），全部合法才写入。C4 1.5 的 `line`、`decision` 是开放字符串，
  由 `Queue.answerable` 一处校验：线必须在目录里、且这条 episode 的卡片上有这条线的问题，结论必须是这条线在目录里的结论；
  `new_label` 只能跟在判成败的答案上（`catalog.RELABEL_LINE`），不填就是不改；
  复议只收当前（或曾经）在复议页签里的条目，也就是只归因于一个可复议模块的拒绝（D87 起只有任务成败判定；规则 2）；
  「整条弃用」是判成败的一个结论，所以同一条线上后来的答案才能撤销它。
  裁决只属于路径上的这个任务（D32）。之后重写运行目录里的 `human-decisions/*.csv`（v1 的列与用词，用 CLI 同一个写法），
  并按当前版本重算任务汇总（含 `pending_adjudication`、1.4 的 `skipped`）。支持 `Idempotency-Key`。
  列队列时发现库里的待裁数过时了，也顺手改对。

## 给 W5a 的接口

```python
from daemon.results import Queue, store_of, refresh_summary, write_copies

store = store_of(runtime)
store.backfill = lambda task, missing: ...   # 本地运行目录不在时把它（至少小文件）从交付目录取回，成功返回 True
store.open_input = ...                        # 可选：换掉读源数据集元数据的方式（上下文管理器，给出 curation.cli.storage.Storage）

# 每次 switch_result_rev 成功之后：任务汇总与待裁数跟着新版本走
refresh_summary(store, runtime.repo, task.id, owner=task.owner_id)

# curation adjudicate-apply 会把 human-decisions/*.csv 改写成只含已应用的裁决；执行完之后调一次，放回全部记录
write_copies(store, runtime.repo, task)
```

- 执行裁决时导出给 CLI 的是 `Queue(store, runtime.repo, task).executable()`：每条线、每条 episode 最新且未应用、并且仍然成立的那一行
  （作废的追问回答不在其中），按 id 排好；执行完用 `repo.mark_adjudications_applied([d.id for d in rows], subtask.id)` 标记。
  不要直接用 `latest_adjudications(unapplied_only=True)`，那样会把作废的回答也交出去。
- 每次提交裁决后本地 `human-decisions/` 会变，需要同步到交付目录（文件很小）；在下一次同步时带上即可。

## 手动验证步骤

在 `backend/` 下执行，依赖见 Daemon 的 README。先造一个任务：九条 episode，时间戳、运动学极限、运动质量、画面质量、视频-动作同步、任务成败六项检查与重复检测都勾（镜头画面缺陷随任务成败判定），记录按测试里的故事手写，
第一个结果版本由真正的 `curation aggregate` 和 `curation report` 写出（`tests/results/conftest.py` 的说明里有逐条的结局）：

```bash
export CURATOR_DATA_DIR=$(mktemp -d)/data
PYTHONPATH=.:../tools ../.venv/bin/python - <<'EOF'
import os, pathlib
from daemon.repo.sqlite import SqliteRepository
from daemon.results import ResultStore, refresh_summary
from tests.results.conftest import (MODULES, build_run_dir, cli, finish_main_run, make_dataset,
                                    make_task, review_as_of_c2_1_4)

data = pathlib.Path(os.environ["CURATOR_DATA_DIR"]); data.mkdir(parents=True, exist_ok=True)
repo = SqliteRepository(data / "curator.db")
ds = make_dataset(data.parent / "datasets" / "droid_9")
task = make_task(repo, ds); finish_main_run(repo, task.id)
run_dir = data / "runs" / task.id; build_run_dir(run_dir, ds)
mods = ",".join(MODULES)
cli("aggregate", "--run-dir", str(run_dir), "--phase", "final", "--revision", "1", "--modules", mods, "--episodes", "0-8")
review_as_of_c2_1_4(run_dir, 1)      # W3 的 C2 1.4 / D42 review.json 修正合入之后这一步什么也不改
cli("report", "--run-dir", str(run_dir), "--revision", "1", "--modules", mods)
repo.switch_result_rev(task.id, 0, 1); refresh_summary(ResultStore(data / "runs"), repo, task.id)
repo.close(); print(f"export T={task.id}")
EOF
```

执行打印出来的那行，再启动 Daemon：

```bash
export CURATOR_MASTER_KEY=$(openssl rand -base64 32) CURATOR_BASE_PATH=/curation \
       CURATOR_AUTH_USER=demo CURATOR_AUTH_PASSWORD=demo-pass CURATOR_LOG_FORMAT=text
../.venv/bin/python -m daemon --host 127.0.0.1 --port 18080 &
B=localhost:18080/curation/api/v1/tasks/$T; c() { curl -s -u demo:demo-pass "$@"; }
```

1. **报告**：`c $B/report | python3 -m json.tool | head -40` —— `revision` 1，`counts` 为 total 9、passed 6、rejected 2、held 1、review 4；
   `links` 里有任务、报告，以及 `?source=task_success` 这条裁决页链接。
   `c "$B/report?rev=2"` 是 404「没有结果版本 r2……」；`c "$B/report?rev=0"` 是 400。
   报告里 `overview.reject_items` 是 STRM-5、TASK-4 各 1 条；时间戳检查一节 `summary` 的 `assessed_episodes` 9、
   `flagged_episodes` 1、`levels` 为 blocking 1、review 0、info 0；重复检测一节（D87）`groups` 1、`duplicated` 2、`levels` 为 info 2，
   `c $B/report/tables/duplicate_groups` 两行：ep0 的 `same_as` 是 `"7"`、ep7 的是 `"0"`，`verdict` 都是 `pass`。
2. **明细表**：`c "$B/report/tables/visual_quality?sort=score&order=desc&limit=3"` —— 最高分的三行（ep8 的两路、ep7 的一路），
   `has_more: true`；带上返回的 `next_cursor`（`&cursor=...`）接着翻，一直翻到底，18 行不重不漏，ep3 那一路没有分的排在最后。
   `c "$B/report/tables/visual_quality?sort=sharpness"` 是 400，列出能排序的列；`c $B/report/tables/nope` 是 404。
3. **单条 episode**：`c $B/episodes/2 | python3 -m json.tool` —— `list: reject`，原因「任务没有完成：3 路复核一致判未完成」（`kind: finding`、`code: failure`、`item: TASK-4`、`appealable: true`），
   证据帧 `details/evidence/task_success/ep000002_0.jpg`，两路视频来自源数据集，`from_ts` 28、`to_ts` 42（v3 拼接文件里的一段）。
   `findings` 里是这一条的全部发现：两路相机各一条「曝光不良」（IMG-2，`info`）和任务成败判定的 `failure`（TASK-4，`blocking`，`appealable: true`）。
   `c $B/episodes/99` 是 404。
   **episode 列表**：`c "$B/episodes?limit=4"` —— ep0–3，`has_more: true`，`counts` 为 all 9、passed 6、reject 2、held 1、review 3；
   `c "$B/episodes?review=true"` 是 ep3、ep4、ep5；`c "$B/episodes?list=reject"` 是 ep1、ep2（`reason_modules` 分别是时间戳检查、
   任务成败判定）；`c "$B/episodes?q=ep%208"` 只有 ep8；`c "$B/episodes?q=abc"` 是 400。
   每条带 `items` / `levels`（ep1 是 `["STRM-5", "IMG-2"]` / `["blocking", "info"]`）；`c "$B/episodes?level=review"` 是 ep3、ep4、ep5，
   `c "$B/episodes?item=TASK-4"` 是 ep2–ep5，`c "$B/episodes?item=SET-1"` 是 ep0、ep7（重复只报告，两条都在 passed），`c "$B/episodes?item=strm-5"` 是 400。
   **同步曲线**：`c $B/episodes/3/sync-curves` 是 404 `no_curves`（手写的运行目录没有曲线文件；被时间戳判废的 ep1 也一样，
   两块并行之后每个模块都看过每一条，D57）；没有这个模块结果的条目是 404 `no_record`（自动化测试里有）；
   有曲线的条目返回逐相机的 `t` / `flow` / `speed`、`lags` / `xcorr` 和 `peak`。
4. **性能剖析**：`c $B/perf` —— `latency` 三行：`probe` 4 次（1 次对冲补发）、P50 3 秒、墙钟 62 秒；`eef_review` 1 次；
   `example_grasp`（模块自己的调用种类，C3 1.1）1 次、失败 1 次。stage 占比 numeric 0.1、frame 0.3、vlm 0.6。`c "$B/perf?scope=subtask"` 是 400（要给 `subtask`）。
5. **裁决队列**：`c "$B/adjudication?status=all"` —— 三张卡片 ep3、ep4、ep5，每张一个「判成败」的问题，
   计数 `{"decided": 0, "pending": 3, "unapplied": 0}`；ep8（运动质量打不出分）不在里面。每个问题带它问的细码与项：
   三张都是 `codes: ["uncertain"]`、`items: ["TASK-4"]`；`reason` 是拿不准的原因（ep3「任务成败拿不准：两层证据矛盾，进人工」，
   ep4、ep5「任务成败拿不准：看不清积木最后是否放进箱子」），`annotation` 是这一条的任务文本。问题上没有 `caption`、`suggestion`、`priority`（C4 7.0.0）。
   `c "$B/adjudication?tab=appeals&status=all"` 只有 ep2（任务成败判定判失败）；时间戳残段 ep1 不能复议，与 ep0 重复的 ep7 没被拒、不在这里（D87）。
6. **提交裁决**：

   ```bash
   c -X POST -H 'Content-Type: application/json' "$B/adjudication" \
     -d '{"decisions":[{"episode_index":3,"line":"task_verdict","decision":"success"},{"episode_index":4,"line":"task_verdict","decision":"unsure","new_label":"wipe the table with the cloth"}]}'
   c -X POST -H 'Content-Type: application/json' "$B/adjudication" \
     -d '{"decisions":[{"episode_index":5,"line":"task_verdict","decision":"success"},{"episode_index":1,"line":"reject_appeal","decision":"restore"}]}'
   c -X POST -H 'Content-Type: application/json' "$B/adjudication" \
     -d '{"decisions":[{"episode_index":2,"line":"reject_appeal","decision":"discard"}]}'
   cat $CURATOR_DATA_DIR/runs/$T/human-decisions/*.csv
   c localhost:18080/curation/api/v1/tasks/$T | python3 -c 'import json,sys; print(json.load(sys.stdin)["pending_adjudication"])'
   ```

   预期：第一条返回 `{"decided":1,"pending":2,"unapplied":2}`：ep3 已裁；ep4 答了「拿不准」并改写了任务文本，卡片是 `unsure`，
   仍算待裁，执行时按新文本重判，所以也算一笔未应用。第二条 400「ep000001 不在复议列表里，不能复议……」，同一批里 ep5 的答案也没写进去
   （全部合法才写入）。第三条 400「ep000002：被拒复议不能选 discard……」：「整条弃用」是判成败的结论，不是复议的。
   只有一个 CSV `task_verdicts.csv`（v1 的列 `episode_id,verdict,new_label,note,at`），两行：「ep000003,判成功」和
   「ep000004,拿不准,wipe the table with the cloth」。任务的待裁数是 2。
7. `c "$B/adjudication?limit=1"` 取第一页，再提交一条裁决之后带着 `next_cursor` 翻下一页：不重不漏；
   结果版本切换之后再用旧游标是 409 `result_changed`（自动化测试里有完整的切换过程）。
8. **跑完的流水线给最终判决**（C4 5.3.2；要一个真跑完的两块任务，如本机 `curator-daemon-eef` 上的任务，`B` 换成它的地址）：
   `c "$B/pipeline/episodes?limit=3"` —— 每一行 `provisional: false`，`verdict` 与运行目录 `revisions/r000N/` 当前版本的清单一致
   （passed → keep、reject → drop、held → held），`verdict_reason` 是拒绝原因的原文；控制台任务详情的「当前结果」写「通过 / 拒绝 / 待补跑」。
   在这个任务上「重试」或「执行裁决」，子任务跑着时这一页回到 `provisional: true`（「暂判…」）。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/results     # 约 1.5 分钟
```

测试的运行目录都是手写的：各模块的结果行写成分片，结果版本由真正的 `curation aggregate --phase final` 和 `curation report`
在进程内写出（每条命令的 `--json` 都按 C2 校验），执行裁决按 W5a 的做法调 `curation adjudicate-apply`。
每个响应都按 C4 1.4.0 校验，错误响应也校验错误体。W3 的 C2 1.4 `review.json` 修正合入之前，`review_as_of_c2_1_4`
把 CLI 写的 `review.json` 改成 1.4（含 D42）的样子（合入之后它什么也不改）。
