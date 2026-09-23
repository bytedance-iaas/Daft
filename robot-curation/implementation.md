# 工程优化实施记录

已落地的 v1 工程优化已并入默认执行路径。判定算法及正式结果结构保持不变。
`curation run` 只保留 `thinking` 开关：`--thinking` / `--no-thinking`，
也可用 `--set pipeline.thinking=true/false`。请求参数由模型策略决定。

## 已提交

| commit | 内容 |
|---|---|
| 09c6ebc70 | 测试开关、配置类型/依赖/冲突校验、v2 拒绝未接入开关 |
| 889bc6a8c | 曾加入私有 VLM executor；后确认原路径已通过 `asyncio.to_thread` 在线程池执行，现已撤掉 |
| f28bdcac6 | 单次 iter_rows 消费,保留原检查函数、硬门短路和返回 schema |
| 72f2f9b88 | 本地检查点、pre-funnel caption 持久化、恢复与运行锁 |
| 0686527e4 | 256 MiB 无损帧缓存、动作指纹复用、有序并行视频哈希 |

## 当前开关

- `thinking`：默认不传相关字段，沿用模型 API 默认。已验证的策略：
  `ark-glm5.2` / `glm-5-2-260617` 与默认豆包模型支持 `thinking.type=enabled/disabled`；
  GLM-5.3-Flash 始终思考，`--no-thinking` 映射为 `thinking.type=enabled` 加
  `reasoning_effort=low`，运行时明确提示“仍在思考”，报告配置快照也记录实际模式。
  未登记模型显式使用开关会报配置错误。

流式漏斗、帧缓存、动作指纹复用及有序并行视频哈希默认启用。
本地输入/输出自动保存检查点；同一输入、配置和实现版本再次运行时，
自动续接最近的未完成批次。逐条结果、运行身份和完成状态保存在运行目录的
`.curation-checkpoint/checkpoint.sqlite3`；每条结果提交一次 SQLite 事务。
检查点只保存在本地，不随 TOS 交付上传。

    curation run --input /data/droid --output /tmp/curation
    curation run --input /data/droid --output /tmp/curation --no-thinking

## 验证

- 流式执行、检查点、缓存与去重测试。
- 真实合成 LeRobot 视频 + 假 VLM:原路径录制,流式/线程池/缓存组合回放,完整 DataFrame schema、行内容、顺序、统计一致;模型调用无缺失、无剩余。
- 覆盖空输入、全硬门 drop、禁用检查、每项调用恰好一次、返回结果重复读取不重算。
- 覆盖 SQLite 记录校验、不同 kind 同 episode、运行锁、指纹变化、多候选拒绝、报告中断后恢复、已完成检查零重跑。
- 已有 v1→v2 录制回放对账通过;57 个冻结算法文件 changed=0、missing=0、added=0。
- 对修改前 AST 比较:run.py、funnel.py、config.py 公开函数签名保持一致。
- 已有并发/caption/仲裁/去重及新增测试合计一次执行 148 passed、7 skipped;后续边界修正另跑对应测试。跳过项需要本地未提供的真实数据集。

## 实测后调整与剩余工作

- 当前源码的漏斗有六处 collect,计划的五处是旧行号。流式实现采用每条 episode 的检查顺序不变、episode 之间并行的方式,只消费一次惰性链,未改算法。同步工作继续使用 Python `asyncio.to_thread` 的默认线程池。
- caption_episodes 已通过 precomputed=auto_caps 复用 pre-funnel caption;不再增加重复实现。M7 的取帧参数与 funnel 不完全相同,当前帧缓存只接入 funnel 的 frame/VLM 段,不宣称消除 M7 解码。
- 缓存是有界内存 LRU,不做有损 JPEG 或全库磁盘缓存。远端帧不按 URI 单独缓存;需要稳定对象版本身份后再扩展。
- 检查点目前仅支持本地输入/输出。输入指纹使用路径、大小、mtime_ns,实现指纹覆盖 Python 源码;不保证识别人为保留大小和修改时间的内容替换。远端 TOS、RRD 暂存路径跨进程稳定性及更强内容身份需要另验。
- 旧版私有 `records.jsonl` 检查点不会自动迁入 SQLite；旧版未完成运行需重新执行。
- 为完整重建旧 DataFrame,检查点保存完整 tensor 行,磁盘成本仍与数据规模相关;返回完整结果所需内存也仍然存在。全部 episode 已恢复时仍构建原扫描以取得 schema,filter 阻止重跑检查;尚未承诺零源 IO。
- 检查点只保存已持久化结果;中断时已请求但未落盘的 VLM 可能重跑。M7 文本归纳、审计、导出不在当前检查点范围内。流式单次 iter_rows 仍受 Daft morsel 出结果粒度影响,不能承诺每个协程结束瞬间就落盘。
- usage_accounting 尚未接入:现有 usage sink 是进程级全局,缺可靠 episode 归属和隔离;不能直接全局替换后声称每条记账可靠。
- thinking 已按模型映射 API 参数；GLM-5.3-Flash 的 low 档仍会思考。质量与延迟仍需真值集验证。请求合并、跳复核、少机位、批量接口和 Ray 尚未开启。
- 未执行 20/1000 条真实 DROID 吞吐实验及 105 条真值/droid-200 质量回测;本地没有这些数据或已确认的生产运行环境。1 天级目标尚未验收,不把合成测试耗时外推为生产收益。
