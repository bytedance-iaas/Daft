# robot-augmentation

机器人演示数据的外观增广：不改动作、不改相机，只改画面里的外观（背景、桌面材质、被操作的物体），把一条已有 episode 变成若干条视觉分布不同、动作标签原样可用的新数据。生成模型用火山方舟 Seedance 2.5（编辑模式），几何约束不靠分割掩码，而是让 VLM 看画面做决策（在哪切段、拿哪帧当参考、核什么），生成后逐段过门、不过就重抽。

**状态：实验阶段。** 方法在单条 so101 抓放 episode 的前置相机上验证过（换本子材质与桌面背景；换被操作方块的材质），批量、多机位、部署与测试尚未做。与 robot-curation 只做上下游黑盒：读它交付的 LeRobot 数据集，产出同布局的 LeRobot 数据集，不 import 它的内部。

- 原理与两条路线（VLM 版 / 掩码档案版）的对比：[docs/chain-vs-mask-comparison.md](docs/chain-vs-mask-comparison.md)
- 流程图：[docs/seedance-pipeline-diagram.md](docs/seedance-pipeline-diagram.md)

## 安装

```bash
pip install -e robot-augmentation            # 基础：PyAV、OpenCV、numpy、requests、TOS SDK
pip install -e "robot-augmentation[mask]"    # 可选：掩码档案模式需要 torch / transformers / ultralytics
```

## 配置

全部走环境变量，代码里不写死数据集、桶或路径（见 `augmentation/config.py`）：

| 变量 | 含义 |
|---|---|
| `ARK_API_KEY`、`ARK_BASE_URL` | 火山方舟密钥与端点（Seedance、VLM 都走它） |
| `TOS_ACCESS_KEY`、`TOS_SECRET_KEY`、`TOS_ENDPOINT`、`TOS_REGION` | TOS 对象存储凭证与端点 |
| `AUG_LEROBOT_SRC` | 源 LeRobot 数据集根（`tos://...`），视频与 parquet 从这里取，封数据集以它为底 |
| `AUG_OUT` | 产物落地的 TOS 前缀 |
| `AUG_WORK` | 本地工作目录（默认 `./augment_work`） |
| `AUG_CAMERA` | 要增广的相机键（默认 `observation.images.front`） |

## 模块

| 模块 | 作用 |
|---|---|
| `ark_video` | 方舟视频生成异步任务客户端（创建、轮询、下载） |
| `tos_io` | TOS 读写与预签名 |
| `video_ops` | 切段、重采样、带重叠的拼接（色调对齐 + 交叉淡入淡出）、按源规格重编码、并排对比 |
| `build_dataset` | 把增广后的相机视频封回一份完整的 LeRobot v3 数据集（其它相机、动作、状态原样） |
| `config` | 环境变量配置 |
| `collect`、`review_page`、`compare` | 收产物进评审目录、生成本地评审页（原 \| 生成 \| 叠加 三格同步）、并排对比视频 |

| `vlm` | 方舟 VLM 调用与通用问答（物体清单、编辑目标解析、材质短语） |
| `maskfree` | 不用掩码的几何层：切点探针、锚帧、意图门、多抽择优、夹缝门（实验） |
| `vlm_check` | 原帧与生成帧并排问 VLM：多画了什么、少了什么（逐抽门与全片复核） |
| `pipeline` | 主流程：切段、逐段生成与过门、拼接、复核、封包 |
| `evaluate`、`geometry` | 运动一致性与几何指标 |

## 跑一条

```bash
export ARK_API_KEY=... TOS_ACCESS_KEY=... TOS_SECRET_KEY=...
export AUG_LEROBOT_SRC=tos://<bucket>/<path>/lerobot_curated AUG_OUT=tos://<bucket>/augment/<dataset> AUG_WORK=/tmp/augment_work
augmentation --name metal_wood --edit "把桌面上那本牛皮纸本子替换成拉丝不锈钢金属板，本子的位置、大小和形态不变；把白色桌面替换成木纹桌面" \
    --seg-len 17 --build-dataset
augmentation --name gold_silver --edit "本子和桌面保持不变；把红色方块换成抛光的黄金材质，把蓝色方块换成抛光的白银材质，方块的位置、大小和形状不变" \
    --seg-len 28 --pick-draws 3 --max-draws 4 --build-dataset
```

默认走 VLM 几何层；`--geom <档案目录>` 切到掩码档案模式；`--chain-only` 为纯链式对照。

## 掩码档案模式（可选，实验）

先建几何档案（VLM 列物体 → GroundingDINO 出框 → VLM 核框 → SAM2 逐帧传播 → Depth‑Anything），再让切段、锚帧、意图门按掩码做；权重放在 `AUG_MODELS`（默认 `augment_work/models`）：

```bash
pip install -e "robot-augmentation[mask]"
python3 -m augmentation.geom_archive --video augment_work/src/front.mp4 --out augment_work/geom/front
augmentation --name gold_silver --edit "..." --seg-len 28 --geom augment_work/geom/front --build-dataset
```

同题对比下两种模式结果等价，VLM 版更便宜、更快，且不依赖分割质量；掩码版留作精细模式与像素级兜底（合成 `--composite`、碎片门 `--sliver-gate`，都默认关，详见 docs）。产物在 `$AUG_WORK/final/<name>/`（生成视频、三格对照、`run.json`）与 `$AUG_OUT/final/<name>/`，`--build-dataset` 另在 `$AUG_OUT/datasets/<name>/` 封一份 LeRobot 数据集。
