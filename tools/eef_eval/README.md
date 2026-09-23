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

## 结果（2026-09-23）

- 观测（`reports/observations.json`）：两套数据 25 路（episode × 相机），可见比例中位数 0.66（最低 0.60），
  对真值 P95 误差中位数 3.2 px（最大 4.2 px），种子图像哈希全部一致；dataset1、dataset2 各一路把投影平移 30 px，观测逐位不变。
  失跟多在倒糖阶段：合成种子按几何投影标成「可见」，但法兰 / 指尖其实被第 7 关节外壳挡住，跟踪器拒绝给出观测。
