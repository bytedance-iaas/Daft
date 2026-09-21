# 增量重新导出（W7 / F2.5）

`curation export [--incremental]` 背后的库。设计见 `docs/design/06-delivery-and-report.md` §4、`02-cli-contract.md` §3.7；
契约是 `docs/contracts/cli/export.schema.json`（`--json` 输出）和 `cli/export-manifest.schema.json`（`manifest.json`）。
CLI 本身由 W3 接，这里只提供库入口。

| 文件 | 内容 |
|---|---|
| `incremental.py` | 入口：`export_run`、`export_dataset`、`resolve_revision_dir`；异常类型 |
| `manifest.py` | `manifest.json` 与 `manifest.detail.json` 的模型、`content_key` / `task_key` / 导出指纹 |
| `diff.py` | 上次导出 vs 新 passed 名单：keep / relabel / renumber / add / drop |
| `incremental_base.py` | 两种格式共用的流程：任务文本、任务表、落盘、日志与进度、上次导出能否信任 |
| `incremental_v2.py`、`incremental_v3.py` | LeRobot v2 / v3 两条路径 |
| `target.py`、`source.py` | 写端（整文件拷贝 + 原子改名）；读端（源对象身份、按 `source_manifest.json` 核对） |
| `episode_stats.py` | v2.1 源缺 `meta/episodes_stats.jsonl` 时补算逐条统计 |

## 一、怎么调

```python
import json
from curation.export.incremental import export_run

outcome = export_run("<run-dir>", "tos://bucket/datasets/x", incremental=True,
                     revision=None, concurrency=1, log=on_log, progress=on_progress)
print(json.dumps(outcome.result, ensure_ascii=False))     # 即 cli/export.schema.json
```

- 导出 `<run-dir>/revisions/r<NNNN>/passed.json`，不指定 `revision` 时取编号最大、带 `commit.json` 的那一版；按清单顺序重新编号。
- **待裁决条目在 passed 里，照常导出；待补跑条目在 held 里，不导出**（D24、D35）。passed 与 held 有交集直接报错。
- 开工前删掉 `<run-dir>/_COMPLETE`（06 篇 §4.4：先删 `_COMPLETE` → 改 → 核验 → 再写），由 `curation verify` 核验通过后重写。
- `<run-dir>/source_manifest.json` 存在时，读到的每个源对象都按它核对大小与 ETag / mtime，对不上抛 `SourceChangedError`。
- 异常与退出码的对应：`ExportInputError` → 2，`SourceChangedError` → 6（带 `exit_code`），其余 `ExportError` 与读不到源数据由 CLI 映射。
- `log(level, msg)`、`progress(done, total)` 两个回调给 CLI 转成 C3 的 JSON Lines；v1 代码里的 `print` 在导出期间被收进 `log`，stdout 保持干净。进度回调最多一秒一次。
- 不走运行目录时用 `export_dataset(source, passed, output_dir, ...)`，`passed` 可以是 `passed.json` 的路径、文档或它的 `episodes` 列表。
- Daemon 算 `delivery_stale`：`export_fingerprint(passed 条目, source_format=..., source_digest=<source_manifest 的 summary.digest>, params=...)`
  与上次成功导出的 `result["fingerprint"]` 比。纯函数，不读数据集；只改了标、名单没动，指纹同样会变。
- 参数：`params={"video_file_mb": ..., "data_file_mb": ...}`（v3 的分文件阈值，缺省取源 `info.json`，再缺省 200 / 100 MB，与 v1 相同）；
  `concurrency` 给文件级并行（默认 1，不并发）；临时文件放 `scratch_dir` / `$CURATION_EXPORT_SCRATCH`，v1 的编码器用 `$TMPDIR`，部署时都指向 scratch 卷。

## 二、产物

```
<run-dir>/export/
├── manifest.json          C2 契约：每条交付 episode 的源编号、新编号、content_key、task_key、所在文件
├── manifest.detail.json   契约放不下的：任务文本与来源、帧布局（全局帧偏移、task_index、v3 视频窗口）、
│                          每个交付文件的大小 / sha256 / 内容描述、导出参数、任务表。两份文件带同一个指纹
├── _EXPORTING             只在导出改动目录期间存在；看到它说明上次导出半途中断
└── lerobot_curated/       交付数据集
    └── meta/curation_episodes.jsonl   新编号 ↔ 源编号、任务文本、instruction_source（原始标注 / 自产caption / 人工改标 / 无）
```

`manifest.json` 最后写，是提交点。任务文本按来源写：自产 caption 和人工改标替换该条的任务，原始标注保持源数据的任务（v1 的 `task_overrides` 语义）。

