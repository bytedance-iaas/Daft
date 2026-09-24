# 对账工具（W0 / F1.1）

v2 重构的安全网。**2026-09-24（设计 13）起基线是 v2 自录的**：设计 13 把判定协议换成连续视频，
冻结点的 v1（图片探针）在定义上无法回答 v2 的请求，v1↔v2 逐位对账就此退役；改为
`run-v2 --fake-vlm` 对合成数据集录一盘新黄金基线（结果 + 录制带），之后的改动回放这盘带子对答案——
模型被钉死，任何差异都来自代码。测试见 `tests/test_v2_parity.py`。
历史：基线原先是 v1 自己的代码生成的（D15/D19/D33/D34/D44 口径见
[`docs/design/10-parity-and-migration.md`](../../docs/design/10-parity-and-migration.md) §3、
`00-overview.md` §7），冻结点 `dev` 的 `eb637ba40`（D44）仍留在 git 历史里，
`dump-v1`、`v1_manifest.json`（`manifest.py` 的 `DEFAULT_COMMIT` 是唯一出处）与 `test_dump_v1_e2e`
继续作为工具自身与 v1 行为的离线回归存在，不再作为 v2 的对照。

**冻结点**：`dev` 的 `eb637ba40`（2026-09-22，PR #155「mcap 与 lance 格式的质检」合入后的头部，D44）。
此前是 `release_v1` 的 `45bdf9292`（D34）；两者之间只有 PR #155，它对 LeRobot 数据集的判决没有影响
（格式嗅探多认两种格式、收尾多清两种缓存），合成数据集上 v1 对 v2 的逐位对账照样全过。
`manifest.py` 的 `DEFAULT_COMMIT` 是唯一出处，`v1-manifest`、`v1-src`、`pack` 和测试都从它取。

| 命令（`python -m parity …`） | 作用 |
|---|---|
| `dump-v1` | 在同一进程里跑一遍 v1 原版 `curation run`（或 `curation rejudge`：执行人工裁决），挂钩取数，导出规范化结果；同时录制或回放全部模型调用 |
| `run-v2` | 按 Daemon 的顺序跑一遍 v2 的原子命令（preflight → … → verify），模型调用走同一套录制带挂钩（W3）；带 `--from … --decisions …` 时在一份已跑完的运行目录副本上跑裁决序列 |
| `compare` | 比较两份导出（或 v2 运行目录）：确定性六项逐位比，VLM 三项按判决比（可带噪声底），终判清单比，回放命中率与调用图；金标是 v1 的 rejudge 时比裁决（见下） |
| `make-fixture` | 生成 8 条 episode 的合成 LeRobot v2 数据集（真视频），离线测试用；`--format mcap` / `--format lance` 是同样 8 条的 mcap（每条一个 cdr 编码的 `.mcap`）与 Lance（lerobot-lance-convert 的三表布局）版本（D44） |
| `tape-summary` | 看一盘录制带：各类调用多少次、有没有失败 |
| `pack` | 打一个带进 Pod 的包：冻结点的 v1 源码 + 当前的对账工具 |
| `archive` / `fetch` | 导出结果连同 `MANIFEST.json`（逐文件 sha256）上传到 TOS / 取回并校验 |
| `v1-manifest` | 从 git 重新生成 `v1_manifest.json`（冻结点逐文件的 blob 哈希） |
| `v1-src` | 从 git 取出冻结点的 v1 源码树，作为本地运行 `dump-v1` 的 `--v1-src` |
| `a-class-check` | A 类算法文件与冻结点逐个比对；有差异且 PR 描述里没有 `parity-change:` 说明就失败（CI 里跑） |

所有命令都在仓库根目录、以 `PYTHONPATH=tools` 运行。

## 它怎么工作

