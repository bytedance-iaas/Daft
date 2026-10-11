# 对账工具（W0 / F1.1）

v2 的安全网。`run-v2 --fake-vlm` 在合成数据集上按 Daemon 的顺序跑一遍原子命令，录下一份黄金基线（运行目录 + 全部模型调用的录制带）；
之后的改动用 `run-v2 --replay` 回放这盘带子再跑一遍，`compare --all-strict` 要求逐位一致。模型被钉死，任何差异都来自代码。
测试见 `tests/test_v2_parity.py` 和 `tests/test_containers_parity.py`。

基线是 v2 自录的（设计 13 起）：判定协议换成连续视频后，冻结点的 v1（图片探针）答不了 v2 的请求，v1 对 v2 的逐位对账已经退役，
v1 一侧的工具（`dump-v1`、`pack`、`archive` 等）随 v1 代码一起删掉了（设计 27，D91）。冻结点 `dev` 的 `eb637ba40`（D44）只剩一个用处：
A 类守卫比对的文件哈希就是那时的，存在 `a_class_manifest.json`。

| 命令（`python -m parity …`） | 作用 |
|---|---|
| `run-v2` | 按 Daemon 的顺序跑一遍 v2 的原子命令（preflight → … → verify），模型调用走录制带挂钩；带 `--from … --decisions …` 时在一份已跑完的运行目录副本上跑裁决序列 |
| `compare` | 比较两个 v2 运行目录：各模块的记录逐位比（模型判定的模块缺省按判决比，`--all-strict` 也逐位比），终判清单比，回放的命中与调用图 |
| `make-fixture` | 生成 8 条 episode 的合成 LeRobot v2 数据集（真视频）；`--format mcap` / `--format lance` 是同样 8 条的 mcap（每条一个 cdr 编码的 `.mcap`）与 Lance（lerobot-lance-convert 的三表布局）版本（D44） |
| `tape-summary` | 看一盘录制带：各类调用多少次、有没有失败 |
| `a-class-check` | A 类算法文件与冻结时的哈希逐个比对；有差异、PR 描述里没有 `parity-change:` 说明、`a_class_declared.json` 也没登记就失败（CI 里跑） |

所有命令都在仓库根目录、以 `PYTHONPATH=tools` 运行。

## 它怎么工作

- **`run-v2`**（设计 11 §3）：在同一进程里按 Daemon 两块的顺序依次调原子命令（设计 17 §3：CPU 块的检查、VLM 块的模型判定，
  最后一步 `check duplicate` 是重复检测，D87；每一段都拿全部所选条目；没有任务标注的条目不判成败，D72），写出一个普通的 v2 运行目录。
  调模型的命令都带 `--hedge`（挂钩替换的正是对冲函数）和 `--concurrency 64`。`parity.json` 记着每一步的命令行、退出码、
  `--json` 输出和录制带统计，各命令的 stderr 在 `logs/parity-run.log`。
- **录制带**（`vlm_tape.jsonl.gz`）：每行一次调用，存请求的规范形式（提示词全文、每段视频或每张图的 sha256、模型参数，不存媒体本身）
  和当时的响应。回放时按请求哈希取回响应；请求差一个字或一个字节，哈希就对不上。`/models` 探活不算模型调用：每条命令探一次，
  回放时这类请求可重复取用，不计入命中和剩余。格式见
  [`docs/contracts/parity/vlm-tape-entry.schema.json`](../../docs/contracts/parity/vlm-tape-entry.schema.json)（2.0）。
- **`compare`**：读两个运行目录的 `checks/*/results.jsonl`（C2 2.0 的记录，见
  [`result-record.schema.json`](../../docs/contracts/cli/result-record.schema.json)）、最新已提交结果版本的清单和
  `checks/duplicate_check/groups.json`。确定性的模块逐位比（浮点按 `repr`），重复检测比重复组与动作哈希撞车组；
  任务成败判定缺省按判决词与规则比，差异率要低于 `--max-diff-rate`，`--all-strict` 时也逐位比。任一边执行出错的条目从清单比较里剔除并列出（D33）。