## 三、增量规则

| 类别 | 条件 | v2（每条独立 parquet + mp4） | v3（多条合并进同一文件） |
|---|---|---|---|
| keep | 源内容、编号、任务都没变 | 一个字节都不动 | 不动（所在帧表文件若因别的条目重写，行内容不变） |
| relabel | 只有任务变了 | 任务编号变了才重写这一条的帧表；视频不动 | 任务编号变了才重写它所在的帧表文件；视频不动 |
| renumber | 编号或全局帧偏移变了 | 帧表按源重写；视频**改名**，不拷贝字节 | 所在帧表文件重写；视频文件不动（只有窗口指向它） |
| add | 新进来的，或源内容变了 | 帧表从源写；视频从源整文件拷贝 | 帧表插到前一条所在的文件；视频编进新文件（接在已有文件之后） |
| drop | 不再交付 | 删掉；空出的位置被改名覆盖 | 它所在的视频文件用剩下条目的源窗口**重编码**；帧表文件重写 |

- meta 文件每次都在本地重建（KB 级），字节有变化才拷进去。
- 任务表：首次和全量导出按 v1 的规则（首次出现顺序）；增量导出保留上一版的编号——空出来的编号先给新文本，还空着就拿最后一个文本补位，
  剩下的新文本追加在后面。按 v1 的规则，一次改标插进来的新文本会让后面所有文本改号、后面所有帧表重写。
- 只改任务文本的条目，不会触发任何视频的拷贝、解码或重编码。

## 四、什么时候退回全量

增量只在上次导出可信时才做，否则重新全量导出并在日志里说原因（`outcome.rebuild_reasons`）：

- 没有上次导出，或调用方没开 `--incremental`；
- `_EXPORTING` 还在（上次导出中断）；`manifest.json` 与 `manifest.detail.json` 对不上；
- 有交付文件缺失或大小变了；
- 导出器版本（`EXPORT_IMPL_VERSION`）、源格式或导出参数变了。

## 五、与 v1 的关系

- 全量导出的产物与 v1 的 `export_lerobot_v2` / `export_lerobot_v3` **逐字节一致**（测试守着），另外多一个 `meta/curation_episodes.jsonl`；
  写字节的地方都是 v1 的 A 类代码原样调用（`_to_parquet_fsx_safe`、`_RollingParquetWriter`、`_RollingVideoWriter`、`_reencode_concat`、`_copy_v2_stats` 等）。
- v2.1 源缺 `meta/episodes_stats.jsonl` 时补算（按 lerobot 的算法）。官方 v2.1 loader 必须读这个文件，缺了就去 Hub 下载并失败；
  v1 只在源有时才拷，所以这类源的 v1 交付打不开。
- 交付文件的权限跟所在目录一致（`mkstemp` 默认 0600，v1 的 `safe_write._publish` 也是这样）。
- 增量导出后 v3 的文件布局与全量导出不同（新增条目进新文件），内容等价；v2 增量与全量逐字节一致（任务表有改标时编号可能不同）。

## 六、FSX 纪律

FSX 挂载拒绝随机写。交付目录里**不产生**任何文件：编码器、parquet 写入都在本地 scratch 完成，
进交付目录的只有顺序整文件拷贝（先拷到同目录的 `.curation-pub-*`，再原子改名），以及同一挂载内的改名和删除。
v2 的视频源在 TOS 上时，先整文件下载到本地 scratch 再拷。测试 `test_only_whole_file_copies_land_in_the_delivery`、
`test_encoders_and_parquet_writers_never_write_into_the_delivery` 拦截了 PyAV、pyarrow 与 `open()`，验证交付目录里只出现 `.curation-pub-*` 的顺序写。

## 七、手动验证步骤

以下命令都在仓库根目录执行，临时文件放在仓库外。

### 1. 测试（共享 venv）

```bash
cd backend
../.venv/bin/python -m pytest -q tests/export tests/contracts    # 约 30 秒，官方 loader 用例在这里跳过（3 skipped）
../.venv/bin/python -m curation.contracts check                   # 无输出、退出码 0
cd ..
```

### 2. 走一遍增量重导出

```bash
export W7=$(mktemp -d)
.venv/bin/python backend/tests/export/demo_reexport.py --work "$W7" --format v2
.venv/bin/python backend/tests/export/demo_reexport.py --work "$W7" --format v3
```

逐项核对 v2 的输出：

