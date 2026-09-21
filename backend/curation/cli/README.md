# curation 命令行（v2，工作包 W3）

`curation` 是 Curator v2 的命令行入口，设计见 `docs/design/02-cli-contract.md`，契约见 `docs/contracts/`（C2 输出、C3 进度、C4 REST）。

本批（W3 前半）落地的内容：

| 命令 | 作用 | `--json` 契约 |
|---|---|---|
| `curation preflight` | 只读 metadata，判格式、数 episode、给出每个模块「可跑 / 需补充 / 不支持」及原因（F2.7） | `cli/preflight.schema.json` |
| `curation snapshot` | 固化任务要读的源对象清单（键、大小、ETag 或修改时间），写 `source_manifest.json` | `cli/source-manifest.schema.json` |
| `curation verify` | 从交付目录逐个回读关键文件，全部通过才最后写 `_COMPLETE` | `cli/verify.schema.json` |
| `curation task …` | Daemon REST API 的薄客户端，给 Agent 和脚本用，输出带 `links` | `openapi.yaml` 里对应接口的响应，原样打印 |

v1 的子命令（`run`、`rejudge`、`review-page`、`prune`、`ls`、`fetch`、`backends`、`public` 和隐藏的 `reprofile`）原样转交给 `legacy.py`（即原来的 `curation/cli.py`，只改了 import 路径），用法和输出都不变。`plan`、`autolabel`、`check`、`aggregate`、`export`、`report`、`adjudicate-apply` 属于 W3 后半，要等黄金基线存档后再动 v1 编排代码。

## 代码结构

| 文件 | 内容 |
|---|---|
| `app.py` | 参数解析、v1 子命令转交、`main()`（`curation.cli:main` 就是它） |
| `framework.py` | 输出纪律、C3 进度事件、错误信封与退出码、SIGTERM / SIGINT |
| `errors.py` | 退出码与对应的异常类 |
| `creds.py` | 凭证只从环境变量读；输入、输出两套访问密钥 |
| `storage.py` | 本地目录与 `tos://` 的统一读写（列举、按范围读、写 `_COMPLETE`） |
| `lerobot_meta.py` | 不读样本数据的 LeRobot 元数据读取：格式识别、episode 表、每条的文件键 |
| `preflight.py` / `snapshot.py` / `verify.py` / `task_client.py` | 四条命令 |
| `source_manifest.py` | `source_manifest.json` 的生成与校验（`--source-manifest`，对不上退出码 6） |
| `episodes.py` / `inputs.py` | `--episodes` 语法（含 `@文件`）、`--input` / `--source` |

## 全局约定

**全局参数**（02 篇 §2）：原子命令都接受 `--json`、`--log-level`、`--config`、`--set k=v`、`--region` / `--input-region` / `--output-region`；`curation task …` 只接受 `--json`、`--log-level`（连接参数另见下文）。参数都写在子命令之后，例如 `curation preflight --input … --json`。

**输出纪律**：

- 带 `--json` 时，stdout 只有一个 JSON 文档：成功时是命令结果，非零退出时是错误信封（`cli/error.schema.json`）。stderr 每行一个 C3 事件（`progress` / `log` / `usage` / `throttle`）。v1 库代码里的 `print` 不会混进 stdout，会被转成 `log` 事件。
- 不带 `--json` 时，stdout 是给人看的结果，日志和进度走 stderr；运行不到 1 秒的阶段不打进度行。

**退出码**（02 篇 §4）：

| 码 | `error.code` | 含义 |
|---|---|---|
| 0 | — | 成功（`verify` 有坏文件时也是 0，看 `failed`） |
| 2 | `usage` | 参数或配置错误；Daemon 调用时属于 bug |
| 3 | `input_unreachable` | 数据读不了：路径不存在、密钥被拒、网络不通、Daemon 连不上 |
| 4 | `module_failed` | 命令整体失败；未预期的异常也归这里，调用栈以 `error` 级日志写到 stderr |
| 5 | `terminated` | 收到 SIGTERM：不再开始新的工作，已在做的做完后退出 |
| 6 | `source_changed` | 源数据和 `--source-manifest` 对不上 |
| 130 | `interrupted` | 收到 SIGINT，立即中止 |