- **调用图一致**：回放一项要求 misses 为 0（候选的每个请求都在带子上）且没有剩余（带子上的每个请求候选都发了）。
  多一个、少一个、提示词差一个字，都会失败。
- **假模型**（`fakevlm.py`，`--fake-vlm` 与 CLI 测试的假端点都用它）：按提示词的格式作答，答案只由请求的文字、媒体的个数和
  每张图的像素尺寸决定（`FakeVlm.answer_key`），不看字节：同一段视频在 macOS 和 Linux 上编出的字节不同，按字节选答案会让同一个测试
  在两个平台上得到不同的判决。录制带的请求哈希仍按字节算，所以基线和回放要在同一台机器上。
- **人工裁决也对账**（D39）：`run-v2 --from <运行目录> --decisions <decisions.json>` 在运行目录的副本上按 Daemon 的顺序跑
  adjudicate-apply → 重判改写了描述的条目 → 终判 → 报告（下一个结果版本）。`--fake-vlm` 录一份裁决基线，同样的裁决 `--replay` 它；
  `compare` 认出金标是裁决序列，只比重判的 task_success 记录（逐位）、终判清单和回放。

## 本地环境

依赖按 `backend/requirements.txt` 和 `backend/requirements-dev.txt` 装在仓库根目录的 `.venv` 里（后端各套测试和本目录的测试都用它；
CI 在 Linux 上装的就是这两份）：

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
.venv/bin/python -m pip install "scipy==1.16.3"      # 只在 macOS 27 上：1.15.3 的 wheel 加载会报 dyld 错误
```

镜像与 CI 是 Python 3.10，本机是 3.12：别用 3.11 以后才有的语法和标准库。scipy 别升到 1.18，它改了 `Rotation.from_euler` 的行为。

## 手动验证步骤（离线，不需要任何密钥）

在仓库根目录执行，约半分钟：

```bash
export PYTHONPATH=tools
python=.venv/bin/python
W=$(mktemp -d)

# 1. 合成数据集：8 条，含一条时间戳跳变、一条残段、一对字节级重复、两条无标注
$python -m parity make-fixture --out $W/mini

# 2. 按 Daemon 的顺序跑原子命令，由假模型现答并录下全部调用：这就是黄金基线 → [run-v2] done; tape record: written=9
$python -m parity run-v2 --out $W/golden --input $W/mini --delivery $W/golden-delivery --fake-vlm

# 3. 回放这盘带子再跑一遍 → [run-v2] done; tape replay: hits=8 misses=0 unused=0
$python -m parity run-v2 --out $W/v2 --input $W/mini --delivery $W/v2-delivery --replay $W/golden/vlm_tape.jsonl.gz

# 4. 与基线对账：八个模块逐位一致、终判清单一致、回放无缺无余 → 最后一行 conclusion: PASS
$python -m parity compare --golden $W/golden --candidate $W/v2 --all-strict
```

逐项核对：
- `$W/golden/revisions/r0001/` 里 `passed` 是 `[0, 3, 4, 6, 7]`，`reject` 是 `[1, 2, 5]`，`held` 为空：2 是时间戳跳变，5 是残段，
  1 被判任务失败（只判一次，不复核、不仲裁，D71）；4、6 没有任务标注，不判成败（D72）；7 是 3 的字节级副本，重复检测只报告（D87），
  3、7 都通过、各带一条 SET-1 发现。`review` 是 `[0, 1, 3, 7]`：0、3、7 的任务成败拿不准，1 是可复议的判废。
- `$python -m parity tape-summary $W/golden/vlm_tape.jsonl.gz`：9 条，probe 8（每条一次视频请求；4、6 只问画面缺陷，D73）和
  1 条 `/models` 探活，`0 failed calls`。
- 把第 3 步的 `--replay …` 换成 `--fake-vlm`，不看带子、由假模型现答，对账结论同样是 PASS。

数据完整性模块（设计 14）不在默认链里（基线录的时候没有它）。要录一盘带它的基线，两次 `run-v2` 都加
`--modules data_integrity,timestamp_check,kinematic_limits,motion_quality,visual_quality,video_action_sync,task_success,duplicate_check`：
CPU 块最前面多一步 `check integrity`（不拦后面的段，D57）；`compare --all-strict` 仍是 PASS，本模块另用
`compare --strict data_integrity --verdict-only task_success` 逐位比。在干净的夹具上三份清单与默认基线相同。

mcap 与 Lance（D44，每种十来秒）：同样 8 条做成两种格式，各录一盘基线、再回放它（`run-v2` 与 Daemon 一样给读源数据的命令带 `--selection`）：

```bash
for F in mcap lance; do
  $python -m parity make-fixture --format $F --out $W/mini-$F
  $python -m parity run-v2 --out $W/golden-$F --input $W/mini-$F --delivery $W/golden-$F-delivery --fake-vlm
  $python -m parity run-v2 --out $W/v2-$F --input $W/mini-$F --delivery $W/v2-$F-delivery --replay $W/golden-$F/vlm_tape.jsonl.gz
  $python -m parity compare --golden $W/golden-$F --candidate $W/v2-$F --all-strict      # conclusion: PASS
