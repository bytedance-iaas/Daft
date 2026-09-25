# 数据完整性（`data_integrity`）

设计：[`docs/design/14-data-integrity.md`](../../../../docs/design/14-data-integrity.md)，决策 D50–D52。

质检漏斗最前面的一档 `integrity`，只有这一个模块：每条 episode 的文件是不是完整、可读。硬门，新建任务时默认勾选（「完整」「快速」两个预设都有），可以取消。

| 结论 | 什么时候 | 去向 |
|---|---|---|
| 判废 | 文件为空或过小、被截断、有成块的零填充、结构读不出、mcap CRC 不符、v1 的逐条结构校验不过（NaN/Inf、时间戳不是严格递增……）、开了逐帧解码时解码失败 | reject，不进后面的档，**不可复议** |
| 可疑 | 帧数或行数与 episode 表对不上、mcap 录制中断但读得出、与另一条文件完全相同、mcap 某 topic 缺失或频率明显偏低、episode 表前后不一致、解码器掩盖了错误 | 照常进后面的档、照常交付；人工裁决「完整性存疑」计入待裁 |
| 出错 | 存储读失败（超时、5xx、源缓存下载失败） | held，重试补跑（D33） |

数据集级的发现（多余文件、近乎全黑的相机、episode 表的区间重叠）写在 `checks/data_integrity/dataset.json` 与报告里，不影响任何条目。

## 代码

| 文件 | 内容 |
|---|---|
| `judge.py` | `IntegrityJudge`：一次调用做一次数据集级检查（文件清单、v3 episode 表、多余文件、内容相同的文件、v1 的近黑相机先验、mcap 与多数条目比较），再逐条判定 |
| `files.py` | 单个文件的 L1 结构与 L2 整读（parquet、mp4、mcap）；`Blob` 把存储读失败变成 `ReadFailure` |
| `mp4.py` | 不解码读 mp4：顶层 box、moov、视频轨样本表 → 每帧的时间、字节位置与大小 |
| `rows.py` | 逐条读行（v1 的读取函数，不先解析数据集语义），跑 `validate_episode_row` |
| `decode.py` | L3 逐帧解码（模块参数 `decode_test`，默认关）；FFmpeg 错误日志按线程捕获 |
| `findings.py` | 原因码、级别与结论 |
| `report.py` | 报告小节的统计、report.md 的几行、`integrity_findings` 明细表 |

阈值（站点配置 `integrity:` 段，缺省见 `judge.DEFAULTS`）：`count_tolerance_frames`（帧数容差，1）、`majority_ratio`（多数的比例，0.8）、`rate_outlier_ratio`（频率低于中位数的比例，0.8）、`min_peers`（至少几条才做多数比较，3）。

## 手动验证

在仓库的 `backend/` 目录下执行，不需要密钥、不需要模型：

```bash
cd backend
PY=../.venv/bin/python
C="$PY -m curation.cli"
D=$(mktemp -d)
PYTHONPATH=../tools $PY -m parity make-fixture --out "$D/mini"                    # 8 条 LeRobot v2.1
PYTHONPATH=../tools $PY -m parity make-fixture --format mcap --out "$D/mini_mcap" # 同样 8 条的 mcap
```

1. 干净数据集：

   ```bash
   $C check --modules data_integrity --input "$D/mini" --run-dir "$D/run" --episodes 0-7 --json
   $PY -c "import json,sys; [print(r['episode_index'], r['verdict'], r['details']['reason']) for r in map(json.loads, open(sys.argv[1]))]" "$D/run/checks/data_integrity/results.jsonl"
   ```

   应看到 `pass 6, abstain 2`：3 与 7 是夹具故意做的字节级复制品，理由是「需要人工裁决：exterior 相机的视频与 ep 7 的内容完全相同（另有 1 项发现）」；`$D/run/checks/data_integrity/dataset.json` 的 `episode_findings` 只有 3 和 7。

2. 几种损坏，每条一种：

   ```bash
   cp -r "$D/mini" "$D/bad"
   V=$D/bad/videos/chunk-000
   : > "$V/observation.images.wrist/episode_000000.mp4"                                                          # 0：空文件
   $PY -c "import os,sys; p=sys.argv[1]; os.truncate(p, os.path.getsize(p)*6//10)" "$V/observation.images.exterior/episode_000001.mp4"   # 1：moov 被截掉
   $PY -c "import os,sys; p=sys.argv[1]; os.truncate(p, os.path.getsize(p)-20)" "$D/bad/data/chunk-000/episode_000002.parquet"          # 2：parquet 被截
   $PY -c "import sys; f=open(sys.argv[1],'r+b'); f.write(bytes(4096))" "$V/observation.images.exterior/episode_000004.mp4"            # 4：文件头置零
   $C check --modules data_integrity --input "$D/bad" --run-dir "$D/run_bad" --episodes 0-7 --json
   ```

   应看到 0、1、2、4 判废（`file_empty`、`file_truncated`、`file_truncated`、`zero_filled`），理由写明哪个文件、哪路相机；5、6 通过，3、7 仍是可疑。

3. mcap：

   ```bash
   cp -r "$D/mini_mcap" "$D/bad_mcap"
   $PY -c "import os,sys; p=sys.argv[1]; os.truncate(p, os.path.getsize(p)*6//10)" "$D/bad_mcap/episode_4.mcap"   # 录制中断
   $PY -c "import os,sys; p=sys.argv[1]; n=os.path.getsize(p)//3; f=open(p,'r+b'); f.seek(n); b=f.read(1); f.seek(n); f.write(bytes([b[0]^255]))" "$D/bad_mcap/episode_6.mcap"
   $C check --modules data_integrity --input "$D/bad_mcap" --run-dir "$D/run_mcap" --episodes 0-7 --json
   ```

   应看到 4 判废（「录制中断，且读不出数据」），6 判废（「一个数据块的 CRC 校验不符」），干净的条目的 `details.files[0].crc` 是 `mcap_chunk`。

4. 逐帧解码：在第 1 步的目录上加 `--resume --param data_integrity.decode_test=true` 再跑，`skipped_existing` 是 0（换了参数全部重做），每条 `details.tiers.L3` 为 `true`。

5. 判决与裁决：把第 2 步的结果接着 `aggregate`（见 CLI README 第 3 步）——判废的条目在 `reject.json`，理由以「未通过「数据完整性」」开头、不出现在「被拒复议」；可疑且留在 passed 的出 `integrity_suspect` 卡片。裁决取值 `intact`（数据无误，保留）/ `broken`（确有问题，判废）/ `unsure`。

6. 控制台：`npm run dev` 看模拟数据——新建任务的「质检范围」第一张卡片是「数据完整性」（默认勾选），第二屏有「逐帧解码测试」开关与耗时说明；报告里有「数据完整性」小节，Episode 明细里有它的一块（发现与检查过的文件）。

样本集：`PYTHONPATH=../tools $PY -m tests.cli.integrity_samples --out "$D/samples"` 生成 `lerobot_v2/`、`lerobot_v3/`、`mcap/` 三份损坏样本，每份的 `damage.json` 写明每条动了什么、应得什么结论；测试用的就是这几个函数。

自动化测试：`$PY -m pytest -q tests/cli/test_integrity.py tests/cli/test_integrity_adjudication.py tests/cli/test_preflight_integrity.py`（约半分钟）；Daemon 端到端（完整性层排在最前、实时面板、裁决队列）是 `tests/orchestr/test_pipeline_batches.py::test_the_data_integrity_layer_goes_first_and_its_episodes_are_readable_live`。
