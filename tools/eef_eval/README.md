# EEF–视频一致性 · 离线评估器

设计 12 篇 §13 的离线评估：唯一读真值的代码（D-E5）。检测器与观测 provider 在 `backend/curation/extensions/eef_consistency/`，
它们只读 `trajectory.json`、视频与种子；这里的脚本才读各数据集的 `evaluation/`、`corruptions.json`、`ground_truth/`。

DEMO 数据在仓库外（`$CURATOR_EEF_DEMO_DATA`，默认 `~/ws/ws_general/galbot`）：

| 路径 | 内容 | 谁读 |
|---|---|---|
| `<ds>/trajectory.json` | 上传件（`eef-video/1.0.0` 单文件包） | 模块 |
| `<ds>/observations_seed/` | P-A 种子，每 15 帧一个锚点，`synthetic_fixture`（dataset2 由 `galbot/tools/make_seed_observations.py`、dataset1 由 `make_seed_observations_dataset1.py` 生成） | 模块的 provider |
| `<ds>/evaluation/truth_pixels/` | 同样构造、逐帧的真实像素（上面两个脚本加 `--every 1 --out <ds>/evaluation/truth_pixels`） | 只有本评估器 |
| dataset2 / dataset3 `corruptions.json`、dataset1 `ground_truth/` | 注入的故障、参数与评估遮罩 | 只有本评估器 |
| `dataset3/` | F5.7 的留出集：`galbot/tools/build_dataset.py --root dataset3 --seed 20260923 --recipes-json dataset3/sweep_recipes.json`（dataset2 同一条原始 episode，换种子、六类故障按幅度扫描共 23 条 + 原版 1 条），再跑导出、种子与逐帧真值三个脚本 | 模块 / 本评估器 |

## 脚本

在仓库根执行，`PYTHONPATH=backend:tools`：

| 命令 | 产物 |
|---|---|
| `.venv/bin/python -m eef_eval.observation_report` | `reports/observations.json`：F5.2 的覆盖率、弃权率、对真值的定位误差、种子图像哈希一致性、耗时，以及投影平移 30 px 的独立性实验 |
| `.venv/bin/python -m eef_eval.acceptance [--dataset dataset3] [--jobs 4]` | `reports/acceptance.json`、`reports/acceptance.md`：F5.7 留出集验收——每条一个子进程跑冻结的 `demo` profile 和复核窗口（替身模型计请求数），之后才读真值；按故障 × 幅度给检出、误报（`ALLOWED` 之外的可疑分项）、弃权、定位误差、拟合值 vs 注入值，以及每分钟视频的 CPU / 墙钟、峰值内存、VLM 请求数 |
| `.venv/bin/python -m eef_eval.review_eval --dataset dataset2 --endpoint URL --model M --api-key-env ARK_API_KEY` | F5.10：先跑 CPU，再按新请求包（一点一轴）问模型，调用全部录进 `reports/review_eval_<数据集>.tape.jsonl.gz`；问完才读真值，逐窗口判定红圈是否在真实的 P 上、绿十字是否跟对、箭头方向是否对（距离 ≤ 5 px / ≥ 12 px、角度 ≤ 5° / ≥ 15°，之间不计），按问题与故障类型报模型给出明确结论的比例和与真值的一致率（`reports/review_eval_<数据集>.json`、`.md`）。`--replay TAPE` 离线复算，`--stand-in` 用假模型只查流程 |
| `.venv/bin/python -m eef_eval.matrix` | `reports/matrix.json`（逐格期望 / 实际 / 是否通过）、`reports/matrix_details.jsonl`（18 条的 detail）；`reports/run/` 是观测、曲线、证据图（不进仓库）。检测器先跑完，评估器才读真值 |

## 结果（2026-09-23）

- 观测（`reports/observations.json`）：两套数据 25 路（episode × 相机），可见比例中位数 0.66（最低 0.60），
  对真值 P95 误差中位数 3.2 px（最大 4.2 px），种子图像哈希全部一致；dataset1、dataset2 各一路把投影平移 30 px，观测逐位不变。
  失跟多在倒糖阶段：合成种子按几何投影标成「可见」，但法兰 / 指尖其实被第 7 关节外壳挡住，跟踪器拒绝给出观测。
- 受控异常矩阵（`reports/matrix.json`，`demo` profile 0.2）：18 条全过、114 格全对，另 4 组轻重档（漂移、朝向、画面抖动、
  记录抖动）重档均大于轻档；每行都检查「不出现不该出的诊断」。拟合值：dataset2 ep6 外参 29.75 mm / 2.01°（注入 3 cm / 2°），
  dataset1 ep3/ep4 恒定旋转 15.2° / 40.1° 绕 −y（注入 15° / 40° 绕 y），dataset2 ep2 29.9° 绕 −z（注入 30° 绕接近轴），
  lag dataset1 ep9/ep10 −0.201 / −0.535 s、dataset2 ep5 +0.330 s。18 条共约 2 分钟（单进程墙钟，每分钟视频每路约 13–15 秒；按 CPU 时间算见下面的留出集验收）。
  这是同源合成变体上的功能回归，不是泛化准确率。
- 留出集验收（`reports/acceptance.md`，F5.7，`demo` 0.2 定数后没动过）：24 条、有故障 23 条、检出 19 条，**任何一条都没有误报**
  （原版也没有），弃权率中位数 0，独立观测对真值 P95 中位数 2.75 px。漏检的 4 条都在阈值以下：时间偏移 1、2 帧（测得 0.064 /
  0.130 s，与注入一致，但门槛是 ≥ 2 帧且残差改善够大）、记录抖动 σ = 1 mm（门槛 2.5 mm 滚动 RMS）、画面抖动峰值 1 px（门槛 2 px）；
  检出下限：外参 5 mm / 0.5°、恒定朝向 5°、漂移峰值 5 mm、时间偏移 3 帧、记录抖动 3 mm、画面抖动 3 px。拟合值：朝向 5.00 / 10.3 /
  20.5 / 39.9°（注入 5 / 10 / 20 / 40°），外参 5.18 mm / 0.53°、9.92 / 1.02、29.75 / 2.01（注入 5 / 0.5、10 / 1、30 / 2），
  时间偏移 0.197 / 0.397 / −0.270 s（注入 0.2 / 0.4 / −0.267）。成本：每分钟视频每路相机 CPU 约 33 秒（含 OpenCV 线程）、
  墙钟约 18 秒（4 路并行），单条峰值内存约 0.5 GB，复核每条 6–12 个请求（每个 13 张图）。**限制**：同一场景的合成变体，
  不代表其他机器人、相机或真实故障上的误报 / 漏报；阈值仍是 `calibrated: false`。