- **只读 v1**：`dump-v1` 启动时按 `v1_manifest.json` 逐文件核对 v1 源码（冻结点 `eb637ba40`，D44），
  对不上就拒跑。之后只在运行期间包几个函数取数，v1 的文件一个字不改：
  - 包 `daft.DataFrame.collect`，在硬门过滤**之前**截下每条的检查结果（v1 自己的报告里，被硬门拦下的条目只剩拦下它的那一项）；
  - 包 `requests` 与 `vlm_client.hedged_request`，录下或回放每一次模型调用（对冲补发只认赢的那一发）；
  - 包 `run_pipeline`、`save_report`、`_skill_profile_stage`、`caption_episodes`、`action_hash`、`episode_fingerprint`、`decode_window`，
    拿到判决、报告、技能画像、补打的任务描述、去重遍历顺序和解码失败；
  - 跑 `rejudge` 时再包 `run_rejudge` 与 `apply_decisions`，拿到它改的是哪次跑批、每条改标重判的结果。
- **录制带**（`vlm_tape.jsonl.gz`）：每行一次调用，存请求的规范形式（提示词全文、每张图的 sha256、模型参数，不存图片本身）
  和当时的响应。回放时按请求哈希取回响应；v2 的请求只要和 v1 差一个字、一帧、一个 JPEG 参数，哈希就对不上。
  格式见 [`docs/contracts/parity/vlm-tape-entry.schema.json`](../../docs/contracts/parity/vlm-tape-entry.schema.json)。
- **规范化记录**：每个模块每条 episode 一行，格式与 v2 的 `checks/<module>/results.jsonl` 相同，
  见 [`docs/contracts/cli/result-record.schema.json`](../../docs/contracts/cli/result-record.schema.json)。
  `verdict` 取 `pass` / `fail` / `abstain` / `scored`（打分项，不投票）/ `error`（D33：降级得来的结论也算出错）。
- **v2 一侧**（`run-v2`，设计 11 篇 §3）：在同一进程里按 Daemon 的顺序依次调 v2 的原子命令，
  每档读上一档的幸存者（`--survivors-out`），写出一个普通的 v2 运行目录，`compare` 直接读它（`checks/*/results.jsonl`、
  `revisions/r0001/` 的清单、`autolabel/`、`checks/dedup/groups.json`、`checks/skill_profile/`）。
  `--replay` 用 v1 的录制带回答；`--fake-vlm` 用内置假模型现答并录一盘新带。调模型的命令都带 `--hedge`
  （v1 总是对冲，挂钩替换的正是对冲函数）和 `--concurrency 64`（N=64 时八把闸门与 v1 出厂值逐项相等）。
  `/models` 探活不算模型调用：v2 每条命令探一次、v1 一次运行探两次，回放时这类请求可重复取用，不计入命中和剩余。
- **假模型**（`fakevlm.py`，`--fake-vlm` 与 CLI 测试的假端点都用它）：按提示词的格式作答，答案由请求的文字、图片张数和
  每张图的像素尺寸决定（`FakeVlm.answer_key`），不看图片字节——同一帧在 macOS 和 Linux 上编出的 JPEG 字节不同，
  按字节选答案会让同一个测试在两个平台上得到不同的判决。录制带的请求哈希仍按字节算，回放要逐字节对上。
  同一任务的条目发的文字相同，答案由 `SEED` 决定：取 11，合成数据集的 task_success 走遍各条路径
  （0、3、7 弃权，1、4、6 靠仲裁判成功，4、6 的补打描述与原标注不同）。
- **人工裁决也对账**（D39）：`dump-v1 … -- rejudge` 让 v1 在自己的交付上执行 `human-decisions/` 里的裁决，
  挂钩取出它按新标注重判的每条（`_build_rerun`：多视角打分 + 逐机位复核两层）、裁决后的三件套和技能归类，并录下这期间的调用；
  `run-v2 --from <v2 运行目录> --decisions <decisions.json> --replay <这盘带>` 在 v2 运行目录的副本上按 Daemon 的顺序跑
  adjudicate-apply → 重判改标条目 → 漏斗 → 增量技能画像 → 终判 → 报告（下一个结果版本）。`compare` 认出金标是 rejudge，
  只比这几样：重判的 task_success 记录逐位一致（v2 的记录另外写明用哪段文字、按哪种口径判的——新标注、`人工改标`、`v1`，
  v1 把它们记在交付条目上），裁决后的技能归类逐条一致，终判清单一致，回放无缺无余。
  v1 的 rejudge 不探活端点，v2 每条命令探一次；带子上没有 `/models` 时 `run-v2` 自己应答（探活不是模型调用）。