done
```

两种格式的终判清单都与 LeRobot 版本相同（`passed` `[0, 3, 4, 6, 7]`、`reject` `[1, 2, 5]`，重复组 `[[3, 7]]`），回放 `hits=8 misses=0 unused=0`。

人工裁决（D39，约十秒）：在第 2 步的基线上执行两条改标，先由假模型现答录一份裁决基线，再回放它对账：

```bash
cat > $W/decisions.json <<'EOF'
{"schema_version": "2.0", "decisions": [
 {"id": 1, "episode_index": 1, "line": "task_verdict", "decision": "unsure", "new_label": "stack the cups", "note": null, "decided_by": "me", "decided_at": 1790000000001},
 {"id": 2, "episode_index": 0, "line": "task_verdict", "decision": "unsure", "new_label": "wipe the table", "note": null, "decided_by": "me", "decided_at": 1790000000002}]}
EOF
$python -m parity run-v2 --out $W/adj-golden --from $W/golden --input $W/mini --decisions $W/decisions.json --fake-vlm
$python -m parity run-v2 --out $W/adj --from $W/golden --input $W/mini --decisions $W/decisions.json --replay $W/adj-golden/vlm_tape.jsonl.gz
$python -m parity compare --golden $W/adj-golden --candidate $W/adj --all-strict      # task_success 0/2 differ … conclusion: PASS
```

裁决基线的带子是 2 条 probe（0、1 按新标注各判一次）和 1 条探活，回放 `hits=2 misses=0 unused=0`；结果在 `revisions/r0002/`。

单元测试与端到端测试：`$python -m pytest -q tools/parity/tests`（约一分钟；`-m "not e2e"` 只跑单元部分）。
`test_v2_parity.py` 就是上面的第 2–4 步和裁决，外加「带子上少一个请求时对账必须失败」的反例；CI 的对账工具一步里一起跑。

## A 类守卫

A 类是从 v1 原样搬来的算法文件（设计 10 §2，路径前缀在 `aclass.py` 的 `A_CLASS`）。`a_class_manifest.json` 是它们在冻结点的 git blob 哈希，
`a_class_declared.json` 登记之后有意接受的改动（新哈希）和删除（`"removed": true`），每条写明理由。守卫只放行这两种，登记过的文件再改一次又会被拦下。

```bash
PYTHONPATH=tools .venv/bin/python -m parity a-class-check        # A-class files: 59, changed 0, missing 0, added 0, declared …
```

## 已知限制

- **字节级重复的两条 episode** 发出的请求完全相同，回放时它们的响应可能对调。真模型对这两条给了不同回答时，会表现为两条互换的差异，
  对账时对照 `checks/duplicate_check/groups.json` 的重复组人工确认。
- `compare` 读运行目录时，取 `revisions/` 下最新的已提交版本（有 `commit.json` 的），没有就取编号最大的；`run-v2` 产生 r0001，
  带 `--from` 的裁决序列产生下一个版本。
- 裁决的对账只覆盖改标（唯一要调模型的一条线）；成败裁决与复议不调模型，它们对清单的作用由 `backend/tests/cli/test_aggregate.py` 钉住。
- 真实数据集上的黄金基线还没有录（F4.2 未完成）；现在的基线只有合成数据集这一份。