**凭证只走环境变量**，没有任何参数接收密钥（argv 在 `ps` 里全局可见）：

| 用途 | 环境变量 |
|---|---|
| 读输入数据集 | `CURATION_INPUT_TOS_ACCESS_KEY` / `CURATION_INPUT_TOS_SECRET_KEY`（可选 `CURATION_INPUT_TOS_SESSION_TOKEN`） |
| 写、读交付目录 | `CURATION_OUTPUT_TOS_ACCESS_KEY` / `CURATION_OUTPUT_TOS_SECRET_KEY`（可选 `CURATION_OUTPUT_TOS_SESSION_TOKEN`） |
| 上面某一组没设时的回落 | `TOS_ACCESS_KEY` / `TOS_SECRET_KEY`（v1 既有） |
| `curation task …` | `CURATOR_URL`（含挂载前缀，如 `https://host/curation`，也可用 `--url`）、`CURATOR_USER` / `CURATOR_PASSWORD` |

一组变量只设了一半（只有 AK 或只有 SK）会直接报错，不会悄悄回落到另一组。输入永远不借用输出的密钥，反之亦然。地区与端点沿用 v1 规则：`--input-region` / `--output-region` > `--region` > `TOS_REGION` > `TOS_ENDPOINT` 里的地区 > `cn-beijing`。

## 各命令要点

**preflight**：`curation preflight --input <tos://… | 本地目录 | 公共数据集名> [--source tos|public|local] [--vlm-backend 名] [--embodiment-id 型号] [--modules a,b] [--source-manifest 文件] --json`

- 只列目录、只读 `meta/` 下的文件，不读 parquet 和视频。
- 不是 LeRobot v2/v3（`.rrd`、mcap、Lance、其他 LeRobot 版本、认不出的目录）时，所有模块都是 `unsupported`，原因写明检测到的格式。
- `info.json` 结构校验沿用 v1 的 `validate_info`，报错原文放进 `validation`（中文，照抄给用户）。
- 运动学极限：型号读到且在规格库里是 `available`；读到但不在规格库里是 `unsupported`，只跳过这一项，其余模块照常；读不到（缺失、空串或 `unknown`）是 `needs_input`，`input_hint.options` 列出规格库的 9 个型号。`--embodiment-id` 覆盖 `robot_type`，与 v1 相同。
- 两个 VLM 模块：没传 `--vlm-backend` 是 `needs_input`（`input_hint.field = "vlm"`）；没有任务标注不会让它们标灰，只在 `notes` 里提示会先补描述。
- `--modules` 只报告所选模块，没选的模块不追问。
- 原因文案用英文（02 篇的 CLI 约定），`validation` 里 v1 的报错保持中文原文。

**snapshot**：`curation snapshot --input … [--episodes 表达式] --out <运行目录>/source_manifest.json --json`

- 记录 `meta/` 下全部文件和所选 episode 的 parquet、各机位视频。TOS 记 ETag，本地记 `mtime_ns`。
- `--episodes` 支持 `34`、`10-20`、`3,10-12` 和 `@文件`（每行一个表达式，`#` 之后是注释）；全部越界报参数错误，部分越界跑交集并警告。
- 之后任何读源数据的命令带上 `--source-manifest`，读到的对象有一个变了就以退出码 6 结束。本批里 `preflight` 接受它，校验 `meta/` 下的对象。

**verify**：`curation verify --run-dir <本地运行目录> --output <交付目录下的 run_id 目录> [--visibility-timeout 60] --json`

- 关键文件 = 运行目录里的全部文件（排除 `logs/`、`inflight.json`、隐藏文件和临时文件）加上 `export/manifest.json` 里列出的数据集文件（`export/lerobot_curated/` 下）。
- 逐个检查：存在（`missing`）、大小与本地一致（`size_mismatch`）、开头不是全零（`zero_filled`）、能解析（`unparseable`：JSON / JSONL 整体解析，parquet 看首尾魔数并解析 footer，mp4 找 `moov`，JPEG / PNG 看魔数）。列举里有但读不到的文件在 `--visibility-timeout` 秒内反复重试，仍读不到记 `not_visible_in_time`。
- mp4 和 parquet 只按范围读头尾，不下载整个文件。
- 全部通过才最后写 `_COMPLETE`；没通过时如果交付目录里有旧的 `_COMPLETE`，会把它删掉。