- r1：`"incremental": false`，5 条，`videos_copied` 10；源 episode 2（held）不在 `$W7/v2-run/export/lerobot_curated/meta/curation_episodes.jsonl` 里，episode 1（待裁决）在；
- r2（剔除中间的 episode 3）：`"incremental": true`，`diff` 为 keep 2 / renumber 2 / drop 1，`videos_copied` 0；
  4 个视频是 `renamed`，前两条的 parquet 与视频是 untouched，只删了 `episode_000004.parquet`；
- r3（给 episode 4 改标）：relabel 1，只重写了三个 meta 文件，没有任何视频或帧表被写。

v3 的输出：r2 的 `videos_reencoded` 是 2（每路相机只重编码含 episode 2 的那个文件），另两个视频文件 untouched；r3 的 `videos_reencoded` 为 0，只写 meta。

脚本里的 untouched 指 inode、修改时间、字节三者都没变；`renamed` 行后面打印了改名前后的 inode，两者相同，说明是改名而不是拷贝。

### 3. 官方 LeRobot loader（独立 venv，不装进共享 venv）

lerobot 0.3.x 只读 v2.1，0.4 起只读 v3.0，所以建两个 venv（在 macOS 上实测过；pip 需要 `--use-feature=truststore`）：

```bash
# v2.1：lerobot 0.3.3，依赖钉在它发布时的版本（torchvision 0.21 没有视频接口的弃用警告）
/opt/homebrew/bin/python3.12 -m venv ~/.cache/curator-lerobot-v21
~/.cache/curator-lerobot-v21/bin/python -m pip install --use-feature=truststore \
  "lerobot==0.3.3" "torch==2.6.0" "torchvision==0.21.0" "torchcodec==0.2.1" \
  "huggingface_hub[cli,hf-transfer]==0.34.4" "datasets==3.6.0" "numpy==2.2.6" "pandas==2.3.3" \
  "pyarrow==21.0.0" "av==15.1.0" "opencv-python-headless==4.12.0.88" pytest jsonschema referencing pyyaml

# v3.0：lerobot 0.6.1（要求 Python 3.12+）
/opt/homebrew/bin/python3.12 -m venv ~/.cache/curator-lerobot-v30
~/.cache/curator-lerobot-v30/bin/python -m pip install --use-feature=truststore \
  "lerobot[dataset]==0.6.1" "opencv-python-headless==4.12.0.88" pytest jsonschema referencing pyyaml
```

跑验收用例（同一个测试文件，各自跑自己能读的格式，另一种跳过）：

```bash
cd backend
~/.cache/curator-lerobot-v21/bin/python -m pytest -q tests/export/test_lerobot_loader.py -rs -p no:cacheprovider   # 2 passed, 1 skipped
~/.cache/curator-lerobot-v30/bin/python -m pytest -q tests/export/test_lerobot_loader.py -rs -p no:cacheprovider   # 1 passed, 2 skipped
cd ..
```

也可以直接检查第 2 步的产物：

```bash
~/.cache/curator-lerobot-v21/bin/python backend/tests/export/lerobot_check.py "$W7/v2-run/export/lerobot_curated" --expect-episodes 4
~/.cache/curator-lerobot-v30/bin/python backend/tests/export/lerobot_check.py "$W7/v3-run/export/lerobot_curated" --expect-episodes 5
```

输出里 `"ok": true`、`"warnings": []`、`"problems": []`，退出码 0。检查读每条 episode 的首、中、尾三帧（全部相机）；
「无警告」按字面算：打开数据集和读帧期间的 Python 警告与 WARNING 及以上的日志都算。
视频后端固定用 `pyav`：不指定时 lerobot 会先试 torchcodec，本机的 FFmpeg 8 加载不了它，会打一条关于机器、不关于数据集的警告。
测试里还有一个反例（`test_v2_check_catches_a_broken_delivery`）：把有时间戳跳变的 episode 2 也导出去，loader 必须拒绝。

## 八、已知限制

- 写端只实现了文件系统目录（本地盘或 FSX 挂载）。交付到 `tos://` 时，由调用方把 `outcome` 里的变化同步上去：
  上传 `written`，`renamed` 做服务端复制（或重传），删除 `deleted`，最后传 `manifest.detail.json`、`manifest.json`。
- 本地工作目录被清理（终态 7 天）后，增量导出需要先把 `export/` 整个回灌（含视频），否则大小对不上，自动退回全量导出。
- v3 在增量导出中新增的条目编进新的视频文件，多次补回会留下偏小的文件；需要时用全量导出（不带 `--incremental`）重排。
- v2.0 源没有 `meta/stats.json` 时交付打不开（与 v1 相同，日志里会警告）。