- **调用图一致**：`compare --all-strict` 的回放一项要求 misses 为 0（v2 的每个请求都在 v1 的带子上）且
  没有剩余（带子上的每个请求 v2 都发了）。多一个、少一个、提示词差一个字，都会失败。
- **干净的基线**：导出结束时汇总所有「执行出错」的迹象，包括录制带里失败的调用、打分回答解析不了、v1 降级留下的痕迹和解码失败。
  有任何一项，`dump.json` 的 `status` 就是 `dirty`，退出码为 3。这样的基线不收，用「回放 + 补录」再跑一遍，
  直到状态变为 `clean`（见下文第 5 步）。

## 本地环境

v1 的依赖按 `backend/requirements.txt` 的版本装在仓库根目录的 `.venv` 里（本目录的测试也用它）：

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install --use-feature=truststore daft==0.7.16 numpy==2.2.6 pandas==2.3.3 \
  pyarrow==24.0.0 opencv-python-headless==4.12.0.88 av==15.1.0 requests==2.34.2 PyYAML==6.0.3 \
  tos==2.9.2 matplotlib==3.10.9 bcrypt==4.3.0 pytest jsonschema "scipy==1.16.3"
```

两处和 `requirements.txt` 不同，都只影响本机：
- 没装 gradio 和 rerun-sdk：它们只在 Web UI 和 `.rrd` 格式里用到。
- scipy 用 1.16.3：1.15.3 的 macOS wheel 在 macOS 27 上加载会报错（dyld：`__thread_bss` zero-fill section）。
  1.18 又改了 `Rotation.from_euler` 的行为，会挂一条 v1 单测。

本机与 Pod 的 CPU 架构不同，解码缩放不保证逐位一致，所以**本机生成的导出只能和本机的比**，基线必须在 Pod 里生成。

## 手动验证步骤（离线，不需要任何密钥）

在仓库根目录执行，约 1 分钟：

```bash
export PYTHONPATH=tools
python=.venv/bin/python
W=$(mktemp -d)

# 0. 从 git 取出冻结点的 v1（工作区里的 v1 已在重组中改动，不能再当基线用）
V1=$($python -m parity v1-src --out $W/v1)

# 1. 合成数据集：8 条，含一条时间戳跳变、一条残段、一对字节级重复、两条无标注
$python -m parity make-fixture --out $W/mini

# 2. 用内置的假模型跑一遍 v1，录制全部调用 → 期望 [dump-v1] clean，退出码 0
$python -m parity dump-v1 --out $W/rec --v1-src $V1 --fake-vlm -- run --input $W/mini --output $W/rec-out \
  --vlm-endpoint http://fake-vlm.local/v1 --vlm-model fake-vlm

# 3. 不连任何模型，用录制带回放再跑一遍
$python -m parity dump-v1 --out $W/rep --v1-src $V1 --replay $W/rec/vlm_tape.jsonl.gz -- run --input $W/mini \
  --output $W/rep-out --vlm-endpoint http://fake-vlm.local/v1 --vlm-model fake-vlm

# 4. 回放对账：九项全部逐位一致、回放 misses=0 → 最后一行 conclusion: PASS
$python -m parity compare --golden $W/rec --candidate $W/rep --all-strict
```

再用同一盘带子跑 v2 的命令链，和 v1 逐位对账（约半分钟）：

```bash
# 5. v2 的原子命令，按 Daemon 的顺序，回放 v1 的录制带 → [run-v2] done; tape replay: hits=121 misses=0 unused=0
$python -m parity run-v2 --out $W/v2 --input $W/mini --delivery $W/v2-delivery --replay $W/rec/vlm_tape.jsonl.gz

