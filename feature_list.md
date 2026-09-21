# Curator v2 需求账本

> 需求来源：《Physical AI Kit - Curator 设计文档Prompt》+ 两轮澄清（冻结决策见
> `robot-curation/docs/design/00-overview.md` §7）。
> **纪律**：feature 的描述、验收标准一经写入不得改写；只允许更新 `status` 字段。
> `status` 取值：`not_completed` / `completed`。

## 阶段 0：设计

### F0.1 需求澄清
- 描述：读需求文档，梳理歧义点，与需求方确认到可开工
- 验收：所有阻塞性问题有明确答复，决策记入设计文档 §7
- status: **completed**

### F0.2 工程设计文档
- 描述：输出 12 篇设计文档，覆盖架构/数据模型/CLI/REST/并发/模块/交付/前端/密钥/部署/对账/工作包
- 验收：文档可支撑多 agent 并行开工，接口契约明确到可编码
- status: **completed**

### F0.3 设计文档评审与完善
- 描述：对照需求文档、v1 代码、v1 使用文档与方舟 API 文档评审 12 篇设计文档；澄清遗留问题；逐项修订并同步飞书评审副本
- 验收：①`robot-curation/docs/design/review-2026-09-20.md` 中每一项都有结论（已修订 / 不改及理由）；②需求方拍板的问题记入 `00-overview.md` §7 决策表；③仓库 Markdown 与飞书副本内容一致
- status: not_completed

## 阶段 1：地基（串行，不可跳过）

### F1.1 对账工具与黄金基线
- 描述：实现 `curation-parity compare`；用 v1 代码对 `umi_640_notask` 跑一遍完整质检，存档为黄金基线
- 验收：①工具对 v1 自己的两次跑批比对结果为「完全一致」；②基线产物含八模块完整结果，已归档
- status: not_completed

### F1.2 Baseline 清理与目录重组
- 描述：删除上游 daft 源码，按设计文档 §6 重组目录，删除 Gradio UI 与内嵌终端，清理依赖
- 验收：①CLI 可运行；②搬运的单元测试全绿；③镜像可构建；④每步独立 commit 可回滚
- 前置：需求方确认删除上游代码
- status: not_completed

### F1.3 契约冻结
- 描述：定稿 C1 模块注册表、C2 CLI JSON schema、C3 进度协议、C4 REST OpenAPI、C5 Repository 接口
- 验收：三份 schema 为真实文件且被契约测试消费；CI 在契约漂移时变红
- status: not_completed

## 阶段 2：后端

### F2.1 CLI 原子命令
- 描述：preflight / plan / decode / check / aggregate / export / report / adjudicate-apply 八条命令
- 验收：①每条命令 `--json` 通过 schema 校验；②全链路跑通 8 条 episode；③默认不并发不重试，并发与重试为显式参数；④凭证只经环境变量，不入 argv
- status: not_completed

### F2.2 Daemon 骨架
- 描述：FastAPI 应用、SQLite Repository、鉴权中间件、SSE、静态资源托管、健康探针
- 验收：①探针豁免鉴权；②SSE 支持 Last-Event-ID 重放；③主密钥缺失时拒绝启动；④游标分页无重复无遗漏
- status: not_completed

### F2.3 任务编排
- 描述：任务/子任务状态机、worker 池、暂停恢复、停止、优雅停机
- 验收：①暂停恢复后结果与不暂停一致；②停止后无孤儿子进程；③进程重启后任务状态可恢复；④非法状态迁移返回 409
- status: not_completed

### F2.4 planner 与 VLM 请求合并
- 描述：执行计划生成、CPU/VLM 两档并发、同 episode 多模块合并、usage 采集
- 验收：①合并与单发模式判决一致率 ≥98%，差异条目人工确认无系统性偏差；②token 摊派总和等于实际用量；③合并可通过配置一键关闭
- status: not_completed

### F2.5 增量重新导出
- 描述：export manifest、diff 算法、LeRobot v2/v3 两条增量路径
- 验收：①剔除中间一条 episode 后重新导出，产物可被官方 lerobot loader 无警告加载；②未受影响的视频文件字节不变；③视频先写本地临时文件再整文件拷贝（FSX 随机写限制）
- status: not_completed

### F2.6 密钥与资源管理
- 描述：AES-GCM 加密存储、连通性校验、方舟拉模型列表、自定义端点
- 验收：①任何 API 响应与日志都不含密钥本体；②TOS 凭证校验含写探针；③删除被运行中任务引用的凭证返回 409
- status: not_completed