**task**：`curation task create --file task.json [--wait]`、`list`、`get`、`wait`、`start|pause|resume|stop`、`retry [--modules a,b]`、`continue`、`report [--rev N]`、`adjudication`

- `--json` 时原样打印 Daemon 的响应，包括 `links`。`adjudication` 汇总待裁决条数和裁决页链接（裁决本身只能在网页上做）。
- `wait` 每 `--poll-interval` 秒（默认 5）查一次，直到任务进入终态且没有运行中的子任务；`--timeout` 到了就打印当时的任务 JSON 并以 0 退出，stderr 有一条警告，调用方看 `state` 判断。
- 写接口可带 `--idempotency-key`，重试时用同一个 key，Daemon 不会重复执行。
- Daemon 的错误保留 CLI 退出码：连不上、401/403、5xx、`precheck_failed` 退出码 3；`source_changed` 退出码 6；其余 4xx（如 `task_state_conflict`、`not_found`）退出码 2。REST 错误体原样放在 `error.details.rest_error`。

## 手动验证步骤

在仓库的 `backend/` 目录下执行。Python 用仓库根目录的虚拟环境，临时文件放在 `$TMPDIR`：

```bash
cd backend
PY=../.venv/bin/python
D=${TMPDIR:-/tmp}/curation-cli-demo && rm -rf "$D" && mkdir -p "$D"
PYTHONPATH=../tools $PY -m parity make-fixture --out "$D/mini"      # 8 条 episode 的 LeRobot v2.1 数据集
```

1. 入口与帮助：`$PY -m curation.cli --help` 列出 `preflight`、`snapshot`、`verify`、`task`，末尾列出 v1 命令；`$PY -m curation.cli --version` 输出 `curation <版本>`；`$PY -m curation.cli ls "$D/mini"` 仍是 v1 的中文输出。

2. 预检（给人看、给机器看）：

   ```bash
   $PY -m curation.cli preflight --input "$D/mini"
   $PY -m curation.cli preflight --input "$D/mini" --vlm-backend ark --json > "$D/pf.json" 2> "$D/pf.err"; echo "exit=$?"
   $PY -c "import json,sys; from curation.contracts import schemas as s; \
   print(s.errors('cli/preflight.schema.json', json.load(open(sys.argv[1])))); \
   print([s.errors('progress.schema.json', json.loads(l)) for l in open(sys.argv[2])])" "$D/pf.json" "$D/pf.err"
   ```

   应看到：`exit=0`，两个列表都是空的；`format.version` 为 `v2`，`labels` 为 6 条有标注、2 条无标注，两个 VLM 模块是 `available` 并带「2 episodes have no task text」的提示。去掉 `--vlm-backend` 再跑，两个 VLM 模块变成 `needs_input`。

3. F2.7 的几条规则（每条改完 `info.json` 都用 `preflight --json` 看 `kinematic_limits` 那一项；带 `--json` 时 stderr 上是 JSON Lines 日志，只看结果可以加 `2>/dev/null`）：

   ```bash
   cp -r "$D/mini" "$D/umi"
   $PY -c "import json,sys; p=sys.argv[1]; i=json.load(open(p)); i['robot_type']='umi_dual_handheld_gripper'; json.dump(i, open(p,'w'))" "$D/umi/meta/info.json"
   $PY -m curation.cli preflight --input "$D/umi" --vlm-backend ark --json      # 只有 kinematic_limits 是 unsupported，退出码 0
   $PY -c "import json,sys; p=sys.argv[1]; i=json.load(open(p)); i.pop('robot_type'); json.dump(i, open(p,'w'))" "$D/umi/meta/info.json"
   $PY -m curation.cli preflight --input "$D/umi" --json                         # needs_input，options 是 9 个型号
   $PY -m curation.cli preflight --input "$D/umi" --modules timestamp_check,dedup --json   # 只报两个模块，不追问
   mkdir -p "$D/rrd" && touch "$D/rrd/episode_000000.rrd"
   $PY -m curation.cli preflight --input "$D/rrd" --json                         # 8 个模块全部 unsupported，原因写 detected .rrd
   ```

