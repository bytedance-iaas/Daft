# EEF–视频一致性 · 离线评估器

设计 12 篇 §13 的离线评估：唯一读真值的代码（D-E5）。检测器与观测 provider 在 `backend/curation/extensions/eef_consistency/`，
它们只读 `trajectory.json`、视频与种子；这里的脚本才读各数据集的 `evaluation/`、`corruptions.json`、`ground_truth/`。

DEMO 数据在仓库外（`$CURATOR_EEF_DEMO_DATA`，默认 `~/ws/ws_general/galbot`）：

| 路径 | 内容 | 谁读 |
|---|---|---|
| `<ds>/trajectory.json` | 上传件（`eef-video/1.0.0` 单文件包） | 模块 |
| `<ds>/observations_seed/` | P-A 种子，每 15 帧一个锚点，`synthetic_fixture`（dataset2 由 `galbot/tools/make_seed_observations.py`、dataset1 由 `make_seed_observations_dataset1.py` 生成） | 模块的 provider |
| `<ds>/evaluation/truth_pixels/` | 同样构造、逐帧的真实像素（上面两个脚本加 `--every 1 --out <ds>/evaluation/truth_pixels`） | 只有本评估器 |
| dataset2 `corruptions.json`、dataset1 `ground_truth/` | 注入的故障、参数与评估遮罩 | 只有本评估器 |

## 脚本

在仓库根执行，`PYTHONPATH=backend:tools`：

| 命令 | 产物 |
|---|---|
| `.venv/bin/python -m eef_eval.observation_report` | `reports/observations.json`：F5.2 的覆盖率、弃权率、对真值的定位误差、种子图像哈希一致性、耗时，以及投影平移 30 px 的独立性实验 |
| `.venv/bin/python -m eef_eval.matrix` | `reports/matrix.json`（逐格期望 / 实际 / 是否通过）、`reports/matrix_details.jsonl`（18 条的 detail）；`reports/run/` 是观测、曲线、证据图（不进仓库）。检测器先跑完，评估器才读真值 |

## 结果（2026-09-23）

- 观测（`reports/observations.json`）：两套数据 25 路（episode × 相机），可见比例中位数 0.66（最低 0.60），
  对真值 P95 误差中位数 3.2 px（最大 4.2 px），种子图像哈希全部一致；dataset1、dataset2 各一路把投影平移 30 px，观测逐位不变。
  失跟多在倒糖阶段：合成种子按几何投影标成「可见」，但法兰 / 指尖其实被第 7 关节外壳挡住，跟踪器拒绝给出观测。
- 受控异常矩阵（`reports/matrix.json`，`demo` profile 0.2）：18 条全过、114 格全对，另 4 组轻重档（漂移、朝向、画面抖动、
  记录抖动）重档均大于轻档；每行都检查「不出现不该出的诊断」。拟合值：dataset2 ep6 外参 29.75 mm / 2.01°（注入 3 cm / 2°），
  dataset1 ep3/ep4 恒定旋转 15.2° / 40.1° 绕 −y（注入 15° / 40° 绕 y），dataset2 ep2 29.9° 绕 −z（注入 30° 绕接近轴），
  lag dataset1 ep9/ep10 −0.201 / −0.535 s、dataset2 ep5 +0.330 s。18 条共约 2 分钟（每分钟视频每路约 13 秒 CPU）。
  这是同源合成变体上的功能回归，不是泛化准确率。
