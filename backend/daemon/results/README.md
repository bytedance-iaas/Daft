# 结果读取（W5b）

把任务运行目录里已提交的结果版本读出来，提供六个接口：报告、明细表、单条 episode、性能剖析、裁决队列、提交裁决。
设计依据：`docs/design/06-delivery-and-report.md` §1、§3、§5、§6，`03-rest-api.md` §6、§7，`07-frontend.md` §5–§6，
`01-data-model.md` §2.7；决策 D16、D21、D22、D25、D29、D32、D35、D40、D42、D43。契约：C4 `openapi.yaml`（1.5.0）、C1 注册表（1.2 的 `REVIEW_LINES`）、
C2 `report` / `final-list` / `result-record` / `commit` / `decisions` / `source-manifest`。

## 文件

| 文件 | 内容 |
|---|---|
| `store.py` | `ResultStore`（每个 Daemon 一个，`store_of(runtime)`）：任务运行目录 `CURATOR_WORK_DIR/<task_id>/`、读哪个结果版本、缓存、两个钩子（`backfill`、`open_input`） |
| `revision.py` | 一个已提交的结果版本：`commit.json`、报告、四份清单、合并后的标注审计、按 `commit.json` 的分片读模块结果 |
| `records.py` | 模块结果的行索引：只认该版本用过的分片，高编号的分片、靠后的行优先（与 CLI 的 `load_parts` 一致） |
| `tables.py` | 明细表：pyarrow 读 Parquet 的行组，排序只认 C1 `TableSpec.sortable`，游标带结果版本 |
| `episode.py` / `videos.py` | 单条 episode 的全模块视图；各机位视频从哪里放（片段 → 交付数据集 → 源数据集） |
| `perf.py` | 性能剖析：全部 / 仅主流程 / 某次子任务 |
| `catalog.py` | 裁决线目录，取自 C1 注册表的 `REVIEW_LINES`（D43）：有哪些线、`review.json` 的哪种条目问哪条线、在哪个页签（`applies_to: reject` 的在复议页签）、算不算待裁（`counts_as_pending`）、收哪些结论及按钮名；v1 各结论的含义（改标、弃用、拿不准、判成败）作为标记叠在上面 |
| `adjudication.py` | 裁决队列：问题、卡片、状态、计数、逐线校验（`Queue.answerable` 一处）、追加记录、CSV 副本、`summary.pending_adjudication` |
| `files.py` | 按文件身份（mtime、大小）缓存的 JSON、有界 LRU、把 NaN 之类转成合法 JSON |
| `../routes/results.py`、`../routes/adjudication.py` | 路由 |

## 行为要点

- **读哪个版本**：缺省读 `task.result_rev`；`?rev=N` 可读 1 到 `result_rev` 之间任一已提交的版本。
  没有 `commit.json` 的版本不认；`result_rev` 之后已提交、还没切换过去的版本也不给看（D25）。
  任务还没有结果、版本号越界、本地文件不全，一律 404 `not_found`，`details.reason` 说明是
  `run_dir_missing`（本地工作目录已清理）还是 `revision_missing`。本地目录不在时先调 `backfill` 钩子，钩子不在或失败就报这个缺口。
- **报告**：`report.json` 原样返回（含 1.4 的 `overview.counts.skipped`、`integrity.skipped_episodes`），外加 `links`：
  任务、报告（旧版本带 `?rev=N`），当前版本还按来源模块给出有待裁条目的裁决页链接（D22）。
- **明细表**：一页默认 100 行、最多 500 行；`sort` 不在白名单是 400，表不存在或这一版没有这张表是 404。
  排序时缺失值（null、NaN）两个方向都排在最后，同值保持文件顺序。游标里是「结果版本 + 最后一行的位置」，
  筛选条件绑在游标上：换了表或排序是 400，结果版本变了是 409 `result_changed`（前端回第一页）。
- **单条 episode**：所在清单与原因、复核项（`{module, kind, text, priority}`，能在裁决页回答的排在前面）、
  判定与交付用的任务文本、该版本看到的每个模块的结果行（C2 `result-record`）、证据帧路径（签名用 `scope=delivery`）、
  各机位视频。LeRobot v3 的多条拼在一个 mp4 里，视频项带 `from_ts` / `to_ts`（取自源数据集或交付数据集的 episode 表），
  签名接口不算时间。缺源文件被剔除的 episode（D40）返回 404，说明缺了哪些文件。
- **源数据集的元数据**用任务的输入密钥读（`open_input` 钩子，缺省走 W8 的 `svc.tos`；HuggingFace 缓存桶匿名读；本地路径直接读），
  每个任务读一次、按源清单核对大小；读不到（密钥被删、TOS 不通、元数据变了）就不给源数据集这一路，一分钟后再试，不让请求失败。
