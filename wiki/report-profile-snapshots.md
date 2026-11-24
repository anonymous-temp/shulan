# Report 的结构化画像读取

Report 通过 Profile 的 `profile-snapshot.v1` 公共读取服务取得结构化事实，在本域转换为 `ReportProfileSnapshot` 并沿用既有上下文组织、指标合并和降级规则。公开接口不返回其他业务的 Prompt，不运行模型、不创建主体、不发布来源事实。

生产装配使用 `VersionedReportProfileGateway`；旧 `HealthProfileSnapshotGateway` 仅保留给显式旧适配器注入和兼容测试。消费者不访问 Profile 私有 Mongo 结构。Profile 的正式来源摄取切换属于另一个任务，不因本次只读接入同时发生。

## 范围和一致性

读取本人及有效血亲主体，维持原主体顺序、`active/pending/disputed` 状态选择和总计 300 个事实的预算。Report 自行映射值、有效时间、更新时间和来源类型；既有 renderer 仍负责如何放入报告上下文。跨用户或跨主体的响应被拒绝。

每个公共快照先由 Profile 完成修订校验；Report 再核对所用主体和修订，必要时重读一次。持续变化标为 `stale`，预算不足标为 `truncated`，未完成的画像来源摄取不会伪装成最新事实。缺失或读取失败延续无画像降级，不等待模型提取任务完成。

本次没有复制事实权威、增加画像生成入口或修改 HTTP 参数。Report 生成开始后使用本次固定的内存快照；旧 Report 中断策略仍封口，不承诺对尚未持久化的步骤逐步恢复。

## 验证

离线测试覆盖本人和血亲的值映射、排除无关主体、公共读取不可用、跨用户拒绝、并发修订、来源待摄取标记和 300 项预算。真实 Mongo 并发一致性需独立联调；此处使用乐观修订检查，不宣称跨集合事务快照。

参见 [Profile 公共契约](profile-public-contracts.md) 和 [架构方案](../docs/architecture/modular-multi-agent-architecture.md)。
