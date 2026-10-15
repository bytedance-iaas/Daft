# 回归样本集的工具：合成注入、打分与登记

设计见 `docs/design/16-regression-samples.md`（注入 §7.5，期望 §8，打分 §8.4）。样本集本身不在仓库里（在 `tos://curation-robo-anchor/`），
这里放可复现的工具：

| 文件 | 做什么 |
|---|---|
| `inject.py` | 在干净条目上注入故障，生成新的 LeRobot v2.x 数据集和 `injection.json` |
| `inject_mcap.py` | mcap 的结构故障（FILE-3）与合法写法：截断、数据块 CRC、摘要区 CRC；无摘要区、CRC 全 0 |
| `inject_v3.py` | LeRobot v3 的索引与引用故障（FILE-9）：行区间、视频时间段、任务编号、帧号 |
| `score.py` | 拿平台的运行目录对样本集的 `expectation.json` 打分：每个检测项的 TP / FP / FN / TN、precision、recall，可与基线比较；直接读记录里的发现（C2 2.0），更早的运行目录不收 |
| `finding_map.json` | 记录里读不到的两样：对照项（controls，平台不应报出的合理情形）与预检项（ingestion，平台应当读得进的格式） |
| `taxonomy.json` | 检测项分类 2.0（67 项，2026-10-14 与样本集 v2 一起定稿，此后已有编号的定义不再改、新项接在本维度最后、删掉的编号不复用；TASK-10 的子类、LABEL-4 的关系在 `subtypes`、`attributes`），条目同平台契约 C6（`docs/contracts/taxonomy.json`）；样本集 v2 的同名文件就是这一版，v1 的还是 1.3，打分时按 `renumbered` 改号；平台侧的注记由下一行生成 |
| `coverage_from_registry.py` | 由模块注册表（`docs/contracts/modules.json`）生成 `taxonomy.json` 的 `platform_status`、`platform_codes`、`platform_conditions`（设计 17 §6.2） |
| `register.py` | 把样本集的子集登记成质检台的数据集（REST，`POST /datasets`）：TOS 地址加访问密钥名，mcap 的映射按探测起草的原样确认；重跑不重复登记 |
| `console_picks.json` | 登记到质检台的 20 个子集（设计 16 §7.9）：地址、名字（`anchor-v1/<子集>`）、备注（格式、机器人、正例、期望值在哪） |
| `console_picks_v2.json` | 样本集 v2 的全部 99 个子集（anchor 63、anchor-nc 25、crafted 11，设计 16 §7.9）：名字 `<样本集>-v2/<子集>`，备注由 v2 的期望值生成（正例、对照、有反例的项数、期望值在哪、许可） |

## `coverage_from_registry.py`

`platform_status` 一列不再手写，跟着注册表走：默认策略下判废或转人工的项「能判」，只报告或只有读数的「有读数」，覆盖它的模块都有前提
（本体在规格库、有状态量、上传 trajectory.json）的「部分」并写明前提，没有模块覆盖的「没有」，对照项「能处理」；预检判的 SET-2 记 `preflight`。
`platform` 一列（样本实测看到的）照旧手写。注册表改了细码或覆盖，`tests/test_coverage.py` 会失败，重新生成：

```bash
PYTHONPATH=tools .venv/bin/python -m regression_samples.coverage_from_registry           # 改写 taxonomy.json
PYTHONPATH=tools .venv/bin/python -m regression_samples.coverage_from_registry --check   # 不一致时退出码 1
```

## `score.py`

score 2.0（设计 17 §6.1）读平台的运行目录（`checks/<模块>/`、`revisions/rNNNN/`、`preflight.json`）：

- 记录自己写着检测项：`findings[].item` 与 `scope`（相机）、模块评估过的 `assessed`、评估不了的 `unassessable`、执行出错的 `status: error`。
  一项由哪些模块评估取注册表（`docs/contracts/modules.json` 的 `covers`，`--registry` 可换）。
- 期望 present 且平台报了 = TP，没报 = FN；期望 absent（在 `clean` 里）而平台报了 = FP，没报 = TN；没有期望的项不算。
- 相机限定的期望只和同一路相机的发现比（`wrist` 与 `observation.images.wrist`、`robot0` 与 `robot0_sensor_camera0_compressed` 算同一路）。
- 这条 episode 上没有模块评估过这一项（没跑、预检不支持、或写明评估不了）记 `not_assessed`，评估它的模块都出错记 `error`，两者都不进
  precision / recall；`recall_end_to_end` 把它们和没有模块覆盖的都留在分母里。注册表里没有模块覆盖的项是平台的 gap，单列。
