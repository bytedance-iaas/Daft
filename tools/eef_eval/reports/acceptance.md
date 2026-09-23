# EEF–视频一致性 · 留出集验收（F5.7）

profile `demo 0.2`（未校准，定数后没动过），数据集 `dataset3`：换了随机种子、按幅度扫描的留出变体，没参与定阈值；但和 dataset2 出自同一条原始 DROID episode，是同一场景上的检出下限扫描，不是跨场景的泛化验收。

- 24 条（有故障 23 条，检出 19 条）；出现误报的条数 0（基准条上 0）；弃权率中位数 0.0；独立观测对真值 P95 误差中位数 2.75 px
- 成本：每分钟视频每路相机 CPU 32.9 秒（用户 + 系统态、含 OpenCV 线程，最多 33.3；墙钟 18.4 秒，4 路并行），峰值内存最多 494.7 MB，VLM 复核每条请求数中位数 8.0（最多 12.0）

| 故障 | 条数 | 检出 | 有误报 | 检出的最小幅度 | 漏检的最大幅度 | 逐幅度（✓ 检出 / ✗ 漏检，括号里是误报） |
|---|---|---|---|---|---|---|
| baseline | 1 | 0 | 0 |  |  |  — |
| calib_offset | 3 | 3 | 0 | 0.005 m |  | 0.005m ✓；0.01m ✓；0.03m ✓ |
| gripper_orient | 4 | 4 | 0 | 5.0 deg |  | 5.0deg ✓；10.0deg ✓；20.0deg ✓；40.0deg ✓ |
| sync_offset | 5 | 3 | 0 | 3 frames | 2 frames | 1frames ✗；2frames ✗；3frames ✓；-4frames ✓；6frames ✓ |
| traj_drift | 4 | 4 | 0 | 0.005 m |  | 0.005m ✓；0.01m ✓；0.02m ✓；0.04m ✓ |
| traj_jitter | 3 | 2 | 0 | 0.003 m | 0.001 m | 0.001m ✗；0.003m ✓；0.008m ✓ |
| video_shake | 4 | 3 | 0 | 3.0 px | 1.0 px | 1.0px ✗；3.0px ✓；6.0px ✓；12.0px ✓ |

拟合值与注入值（被支持的诊断）：

- ep5 gripper_orient 5.0deg：{"injected_deg": 5.0, "fitted_deg": 5.002}
- ep6 gripper_orient 10.0deg：{"injected_deg": 10.0, "fitted_deg": 10.318}
- ep7 gripper_orient 20.0deg：{"injected_deg": 20.0, "fitted_deg": 20.474}
- ep8 gripper_orient 40.0deg：{"injected_deg": 40.0, "fitted_deg": 39.888}
- ep16 sync_offset 1frames：{"injected_s": 0.067, "fitted_s": null, "camera_lag_s": [0.0638, 0.0633]}
- ep17 sync_offset 2frames：{"injected_s": 0.133, "fitted_s": null, "camera_lag_s": [0.1305, 0.1299]}
- ep18 sync_offset 3frames：{"injected_s": 0.2, "fitted_s": 0.1972, "camera_lag_s": [0.1972, 0.1966]}
- ep19 sync_offset 6frames：{"injected_s": 0.4, "fitted_s": 0.3972, "camera_lag_s": [0.3972, 0.3966]}
- ep20 sync_offset -4frames：{"injected_s": -0.267, "fitted_s": -0.2695, "camera_lag_s": [-0.2695, -0.2701]}
- ep21 calib_offset 0.005m：{"injected_mm": 5.0, "injected_deg": 0.5, "fitted_mm": 5.18, "fitted_deg": 0.533}
- ep22 calib_offset 0.01m：{"injected_mm": 10.0, "injected_deg": 1.0, "fitted_mm": 9.92, "fitted_deg": 1.019}
- ep23 calib_offset 0.03m：{"injected_mm": 30.0, "injected_deg": 2.0, "fitted_mm": 29.75, "fitted_deg": 2.011}