# 6. v1 对 v2：九项逐位一致、终判清单一致、回放无缺无余 → conclusion: PASS
$python -m parity compare --golden $W/rec --candidate $W/v2 --all-strict
```

`$W/v2/parity.json` 记着每一步的命令行、退出码、`--json` 输出和录制带统计；各命令的 stderr 在 `$W/v2/logs/parity-run.log`。
`--fake-vlm` 代替 `--replay …` 时不看带子，由假模型现答，结论应同样是 PASS。

逐项核对：
- 第 2 步的 `$W/rec/final.json` 里，`passed` 是 `[0, 1, 3, 4, 6]`，`reject` 是 `[2, 5, 7]`
  （2 是时间戳跳变，5 是残段，7 与 3 字节级重复）；`v1_views.passed_json` 里却有 7：
  这是 v1 的一个小问题，`passed.json` 没扣掉被去重剔除的条目（交付数据集里是扣掉了的）。
  `review` 是 `[0, 3, 7]`：三条都在 task_success 上弃权。第 6 步比 review 时把 7 摘出来单列
  （「left out of review (rejected by v1 …)」）：v1 还在问一条被它拒掉的副本成败，v2 里被拒的条目没有成败问题，只有复议（D42）。
- `$python -m parity tape-summary $W/rec/vlm_tape.jsonl.gz` 应列出 probe / endstate / arbitration / caption 各类调用，`0 failed calls`。
- 单元测试与端到端测试：`$python -m pytest -q tools/parity/tests`（约 1 分钟；`test_v2_parity.py` 就是第 5–8 步，
  外加「带子上少一个请求时对账必须失败」「按首轮完整流程重判的改标对不上 v1 的 rejudge」两个反例，CI 的对账工具一步里一起跑）。

mcap 与 Lance 的对账（D44，每种约半分钟）：同样 8 条做成两种新格式，v1（新冻结点，带 PR #155）跑一遍录带子，
v2 的命令链回放它（`run-v2` 与 Daemon 一样给读源数据的命令带 `--selection`），标准与 LeRobot 相同：

```bash
for F in mcap lance; do
  $python -m parity make-fixture --format $F --out $W/mini-$F
  $python -m parity dump-v1 --out $W/rec-$F --v1-src $V1 --fake-vlm -- run --input $W/mini-$F --output $W/rec-$F-out \
    --vlm-endpoint http://fake-vlm.local/v1 --vlm-model fake-vlm
  $python -m parity run-v2 --out $W/v2-$F --input $W/mini-$F --delivery $W/v2-$F-delivery --replay $W/rec-$F/vlm_tape.jsonl.gz
  $python -m parity compare --golden $W/rec-$F --candidate $W/v2-$F --all-strict      # conclusion: PASS
done
```

v1 的 `final.json` 与 LeRobot 版本相同（`passed` `[0, 1, 3, 4, 6]`、`reject` `[2, 5, 7]`），回放 `misses=0 unused=0`；
v2 的交付在 `$W/v2-mcap-delivery/export/mcap_curated/` 与 `$W/v2-lance-delivery/export/lance_episodes/`。
自动化的版本是 `tools/parity/tests/test_containers_parity.py`。

人工裁决的对账（D39，约半分钟）：v1 的 rejudge 在交付上执行两条改标，v2 的裁决序列回放它录下的带子：

```bash
# 7. v1 执行裁决：0、1 两条改标 → [dump-v1] clean: {'records': {'task_success': 2}, … 'passed': 5, 'reject': 3, 'review': 3}
cp -R $W/rec-out $W/rej-delivery && mkdir -p $W/rej-delivery/human-decisions
printf 'episode_id,decision,new_label,note,at\nep000001,采纳建议改标,stack the cups,,2026-09-22 01:00:00\nep000000,采纳建议改标,wipe the table,,2026-09-22 01:00:01\n' \
  > $W/rej-delivery/human-decisions/label_decisions.csv
printf 'checks:\n  task_success:\n    vlm:\n      endpoint: http://fake-vlm.local/v1\n      model: fake-vlm\n' > $W/fake.yaml
$python -m parity dump-v1 --out $W/rej --v1-src $V1 --fake-vlm -- rejudge --delivery $W/rej-delivery --input $W/mini --config $W/fake.yaml

# 8. v2 的裁决序列，同样两条改标（relabel_rerun 缺省 v1），回放第 7 步的带子
#    → [run-v2] done; tape replay: hits=26 misses=0 unused=0；compare 的结论 PASS
cat > $W/decisions.json <<'EOF'
{"schema_version": "1.0", "decisions": [
 {"id": 1, "episode_index": 1, "line": "label", "decision": "custom_label", "new_label": "stack the cups", "note": null, "decided_by": "me", "decided_at": 1790000000001},
 {"id": 2, "episode_index": 0, "line": "label", "decision": "custom_label", "new_label": "wipe the table", "note": null, "decided_by": "me", "decided_at": 1790000000002}]}