- **性能剖析**：`scope=all` 是该版本提交时的 `perf.json`；`main` / `subtask` 按运行窗口切分 `details/vlm_latency.csv`
  （CSV 里没有子任务列，子任务串行执行，所以请求开始时刻落在哪个子任务的起止之间就归它，其余归主流程），
  并去掉该版本提交之后才发出的请求。延迟分桶沿用 v1（次数 = 发起次数，失败 = 补发、重试后仍没拿到结果的调用，
  分位数只算成功的请求，墙钟 = 忙碌区间的并集）。stage 墙钟取自库里的分档进度，合并请求数取自实际调用账；
  外层重试次数没有落盘，`retries` 不给；`container` 是本容器的 cgroup 配额（CLI 与 Daemon 同一个容器）。
- **裁决队列**：问题就是当前版本 `review.json` 的条目，原样读，不重新推导（C2 1.5、D42、D43）；条目带 `line` 就用它，没有就按 `kind` 查目录：`label_conflict` → 标注分歧，
  `task_verdict` → 判成败，`reject_appeal` → 复议页签（任务成败判定的拒绝，D42 起还有去重剔除的重复项）；目录里没有的种类不问。原始标注、画面描述来自该版本的 `label_audit.json`，
  建议的新标注就是画面描述（v1 采纳的就是它）。答过的问题在后来的版本里不再出现时，从最近一个问过它的版本取回，
  所以「已裁 / 已应用」的卡片一直在，还能改。一条 episode 一张卡片，按 episode 下标排，游标同样带结果版本。
  - 状态：任一问题「整条弃用」→ 已裁（执行后为已应用），压过一切成败结论（规则 1）；否则有「拿不准」→ `unsure`，
    仍算待裁、仍在队列里（规则 3）；否则全部答了 → 已裁 / 已应用；只改了标、还没执行 → 已裁（执行时按新标注重判，规则 4）；其余待裁。
  - 计数：待裁、已裁只数有 `counts_as_pending` 线上问题的卡片（复议候选不是必做的事），尚未应用数两个页签都算；这就是 `summary.pending_adjudication`。
  - 去重剔除的复议问题带 `duplicate_of`（与哪一条重复）。
- **提交裁决**：只记录（追加一行，后写者胜），全部合法才写入。C4 1.5 的 `line`、`decision` 是开放字符串，
  由 `Queue.answerable` 一处校验：线必须在目录里、且这条 episode 的卡片上有这条线的问题（v1 的可选成败例外），结论必须是这条线在目录里的结论；
  `new_label` 只给「采纳建议改标」「自行改写标注」，自行改写必须填，采纳时不填就用建议的新标注；
  复议只收当前（或曾经）在复议页签里的条目 —— 也就是只归因于一个可复议模块的拒绝（任务成败判定、D42 起的去重；规则 2）；
  标注上已经「整条弃用」的不再收成败结论；只有标注问题的卡片，改了标之后才收成败结论（v1 的可选成败）。
  裁决只属于路径上的这个任务（D32）。之后重写运行目录里的 `human-decisions/*.csv`（v1 的列与用词，用 CLI 同一个写法），
  并按当前版本重算任务汇总（含 `pending_adjudication`、1.4 的 `skipped`）。支持 `Idempotency-Key`。
  列队列时发现库里的待裁数过时了，也顺手改对。

## 给 W5a 的接口

```python
from daemon.results import store_of, refresh_summary, write_copies

store = store_of(runtime)
store.backfill = lambda task, missing: ...   # 本地运行目录不在时把它（至少小文件）从交付目录取回，成功返回 True
store.open_input = ...                        # 可选：换掉读源数据集元数据的方式（上下文管理器，给出 curation.cli.storage.Storage）

# 每次 switch_result_rev 成功之后：任务汇总与待裁数跟着新版本走
refresh_summary(store, runtime.repo, task.id, owner=task.owner_id)

# curation adjudicate-apply 会把 human-decisions/*.csv 改写成只含已应用的裁决；执行完之后调一次，放回全部记录
write_copies(store, runtime.repo, task)
```

- 执行裁决时导出给 CLI 的是 `repo.latest_adjudications(task.id, unapplied_only=True)`（每条线、每条 episode 最新且未应用的一行）。
- 每次提交裁决后本地 `human-decisions/` 会变，需要同步到交付目录（文件很小）；在下一次同步时带上即可。

## 手动验证步骤