- 几个模块报同一项：总指标取并集，另给每个模块自己的 precision / recall（`by_module`，看该调谁）。每项还数有区间（`frames` / `time_s`）
  的发现占比（`intervals`，P19）。
- 数据集级的项（分类表 level 为 dataset，或条目标了 `unit: subset`）按子集只计一次。数据集级发现取结果版本 `report.json` 各小节的
  `dataset_findings`，只用于这类项；逐条的项只看逐条记录（完整性模块的表检查在报告里另给一条汇总的 `table_overlap`，不铺到子集的每一条）。
- episode 级取最终清单：一条只在已检查项上干净的 episode 被拒，拒它的模块能报其中一项才算误报，否则记 `dropped_outside_checked`。
- 对照项看平台有没有报出它守着的检测项（不该报）；SET-2 看预检能不能读进来。
- EEF–视频一致性只给意见、不判废：它的发现带不一致置信度 p，p 达到 `--eef-min-p`（缺省 0.7，demo 档位的「不一致」）才算报了；
  冲突（`conflict`）的 p 按定义不低于高档下限，缺省阈值下总算报了（设计 25 §7）。
- C2 2.0 以前（D59）的运行目录直接报错：重跑那个子集再打分。

```bash
# 运行目录名 -> 子集的映射写在 runs.json 里；也可以用 --run 子集=目录 逐个给
PYTHONPATH=tools .venv/bin/python -m regression_samples.score \
    --expectation <样本集>/anchor/v1/expectation.json \
    --runs-root <运行目录的上一级> --runs-map <运行目录的上一级>/runs.json \
    --out score.json --markdown score.md
```

与基线比较：`--baseline <上一次的 score.json> --max-drop 0.05 --min-support 5`，任何一项的 precision、recall、对照项通过率
比基线掉超过 0.05（且这一项至少有 5 条计数）就以退出码 3 结束；`--require-all-runs` 让缺运行目录的子集以退出码 2 结束。
平台 `1b30fb224` 那份基线是旧格式（C2 1.0）的，已作废（设计 27 §3）；2.0 的基线要在集群上对样本集重跑一次再录。

## `inject.py`

取一个 LeRobot v2.x 数据集里量过没问题的条目，每条注一种故障，写成一个新的 LeRobot v2.x 数据集。故障落在字节、表格或画面里，
和真实缺陷同一位置，平台像读别的数据集一样读它。每条只注一种故障，两档强度（`obvious` / `borderline`），另加几条原样对照。

```bash
.venv/bin/python tools/regression_samples/inject.py \
    --base <lerobot v2.x 数据集目录> --out <新数据集目录> \
    --episodes 6,8,11,12 --plan droid --controls 3 --seed 16
```

- `--plan`：`droid`（22 种画面 / 文件 / 表格故障）、`droid_tablegap`（只有「表里少了行」，单独成集，见设计 16 §11 第 11 条）、
  `fastumi`（动作空间故障与标注互换），或用逗号列出故障名。
- `--episodes`：基底条目，应当是量过没问题的；每条只用一次（够多的话）。
- 输出目录里的 `injection.json` 是真值：每条的故障、档位、范围（哪路相机 / 哪个通道、帧区间）、参数、基底条目与 lineage。
  复制类故障（`duplicate`）的条目继承被复制条目的故障，`base_episode` 与 `lineage` 也记被复制条目的根；抽到但没用上的基底记在
  `slot_base_episode`。
- 需要 PyAV（带 libsvtav1 / libx264 / mpeg4 编码器）与 Pillow；`garble` 故障要能 import 平台包里的 `curation.extensions.integrity.mp4`
  （`PYTHONPATH=backend`），否则跳过。

