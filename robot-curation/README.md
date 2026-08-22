# robot-curation

机器人演示数据集的质检与清洗服务: 接收 LeRobot v2 / v3 格式的数据集, 用六个维度做自动质检,
把拿不准的轨迹交给人裁决, 最后产出一份可直接用于训练的交付数据集和一份可追溯的质检报告。
数据放在火山 TOS 对象存储里, 质检时不用先把数据集整个拷到服务器上。底座是 Daft 数据引擎。

- 版本与变更: [release_note.md](release_note.md)
- 两种用法: Web 质检台(浏览器里点)和命令行 `curation`

## 安装

容器镜像是推荐方式(`Dockerfile` 在本目录); 本地开发:

```bash
pip install -r requirements.txt
pip install -e .
curation --help
```

模型服务用火山方舟, 密钥通过环境变量 `ARK_API_KEY` 注入,
不写进任何文件。读写 TOS 用 `TOS_ACCESS_KEY` / `TOS_SECRET_KEY` / `TOS_REGION`。

## 三分钟跑一次

```bash
# 1. 质检: 对 TOS 上的一个数据集跑前 20 条
curation run --input  tos://你的桶/datasets/droid --input-region cn-beijing \
             --output tos://你的桶/deliveries/droid-20 --max-episodes 20

# 2. 看结果 + 人工裁决: 打开质检台, 「质检报告」页选交付名 droid-20
curation ui --delivery tos://你的桶/deliveries --port 7860

# 3. 执行裁决: 按界面上做的裁决重判, 交付数据集同步修正(界面上也有一键按钮)
curation rejudge --delivery tos://你的桶/deliveries/droid-20 --delivery-region cn-beijing \
                 --input    tos://你的桶/datasets/droid        --input-region cn-beijing
```

跑完的交付目录长这样:

```
deliveries/droid-20/
├── human-decisions/        人工裁决记录, 跨跑批累积
├── 20260816-052943/        一次跑批的全部产出
│   ├── report.md  passed.json  reject.json  review.json  run.json
│   ├── details/            明细 CSV、证据帧、同步曲线、裁决用视频片段
│   ├── episodes_parquet/   清洗后数据集(轨迹级)
│   └── lerobot_curated/    完整的 LeRobot 数据集, 拿走即用
└── latest                  最近一次跑批的目录名
```

## 完整流程

```
原始数据集 ──▶ ① 质检 ──▶ 交付目录(通过 / 拒绝 / 待裁决)
                               │ 有待裁决的
                               ▼
                        ② 人工裁决(界面, 只记录不改数据)
                               ▼
                        ③ 执行裁决(curation rejudge / 界面一键)
                               ▼
                          ✅ 终态数据集
```

| 维度 | 类型 | 查什么 |
|---|---|---|
| 时间戳检查 | 一票否决 | 时间轴单调、无重复、帧率合理 |
| 运动学极限 | 一票否决 | 关节位置 / 速度是否越过该机器人型号的物理极限 |
| 运动质量 | 打分项 | 平滑度、尖刺、路径效率、末态稳定、夹爪一致性、执行器饱和、卡顿 / 静止 |
| 视觉质量 | 打分项 | 糊、曝光、坏帧(逐相机) |
| 视频-动作同步 | 一票否决 | 画面与动作的时间轴是否对齐, 区分相机没拍到、视角造成的假错位、真正的时间错位 |
| 任务成败判定 | 一票否决 | 视觉语言模型判这条演示到底成没成(多相机取证 + 多路仲裁) |

一票否决项违反即判废; 打分项加权求和, 低于阈值才判废。证据不足的轨迹**弃权**, 进待裁决队列, 不直接判废。
数据集级还做技能画像、标注审计(发现原始标注与画面不符的轨迹)和字节级精确去重。

## 数据来源

Web 质检台的「数据集目录」二选一:

- **私有**: 填 `tos://桶/前缀` + 地区。本实例挂载的桶直接从挂载盘读; 其他桶用本实例的 TOS 密钥在线直读
- **字节 HuggingFace 镜像**: 匿名只读的公共数据集镜像, 目录与地区自动填好, 只列 LeRobot 格式的数据集

桶名或地区填错会立刻弹窗提示; 交付目录填完即做一次真实写探针, 写不进去当场说。
从 rerun viewer 点「Diagnose」可直接跳进质检台, 数据集地址、地区、交付目录自动填好。

## 命令行速查

| 命令 | 作用 |
|---|---|
| `curation run` | 质检 + 清洗 + 交付。`--lite` 跳过模型环节秒级出报告; `--only` / `--skip` 选模块; `--max-episodes N` |
| `curation rejudge` | 按人工裁决重判并同步交付数据集 |
| `curation reprofile` | 技能体系或判据调整后, 重算老交付的技能画像(不重跑模型) |
| `curation review-page` | 生成静态审片站 |
| `curation prune` | 清理旧批次(不碰裁决记录) |
| `curation fetch` | 从镜像拉公开数据集到自己的桶 |
| `curation public` | 列出镜像里可直接质检的公共数据集 |
| `curation backends` | 模型服务在线状态 |
| `curation ui` | 启动 Web 质检台 |

输入 / 输出都接受 `tos://` 地址和本地挂载路径。

## 配置

出厂默认在 `curation/pipeline/default.yaml`; 站点配置 `site.yaml` 通过 `--config` 或环境变量
`CURATION_CONFIG` 叠加在默认之上, 只写与默认不同的部分; 单次运行用 `--set 键=值` 覆盖。

常用段落:

```yaml
vlm_backends:            # 模型服务预设, 界面「模型服务」下拉从这里来
  ark:
    endpoint: https://ark.cn-beijing.volces.com/api/v3
    model: <方舟 Model ID>
    api_key_env: ARK_API_KEY
tos_buckets:             # 本实例挂载的桶及其数据集根
  - bucket: <桶名>
    mount_root: /mnt/tos
    datasets_path: /mnt/tos/datasets
public_datasets:         # 公共数据集镜像(不配 = 界面上不出现这一项)
  bucket: <镜像桶名>
  region: cn-beijing
```

## 部署

火山 VKE 容器部署: 数据桶通过 TOS-FSX 挂载或直连; 站点配置走 ConfigMap; API 密钥与界面凭证只存
K8s Secret; 公网入口经 APIG 网关 + Basic 认证。隔离靠部署——**一个客户一个实例**, 不同客户不共用。

## 安全约定

- API 密钥永不出现在配置文件、命令行、镜像里; 配置只写环境变量名
- 界面上的路径框只收 `tos://` 地址, 不收容器内本地路径
- 内嵌终端仅供内部排障, 客户部署默认不开

## 已知限制

- rerun(`.rrd`)格式的质检本版本默认关闭(`ingest.rrd_enabled: false`)
- 命令行 `--batch` 暂不接受 `tos://` 地址(界面上的「跑全部」支持直连桶)
- 直连桶的交付先落在实例本地盘、跑完整体上传, 交付体积受实例本地盘容量限制

## License

随 Daft 仓库, Apache 2.0。