在 `backend/` 下执行，依赖见 Daemon 的 README。先造一个任务：九条 episode，八个模块都勾，记录按测试里的故事手写，
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

1. **报告**：`c $B/report | python3 -m json.tool | head -40` —— `revision` 1，`counts` 为 total 9、passed 5、rejected 3、held 1、review 5；
   `links` 里有任务、报告，以及 `?source=task_success`、`?source=skill_profile` 两条裁决页链接。
   `c "$B/report?rev=2"` 是 404「没有结果版本 r2……」；`c "$B/report?rev=0"` 是 400。
2. **明细表**：`c "$B/report/tables/visual_quality?sort=score&order=desc&limit=3"` —— 最高分的三行（ep8 的两路、ep7 的一路），
   `has_more: true`；带上返回的 `next_cursor`（`&cursor=...`）接着翻，一直翻到底，16 行不重不漏，ep3 那一路没有分的排在最后。
   `c "$B/report/tables/visual_quality?sort=sharpness"` 是 400，列出能排序的列；`c $B/report/tables/nope` 是 404。
3. **单条 episode**：`c $B/episodes/2 | python3 -m json.tool` —— `list: reject`，原因「未通过「任务成败判定」:3 路复核一致判未完成」，
   证据帧 `details/evidence/task_success/ep000002_0.jpg`，两路视频来自源数据集，`from_ts` 28、`to_ts` 42（v3 拼接文件里的一段）。
   `c $B/episodes/99` 是 404。
4. **性能剖析**：`c $B/perf` —— probe 4 次（1 次对冲补发）、P50 3 秒、墙钟 62 秒，arbitration 1 次失败；
   stage 占比 numeric 0.1、frame 0.3、vlm 0.6。`c "$B/perf?scope=subtask"` 是 400（要给 `subtask`）。
5. **裁决队列**：`c "$B/adjudication?status=all"` —— 三张卡片：ep3（判成败）、ep4（标注分歧）、ep5（两个问题），
   计数 `{"decided": 0, "pending": 3, "unapplied": 0}`；ep8（运动质量打不出分）不在里面。
   `c "$B/adjudication?tab=appeals&status=all"` 是 ep2（任务成败判定判失败）和 ep7（与 ep0 重复，D42 起可以复议）；时间戳残段 ep1 不能复议。
6. **提交裁决**：

   ```bash
   c -X POST -H 'Content-Type: application/json' "$B/adjudication" \
     -d '{"decisions":[{"episode_index":3,"line":"task_verdict","decision":"success"},{"episode_index":4,"line":"label","decision":"adopt_suggestion"}]}'
   c -X POST -H 'Content-Type: application/json' "$B/adjudication" \
     -d '{"decisions":[{"episode_index":1,"line":"reject_appeal","decision":"restore"}]}'
   c -X POST -H 'Content-Type: application/json' "$B/adjudication" \
     -d '{"decisions":[{"episode_index":5,"line":"label","decision":"discard"},{"episode_index":5,"line":"task_verdict","decision":"success"}]}'
   cat $CURATOR_DATA_DIR/runs/$T/human-decisions/*.csv
   c localhost:18080/curation/api/v1/tasks/$T | python3 -c 'import json,sys; print(json.load(sys.stdin)["pending_adjudication"])'
   ```

   预期：第一条返回 `{"decided":2,"pending":1,"unapplied":2}`；第二条 400「ep000001 不是被任务成败判定拒掉的条目，不能复议……」；
   第三条 400「已经「整条弃用」……」，而且整批都没写进去（全部合法才写入）。
   CSV 是 v1 的列：`label_decisions.csv` 一行「采纳建议改标,wipe the table with the cloth」，`task_verdicts.csv` 一行「判成功」。
   任务的待裁数是 1。
7. `c "$B/adjudication?limit=1"` 取第一页，再提交一条裁决之后带着 `next_cursor` 翻下一页：不重不漏；
   结果版本切换之后再用旧游标是 409 `result_changed`（自动化测试里有完整的切换过程）。

## 自动化测试

```bash
../.venv/bin/python -m pytest -q tests/results     # 约 1.5 分钟
```

测试的运行目录都是手写的：各模块的结果行写成分片，结果版本由真正的 `curation aggregate --phase final` 和 `curation report`
在进程内写出（每条命令的 `--json` 都按 C2 校验），执行裁决按 W5a 的做法调 `curation adjudicate-apply`。
每个响应都按 C4 1.4.0 校验，错误响应也校验错误体。W3 的 C2 1.4 `review.json` 修正合入之前，`review_as_of_c2_1_4`
把 CLI 写的 `review.json` 改成 1.4（含 D42）的样子（合入之后它什么也不改）。