| 故障 | 检测项 | 明显 / 临界 |
|---|---|---|
| `frozen` | IMG-1 | 一路相机停帧 3 s / 0.8 s |
| `dark`、`overexposed` | IMG-2 | 亮度 ×0.12 整条 / ×0.45 一段；增益 3.2 / 1.6 |
| `blur` | IMG-4 | 高斯半径 8 / 2.5 px |
| `occlusion` | IMG-8 | 盖住 70% / 25% 画面 |
| `shake` | IMG-6 | 每帧随机平移 ±14 / ±4 px |
| `smudge` | IMG-7 | 模糊污渍，半径 35% / 15% 画面宽 |
| `blocks` | IMG-5 | 错位色块与撕裂带，12 / 2 帧 |
| `garble` | FILE-4 | 压缩帧被随机字节覆盖，6 / 1 帧（解码失败） |
| `shift_video` | AV-1 | 一路视频提前 0.6 / 0.2 s |
| `camera_swap` | MV-2 | 腕 ↔ 外 / 外 ↔ 外 两路视频文件对调 |
| `resolution` | FILE-7 | 一路重编码成一半 / 减 16 px，声明不变 |
| `truncate_video`、`zero_fill`、`empty_video` | FILE-2、FILE-1 | 截到 60% / 97%；开头 4 KB / 中间 512 B 置零；删除 / 清空文件 |
| `nan_action` | FILE-6 | 5 / 1 行 NaN |
| `drop_rows` | STRM-3、FILE-5 | 表里删 1 s / 2 帧的行，视频不动 |
| `timestamps` | STRM-4 | 倒退 0.5 s 三行 / 重复一行 |
| `spike`、`sawtooth`、`constant_channel`、`stale_state` | ACT-2、ACT-1、ACT-3、ACT-8 | 状态量单点跳变 12 / 4 倍 p95 步长；锯齿 20% / 5% 量程；夹爪通道全程 / 后半段恒定；状态量沿用旧值 |
| `duplicate` | SET-1 | 字节级副本 / 重编码副本 |
| `label_swap` | LABEL-4 | 换成另一条的任务描述（需要多任务的基底） |
| `drift`、`clock_reset` | AV-5 | 画面越来越落后，到最后一帧落后 0.6 / 0.2 s；中段起整体落后 0.6 / 0.2 s（`--plan droid_sync`） |
| `--dataset-fault meta_fps`、`meta_totals` | FILE-8 | `meta/info.json` 的 fps 写成两倍；总条数、总帧数多写（整个子集一处，`--plan none` 时只生成原样对照） |

```bash
# mcap 结构故障：每个基底用一次，前几个按 --plan 注入，余下的做对照
.venv/bin/python tools/regression_samples/inject_mcap.py --out <新目录> --bases a.mcap,b.mcap,... --controls 2
# LeRobot v3 索引故障：故障:episode[:参数]，其余条目原样留作对照（样本集里的 svla_so101_index_injected 用的是下面这组，基底是精简后的 10 条）
.venv/bin/python tools/regression_samples/inject_v3.py --base <v3 数据集> --out <新目录> --plan offset:0:6,offset:1:1,video_range:2:1.0,dangling_task:25,frame_index:38
```

## `register.py`

质检台的接口要登录（HTTP Basic，设计 08 §5）：账号密码从 `CURATOR_USER` / `CURATOR_PASSWORD` 读，没设就在终端问，只发给 Daemon，不打印、不落盘。
`--kube 命名空间/pod` 由脚本自己开 `kubectl port-forward`、用完关掉；能直接访问 Daemon 时用 `--api`。不给 `--credential` 时用 Daemon 的缺省访问密钥
（或唯一的一把）；登记前先用它列一次第一个子集的上级目录，钥匙或桶不对就停在这里，一个都不登记。

```bash
# 看 Daemon 里有哪些访问密钥（只有名字、区域、末 4 位）
.venv/bin/python tools/regression_samples/register.py --kube dataverse/dataverse-curation-0 --list-credentials
# 登记 console_picks.json 里的 20 个子集，每个一行结果；--out 另存一份 JSON Lines
.venv/bin/python tools/regression_samples/register.py --kube dataverse/dataverse-curation-0 \
    --picks tools/regression_samples/console_picks.json [--credential <访问密钥名>] [--out registered.jsonl]
# 样本集 v2 的全部 99 个子集
.venv/bin/python tools/regression_samples/register.py --kube dataverse/dataverse-curation-0 \
    --picks tools/regression_samples/console_picks_v2.json --out registered_v2.jsonl
```

每行是 `[i/N] 新登记|已登记 <ds-id> <名字> | <格式> <条数> 条 | 预检通过|预检有问题：… | 映射 …`；同一地址再登记返回原来那条（名字、备注变了就改过来）。
退出码：0 全部登记；1 有失败的（其余照常登记）；2 登录被拒、钥匙不对、输入有误或连不上（中途断开时前面的已登记，重跑即可）。

## 手动验证步骤

打分：