EOF
$python -m parity run-v2 --out $W/v2-adj --from $W/v2 --input $W/mini --decisions $W/decisions.json --replay $W/rej/vlm_tape.jsonl.gz
$python -m parity compare --golden $W/rej --candidate $W/v2-adj --all-strict
```

逐项核对：第 7 步的带子有 26 条（16 次打分、8 次逐机位复核、2 次技能画像归类的文本调用），没有仲裁——v1 的 rejudge 只跑两层；
1 按新标注判成功，0 仍弃权、回到待裁决；第 8 步 `task_success 0/2 differ`、`skill_profile 0/5 differ`，终判清单一致（7 照样单列）。
把 `decisions.json` 加上 `"relabel_rerun": "full"` 再跑第 8 步：v2 按首轮完整流程重判，发出 v1 没发过的请求，回放出现 misses，结论 FAIL。

## 在现网 Pod 里生成黄金基线

> 2026-09-21 需求方决定暂缓：v1 在 Pod 上跑过、结果正常，旧系统有备份。真实数据的基线在 F4.2 黄金对账之前补；
> 在那之前，拆 v1 编排的改动靠合成数据集上冻结点 v1 与 v2 的逐位对账守住（设计 11 篇 §3）。

> ⚠️ 现网 Pod 同时在给用户提供服务。基线跑批会和用户任务抢 CPU、内存和方舟配额（v1 的界面本来一次只跑一个任务），
> umi 全量一遍大约要一两个小时。建议挑没人用的时段跑，或者用同一个镜像、同样的环境变量和配置另起一个一次性 Pod 来跑。

基线要两份（D19）：`droid_lerobot` 前 50 条、`umi_640_notask` 全量；
另外跑几遍 v1，量出模型输出的自然波动，作为噪声底。具体为 droid 50 条跑两遍，umi 前 64 条另跑两遍。
下面的 `<…>` 换成实际值。

1. **本机打包**（仓库根目录；包里是冻结点的 v1 源码 + 当前提交的对账工具，解开后统一在 `robot-curation/` 下）

   ```bash
   PYTHONPATH=tools .venv/bin/python -m parity pack --out parity-pod.tar.gz
   ```

2. **放进 Pod 并解开**

   ```bash
   kubectl cp parity-pod.tar.gz <ns>/<pod>:/tmp/parity-pod.tar.gz
   kubectl exec -n <ns> <pod> -- bash -lc 'mkdir -p /tmp/parity && tar -xzf /tmp/parity-pod.tar.gz -C /tmp/parity'
   ```

3. **空跑自检**（合成数据 + 假模型，不花 token，确认 Pod 里的环境能跑通）

   ```bash
   kubectl exec -n <ns> <pod> -- bash -lc 'cd /tmp/parity/robot-curation && export PYTHONPATH=tools &&
     python -m parity make-fixture --out /tmp/parity/mini &&
     python -m parity dump-v1 --out /tmp/parity/smoke --fake-vlm -- run --input /tmp/parity/mini \
       --output /tmp/parity/smoke-out --vlm-endpoint http://fake-vlm.local/v1 --vlm-model fake-vlm'
   ```

4. **正式跑一份基线**（后台、不占终端；不带 TTY，v1 开跑前就不会停下来追问机器人型号）

   ```bash
   kubectl exec -n <ns> <pod> -- bash -lc 'cd /tmp/parity/robot-curation && export PYTHONPATH=tools &&
     mkdir -p /tmp/golden && setsid nohup python -m parity dump-v1 --out /tmp/golden/droid50-a --label droid50-a -- \
       run --input tos://<桶>/<前缀>/droid_lerobot --input-region <地域> \
           --output tos://<交付桶>/<前缀>/golden/v1/droid50-a --output-region <地域> \
           --max-episodes 50 --vlm-model doubao-seed-2-0-pro-260215 \
       > /tmp/golden/droid50-a.log 2>&1 < /dev/null &'
   ```

   现网配置里的模型如果已经是 `doubao-seed-2-0-pro-260215`，`--vlm-model` 可以省掉；它和对账口径里的固定模型必须一致。
   Pod 里的 `CURATION_CONFIG` 会让 v1 自动用现网的站点配置；实际生效的配置（v1 自己脱敏过的）和它的哈希记在 `dump.json`。
   umi 同样的命令，去掉 `--max-episodes`；umi 不在运动学规格库里，这一项会整项跳过，属正常。

5. **看状态**：`python -m parity tape-summary /tmp/golden/droid50-a/vlm_tape.jsonl.gz`，
   以及 `dump.json` 里的 `status` / `problems`。状态是 `dirty` 时，不整批重跑，改用「回放 + 补录」：

   ```bash
   python -m parity dump-v1 --out /tmp/golden/droid50-a2 --label droid50-a \
     --replay /tmp/golden/droid50-a/vlm_tape.jsonl.gz --record-missing \
     [--drop-hashes /tmp/golden/droid50-a/suspect_hashes.txt] -- run …（同第 4 步，--output 换一个新目录）
   ```

   录过的请求直接回放，失败的和因此新出现的请求才真调模型。`suspect_hashes.txt` 列的是打分回答解析不了的请求，带上它就会重新问一遍。

6. **回放自检**（不花 token）：用第 4 或第 5 步的录制带回放一遍，然后
   `python -m parity compare --golden /tmp/golden/droid50-a --candidate /tmp/golden/droid50-a-replay --all-strict`，
   要求最后一行为 `conclusion: PASS`。

7. **噪声底**：同样的命令再真跑一遍（`droid50-b`），然后
   `python -m parity compare --golden /tmp/golden/droid50-a --candidate /tmp/golden/droid50-b`：
   确定性六项必须全 PASS（F1.1 验收①）；VLM 三项的差异率就是 v1 自身的噪声底，记进进度文件。

8. **存档**：`python -m parity archive --dump /tmp/golden/droid50-a --to tos://<交付桶>/<前缀>/golden/v1/droid50-a/dump --manifest-out /tmp/golden/droid50-a.manifest.json`，
   把 `*.manifest.json` 拷回本机，放进仓库的 `tools/parity/golden/`，随代码提交。