### F2.7 预检
- 描述：格式识别、模块可用性三态、机器人型号追问
- 验收：①非 LeRobot v2/v3 全模块标灰并给出原因；②规格库不支持的型号 → 整项跳过且任务不失败；③读不到型号 → needs_input；④未勾选该模块则不追问
- status: not_completed

## 阶段 3：前端

### F3.1 静态 HTML 预览
- 描述：先出任务列表 + 新建任务两页定风格，确认后补齐详情/报告/裁决/密钥四页
- 验收：需求方确认风格
- status: not_completed

### F3.2 前端实现
- 描述：React + Arco Design 六个页面
- 验收：①深链参数行为与 v1 逐项一致；②SSE 不可用时自动降级 5s 轮询；③所有表单必填项红 `*` 且校验；④前端零业务状态，刷新不丢；⑤模块清单完全数据驱动
- status: not_completed

### F3.3 人工裁决页
- 描述：统一裁决入口，按来源模块分组；三条裁决线语义与 v1 一致
- 验收：①「整条弃用」压过成败裁决；②复议只对归因任务成败判定的拒绝开放；③「拿不准」条目保留在队列；④视频走预签名 URL，过期自动重签
- status: not_completed

## 阶段 4：交付

### F4.1 镜像与 Helm Chart
- 描述：多阶段 Dockerfile、Chart（单副本、PVC、Secret、探针、优雅停机）
- 验收：①VKE 上 helm install 一次成功；②滚动升级时运行中任务置 paused 且可恢复；③帧缓存卷非 FSX 挂载
- status: not_completed

### F4.2 黄金对账验收
- 描述：对 `umi_640_notask` 全量执行对账
- 验收：①CPU 五项（时间戳/运动学/运动质量/视觉质量/视频-动作同步）与 dedup 逐位一致；②task_success 与 skill_profile 判决差异 <2% 且逐条人工确认
- status: not_completed

### F4.3 README 与手动验证步骤
- 描述：持续维护 README，确保人工验证步骤始终可执行
- 验收：按 README 步骤可完成一次完整质检并看到报告
- status: not_completed

## 修订记录（2026-09-20 设计评审）

> 原有条款一字未改。下面是需求方在评审中拍板的决策对各 feature 的追加与替换说明，
> 与原文冲突处以本节为准。决策编号见 `robot-curation/docs/design/00-overview.md` §7。

- **F1.1 / F4.2（D19）追加**：黄金基线为两个数据集 —— `umi_640_notask` 与 `droid_lerobot` 前 50 条，两个都要过。
  对账比较的是规范化记录，v1 一侧由 `tools/parity/dump_v1.py` 导出；F1.1 验收①的「完全一致」指确定性六项，
  VLM 三项给出 v1 自身的波动基线。
- **F2.1（D18 / D22）替换**：原子命令为 preflight / plan / autolabel / check / aggregate / export / report /
  adjudicate-apply / verify 九条（`decode` 取消，新增 `autolabel` 与 `verify`），另加客户端命令 `curation task …`。
  `check` 可一次接受同一档内的多个模块。
- **F2.2（D21）替换**：验收④的「游标分页」改为：任务列表页码分页且 `total` 正确；裁决队列、日志、episode 列表用游标。
- **F2.3（D20）追加**：状态机含「待启动」与系统暂停；`stopped` / `failed` 可继续运行；优雅停机超时不把任务置为失败。
- **F2.4（D23）替换**：现有两个 VLM 模块不参与合并、按 v1 调用图原样跑；验收①②由示例模块承担，
  另加「N=64 时推导出的闸门与 v1 出厂默认逐项相等」。
- **F2.7 追加**：没有任务标注不导致 VLM 模块标灰（由 autolabel 补）；未选 VLM 后端 → needs_input。
- **F3.2 追加**：在 `/curation` 挂载前缀下可用；任务列表页码分页。
- **F4.1 追加**：StatefulSet + 块存储数据卷；不再有帧缓存卷；升级后系统暂停的任务自动续跑。

## 范围外（本期明确不做）

- 新质检模块的算法实现；算法调优、阈值调整
- mcap / LanceDB 的 reader
- 火山 IAM 对接（只留 AuthProvider 抽象）
- 跨 episode 的 VLM 帧合并（只留 MergeStrategy 扩展点）
- VLM 并发度与合并粒度的调优（单独任务）
- 多副本、外置 RDS、请求层 LB
- 与火山控制台的像素级样式对齐
