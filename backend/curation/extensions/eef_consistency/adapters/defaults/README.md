# 内置相机标定

会话里既没有内参文件、也没有 SLAM 日志时，`adapters/umi.derive_calibration` 按图像尺寸用这里的标定（设计 24 §4）。

| 文件 | 相机 | 来源 |
|---|---|---|
| `gopro13_intrinsics_2_7k.json` | GoPro 13，2.7K 4:3（2704×2028），Max Lens Mod 鱼眼 | GitHub `TrossenRobotics/trumi` 的 `example/calibration/gopro13_intrinsics_2_7k.json`（MIT 许可），原样拷贝，sha256 `d4d06eaa36c512129c7313f0a84227eed75c5fcc8453b5b6294ccabe13245f1f` |

这是同型号相机的近似标定，不是每台相机自己的；会话里带了内参文件或 SLAM 日志时优先用会话里的。