## 导出目录里有什么

| 文件 | 内容 |
|---|---|
| `dump.json` | 状态（clean / dirty / refused）与问题清单、v1 源码核对结果、命令行、生效配置及其哈希、各库版本、耗时、录制统计 |
| `records/<module>.jsonl` | 六项漏斗检查的规范化记录，按 episode 下标排序 |
| `verdicts.jsonl` | 漏斗判决：keep / drop、原因、硬门失败项、软分、弃权项 |
| `autolabel.jsonl` | 无标注条目补打的任务描述 |
| `dedup.json` | 去重遍历顺序、动作哈希撞车组、内容指纹、剔除的重复对 |
| `skill_profile.json` | 每条的归族、标注分歧复核队列（重打标按字面排序，并发下顺序不定）、原始画像 |
| `final.json` | 终判清单（v2 口径：passed = 漏斗 keep − 重复项）以及 v1 三个文件各自的名单 |
| `vlm_tape.jsonl.gz` | 录制带（录制与补录模式） |
| `replay_misses.jsonl` | 回放时没在录制带上找到的请求（回放模式） |
| `suspect_hashes.txt` | 打分回答解析不了的请求（有才写） |

## 已知限制

- **字节级重复的两条 episode** 发出的请求完全相同，回放时它们的响应可能对调。真模型对这两条给了不同回答时，
  会表现为两条互换的差异，对账时对照 `dedup.json` 人工确认。
- 除打分外，其它回答解析不了（比如仲裁返回的 JSON 坏了）时，导出只能认出「这条降级了」，定位不到是哪次请求，只能整份重跑。
- `compare` 读 v2 运行目录时，取 `revisions/` 下最新的已提交版本（有 `commit.json` 的），没有就取编号最大的；`run-v2` 产生 r0001，
  带 `--from` 的裁决序列产生下一个版本。
- 裁决的对账目前只覆盖改标（唯一要调模型的一条线）；成败裁决与复议不调模型，它们对清单的作用由 `backend/tests/cli/test_aggregate.py` 按 v1 的规则钉住。