4. 固化源文件清单，然后改动源数据：

   ```bash
   $PY -m curation.cli snapshot --input "$D/mini" --episodes 0-3 --out "$D/run/source_manifest.json"
   $PY -m curation.cli preflight --input "$D/mini" --source-manifest "$D/run/source_manifest.json" --json > /dev/null; echo "exit=$?"   # exit=0
   echo '{"episode_index": 8, "tasks": [], "length": 1}' >> "$D/mini/meta/episodes.jsonl"
   $PY -m curation.cli preflight --input "$D/mini" --source-manifest "$D/run/source_manifest.json" --json; echo "exit=$?"
   ```

   最后一条应输出 `code` 为 `source_changed`、`details.key` 为 `meta/episodes.jsonl` 的错误信封，`exit=6`。

5. 交付核验（本地目录充当交付目录）：

   ```bash
   mkdir -p "$D/work/checks/timestamp_check" "$D/work/logs"
   echo '{"episodes": [0, 1]}' > "$D/work/passed.json"
   echo '{"episode_index": 0, "verdict": "pass"}' > "$D/work/checks/timestamp_check/results.jsonl"
   echo '{"ts": 1}' > "$D/work/logs/verify.jsonl"                 # 进行中的日志，不参与核验
   mkdir -p "$D/out" && cp -r "$D/work/passed.json" "$D/work/checks" "$D/out/"
   $PY -m curation.cli verify --run-dir "$D/work" --output "$D/out" --json; ls "$D/out"
   ```

   应输出 `"checked": 2, "failed": [], "complete_marker": true`，`$D/out` 里出现 `_COMPLETE`。再把交付里的文件写成全零后重跑：

   ```bash
   $PY -c "import sys; p=sys.argv[1]; n=len(open(p,'rb').read()); open(p,'wb').write(bytes(n))" "$D/out/passed.json"
   $PY -m curation.cli verify --run-dir "$D/work" --output "$D/out" --visibility-timeout 0 --json; ls "$D/out"
   ```

   应输出 `{"path": "passed.json", "reason": "zero_filled"}`、`complete_marker` 为 `false`，`_COMPLETE` 被删掉。

6. `curation task …`（Daemon 还在 W4 开发中，用测试里的桩服务代替）。另开一个终端，在 `backend/` 下启动桩：

   ```bash
   ../.venv/bin/python -c "
   import time
   from tests.cli.fakes import StubDaemon, make_task
   with StubDaemon(port=8765) as d:
       d.tasks['task_01'] = make_task('task_01', 'running', pending=3)
       print(d.url, flush=True); time.sleep(3600)"
   ```

   回到原终端：

   ```bash
   export CURATOR_URL=http://127.0.0.1:8765/curation CURATOR_USER=agent CURATOR_PASSWORD=s3cret-pw
   $PY -m curation.cli task get task_01                 # 状态、待裁决条数和三条链接
   $PY -m curation.cli task adjudication task_01 --json # pending 3，附裁决页链接
   $PY -m curation.cli task resume task_01 --json; echo "exit=$?"   # 409 task_state_conflict → exit=2，details.rest_error 是 REST 错误体
   $PY -m curation.cli task wait task_01 --poll-interval 1 --json & sleep 3; kill -TERM $!; wait $!; echo "exit=$?"   # exit=5，信封 code 为 terminated
   CURATOR_PASSWORD=wrong $PY -m curation.cli task get task_01 --json; echo "exit=$?"   # 401 → exit=3
   ```

7. 自动化测试（约 15 秒）：

   ```bash
   $PY -m pytest -q tests/cli tests/contracts
   $PY -m curation.contracts check
   $PY -m pytest -q curation/tests --ignore=curation/tests/test_environment.py   # v1 单测，本机已知 3 条失败（无 GPU / FSX 相关）
   ```
