# 回归样本集的工具：合成注入与打分

设计见 `docs/design/16-regression-samples.md`（注入 §7.5，期望 §8，打分 §8.4）。样本集本身不在仓库里（在 `tos://curation-robo-anchor/`），
这里放可复现的工具：

| 文件 | 做什么 |
|---|---|
| `inject.py` | 在干净条目上注入故障，生成新的 LeRobot v2.x 数据集和 `injection.json` |
| `score.py` | 拿平台的运行目录对样本集的 `expectation.json` 打分：每个检测项的 TP / FP / FN / TN、precision、recall，可与基线比较 |
| `finding_map.json` | 对照表：平台每个模块的哪种结果算报出了哪个检测项 |
| `taxonomy.json` | 检测项分类 1.0（69 项），与样本集里的同名文件一致 |

## `score.py`

平台是按「模块 + 原因码」出结果的（`checks/<模块>/results.jsonl`），样本集的期望是按检测项写的。`finding_map.json` 把前者换成后者，
`score.py` 再逐条逐项比：

- 期望 present 且平台报了 = TP，没报 = FN；期望 absent（在 `clean` 里）而平台报了 = FP，没报 = TN；没有期望的项不算。
- 相机限定的期望只和同一路相机的结果比（`wrist` 与 `observation.images.wrist`、`robot0` 与 `robot0_sensor_camera0_compressed` 算同一路）。
- 这条 episode 上能报这一项的模块都没跑（漏斗短路、没选、预检不支持）记 `not_assessed`，都执行出错记 `error`，两者都不进 precision / recall。
- 对照表里没有任何规则能报的项是平台的 gap，单列。control 类检测项看平台有没有误报；SET-4 看预检能不能读进来。

```bash
# 运行目录名 -> 子集的映射就是基线目录里的 runs.json；也可以用 --run 子集=目录 逐个给
PYTHONPATH=tools .venv/bin/python -m regression_samples.score \
    --expectation <样本集>/anchor/v1/expectation.json \
    --runs-root <运行目录的上一级> --runs-map <运行目录的上一级>/runs.json \
    --out score.json --markdown score.md
```

CI 里与基线比较：`--baseline <上一次的 score.json> --max-drop 0.05 --min-support 5`，任何一项的 precision、recall、control 通过率
比基线掉超过 0.05（且这一项至少有 5 条计数）就以退出码 3 结束；`--require-all-runs` 让缺运行目录的子集以退出码 2 结束。
平台 `1b30fb224` 上不用模型的 6 个模块的第一份打分在 `tos://curation-robo-anchor/baseline/1b30fb224/score/`。

对照表要跟着平台走：平台改了问题码、细节字段名或模块 id，`tests/test_score.py` 的一致性检查会失败，同步改 `finding_map.json`。

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
  复制类故障（`duplicate`）的条目继承被复制条目的故障。
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
| `label_swap` | LABEL-5 | 换成另一条的任务描述（需要多任务的基底） |

## 手动验证步骤

打分：

1. `PYTHONPATH=tools .venv/bin/python -m pytest -q tools/regression_samples/tests`，全部通过。
2. 从 `tos://curation-robo-anchor/` 取 `anchor/v1/expectation.json` 与 `baseline/1b30fb224/`（含 `runs.json`），按上面的命令打分：
   终端不打印东西、退出码 0，`score.md` 的第一行是 `# Score: anchor v1`，写明 911 条全部打分、68 个子集都有运行目录。
3. 再加 `--baseline baseline/1b30fb224/score/anchor.json` 跑一遍：退出码 0，`score.md` 末尾写「Against the baseline: no regression」。

注入：

1. 在有 `.venv` 的仓库根目录，用 `backend/tests/cli/integrity_samples.py` 之类的小夹具或任意 LeRobot v2.x 数据集当基底，跑上面的命令；
   终端每行打印一条 `ep N <- base M: <故障> <档位> <检测项> <参数>`，最后 `INJECT_DONE`。
2. 打开输出目录：`meta/info.json` 的 `total_episodes` 等于输出条数，`meta/episodes.jsonl` 每条一行，`injection.json` 的条目数相同。
3. 用 PyAV 逐条解码输出视频：除 `garble`、`truncate_video`、`zero_fill`、`empty_video` 外，帧数都等于该条 parquet 的行数；
   `resolution` 那一路的宽高与 `injection.json` 里的 `actual` 一致。
4. 把输出目录登记到质检台跑一遍数据完整性：`truncate_video`、`zero_fill`、`empty_video`（空文件）、`garble`、`nan_action`、`timestamps` 应被拒；
   `duplicate` 记为可疑。