1. `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests`，全部通过。
2. 拿对账工具的合成数据集当一个子集：按 [对账工具 README](../parity/README.md) 手动验证的第 1、2 步录一份基线（`$W/golden`），
   再写一份小期望值打分（不需要任何密钥）：

   ```bash
   cat > $W/expectation.json <<'EOF'
   {"set": "mini", "set_version": "v1", "episodes": [
    {"dataset": "mini", "episode_index": 2, "verdict": "defective", "problems": [{"item": "STRM-3"}], "clean": [{"item": "FILE-4"}]},
    {"dataset": "mini", "episode_index": 5, "verdict": "defective", "problems": [{"item": "STRM-5"}], "clean": [{"item": "FILE-4"}]},
    {"dataset": "mini", "episode_index": 3, "problems": [{"item": "SET-1"}], "clean": [{"item": "STRM-3"}]},
    {"dataset": "mini", "episode_index": 7, "problems": [{"item": "SET-1"}], "clean": [{"item": "STRM-3"}]},
    {"dataset": "mini", "episode_index": 0, "verdict": "clean_on_checked_items", "clean": [{"item": "STRM-3"}, {"item": "SET-1"}]}]}
   EOF
   PYTHONPATH=tools .venv/bin/python -m regression_samples.score --expectation $W/expectation.json --run mini=$W/golden \
       --out $W/s.json --markdown $W/s.md
   ```

   退出码 0。`s.md` 第一行是 `# Score: mini v1 (taxonomy 2.0, map 2.1)`，episode 级 TP 2、TN 1；STRM-3、STRM-5 各 TP 1，
   SET-1 按子集计、TP 1 TN 1；FILE-4 的两条记「not assessed」（默认链没有数据完整性模块）。
3. 再加 `--baseline $W/s.json --min-support 1` 跑一遍：退出码 0，末尾写「Against the baseline: no regression」。

注入：

1. `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests/test_inject.py`：mcap 的合法写法读得回全部消息、坏的那几条
   在规范规定的位置被识别；v3 的每种故障只改了一条 episode 的一处引用，基底不变。
2. 在有 `.venv` 的仓库根目录，用 `backend/tests/cli/integrity_samples.py` 之类的小夹具或任意 LeRobot v2.x 数据集当基底，跑上面的命令；
   终端每行打印一条 `ep N <- base M: <故障> <档位> <检测项> <参数>`，最后 `INJECT_DONE`。
3. 打开输出目录：`meta/info.json` 的 `total_episodes` 等于输出条数，`meta/episodes.jsonl` 每条一行，`injection.json` 的条目数相同。
4. 用 PyAV 逐条解码输出视频：除 `garble`、`truncate_video`、`zero_fill`、`empty_video` 外，帧数都等于该条 parquet 的行数；
   `resolution` 那一路的宽高与 `injection.json` 里的 `actual` 一致。
5. 把输出目录登记到质检台跑一遍数据完整性：`truncate_video`、`zero_fill`、`empty_video`（空文件）、`garble`、`nan_action`、`timestamps` 应被拒。
   `duplicate` 不归完整性（`duplicate_content` 已删，D87）：同一个任务再勾上重复检测（`duplicate_check`），明显档（字节级副本）与被复制的那条
   各得一条 SET-1 发现、互相写明，只报告、都不拒；临界档（重编码副本）画面字节不同，查不出（`taxonomy.json` 里 SET-1 的平台注记）。

登记：

1. `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests/test_register.py`：对着桩 Daemon，登录头、写请求的
   `Content-Type` 与 `Idempotency-Key`、请求体、缺省钥匙、mcap 映射确认、重跑只改备注、登录被拒时一个不登记都对。
2. 本机起一个不鉴权的 Daemon（`.claude/launch.json` 的 `curator-daemon-local`，本地根目录放一份 LeRobot、一份 GenRobot mcap、一份 ABC-130k mcap），
   `register.py --api http://127.0.0.1:<端口>/curation/api/v1 --no-auth <三个目录>`：三行都是「新登记」，LeRobot「预检通过」，GenRobot「映射 已确认（UMI 手持夹爪（内置），第 1 版）」，
   ABC-130k「预检有问题：mcap 里找不到必需的动作 topic（/action）…」且映射已确认（只能可视化，不能质检）；再跑一遍三行都是「已登记」。
3. 集群：`--kube dataverse/dataverse-curation-0 --list-credentials` 列出钥匙；带 `--picks console_picks.json` 跑完，控制台「数据集」里搜 `anchor` 能看到 20 条，
   名字、备注、条数与 `console_picks.json` 一致。带 `--picks console_picks_v2.json` 跑完，搜 `-v2/` 能看到 99 条（`anchor-v2/`、`anchor-nc-v2/`、
   `crafted-v2/` 各 63、25、11），条数与 v2 的 `index.json` 一致。
