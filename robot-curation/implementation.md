# 工程优化实施记录

已实现默认关闭的 v1 开关,既有 Python 函数签名、判定算法及正式结果结构保持不变。
入口为 curation run 的 --enable / --disable,或原有 --set pipeline.optimizations.<flag>=true/false。

## 已提交

| commit | 内容 |
|---|---|
| 09c6ebc70 | 测试开关、配置类型/依赖/冲突校验、v2 拒绝未接入开关 |
| 889bc6a8c | 私有 VLM executor,保留上下文传播和调用方默认线程池 |
| f28bdcac6 | 单次 iter_rows 消费,保留原检查函数、硬门短路和返回 schema |
| 72f2f9b88 | 本地检查点、pre-funnel caption 持久化、恢复与运行锁 |
| 0686527e4 | 256 MiB 无损帧缓存、动作指纹复用、有序并行视频哈希 |

## 当前可用开关

- dedicated_executor
- streaming_funnel
- checkpoint(依赖 streaming_funnel)
- resume(依赖 checkpoint)
- frame_cache
- action_hash_reuse
- parallel_video_hash

未实现的 flag 仍保留在计划清单,显式开启会报配置错误,不会假装生效。
所有已实现 flag 默认 false; --disable all 可覆盖站点配置回到原执行路径。

基线示例:

    curation run --input /data/droid --output /tmp/ab/base --disable all

工程组合示例:

    curation run --input /data/droid --output /tmp/ab/optimized --enable dedicated_executor --enable streaming_funnel --enable frame_cache --enable action_hash_reuse --enable parallel_video_hash --enable checkpoint

上一条中断后,同一输入/配置/实现版本恢复:

    curation run --input /data/droid --output /tmp/ab/optimized --enable dedicated_executor --enable streaming_funnel --enable frame_cache --enable action_hash_reuse --enable parallel_video_hash --enable checkpoint --enable resume

## 验证

- 新增开关、线程池、流式执行、检查点、缓存与去重测试。
- 真实合成 LeRobot 视频 + 假 VLM:原路径录制,流式/线程池/缓存组合回放,完整 DataFrame schema、行内容、顺序、统计一致;模型调用无缺失、无剩余。
- 覆盖空输入、全硬门 drop、禁用检查、每项调用恰好一次、返回结果重复读取不重算。
- 覆盖截断日志、不同 kind 同 episode、运行锁、指纹变化、多候选拒绝、报告中断后恢复、已完成检查零重跑。
- 已有 v1→v2 录制回放对账通过;57 个冻结算法文件 changed=0、missing=0、added=0。
- 对修改前 AST 比较:run.py、funnel.py、config.py 公开函数签名保持一致。
- 已有并发/caption/仲裁/去重及新增测试合计一次执行 148 passed、7 skipped;后续边界修正另跑对应测试。跳过项需要本地未提供的真实数据集。

## 实测后调整与剩余工作

- 当前源码的漏斗有六处 collect,计划的五处是旧行号。流式实现采用每条 episode 的检查顺序不变、episode 之间并行的方式,只消费一次惰性链,未改算法。
- caption_episodes 已通过 precomputed=auto_caps 复用 pre-funnel caption;不再增加重复实现。M7 的取帧参数与 funnel 不完全相同,当前帧缓存只接入 funnel 的 frame/VLM 段,不宣称消除 M7 解码。
- 缓存是有界内存 LRU,不做有损 JPEG 或全库磁盘缓存。远端帧不按 URI 单独缓存;需要稳定对象版本身份后再扩展。
- 检查点目前仅支持本地输入/输出。输入指纹使用路径、大小、mtime_ns,实现指纹覆盖 Python 源码;不保证识别人为保留大小和修改时间的内容替换。远端 TOS、RRD 暂存路径跨进程稳定性及更强内容身份需要另验。
- 为完整重建旧 DataFrame,检查点保存完整 tensor 行,磁盘成本仍与数据规模相关;返回完整结果所需内存也仍然存在。全部 episode 已恢复时仍构建原扫描以取得 schema,filter 阻止重跑检查;尚未承诺零源 IO。
- 检查点只保存已持久化结果;中断时已请求但未落盘的 VLM 可能重跑。M7 文本归纳、审计、导出不在当前检查点范围内。流式单次 iter_rows 仍受 Daft morsel 出结果粒度影响,不能承诺每个协程结束瞬间就落盘。
- usage_accounting 尚未接入:现有 usage sink 是进程级全局,缺可靠 episode 归属和隔离;不能直接全局替换后声称每条记账可靠。
- thinking、请求合并、跳复核、少机位、批量接口和 Ray 均未开启,仍需端点能力、完整契约和真值集验证。
- 未执行 20/1000 条真实 DROID 吞吐实验及 105 条真值/droid-200 质量回测;本地没有这些数据或已确认的生产运行环境。1 天级目标尚未验收,不把合成测试耗时外推为生产收益。
