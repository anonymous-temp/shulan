# Document 固定准备与兼容发布

`DocumentCompatibilityService` 将不可变读取迁移与正式来源迁移分开。Record、Report 可以先保存一组固定材料/事实/解读引用，再显式请求 Document 使用原 `LegacyPublicationGateway` 发布。该命令保留原 canonicalReportId、sourceFingerprint、实际旧 interpretationVersion 与 sourceRevision 算法，不新增 OCR、模型调用或业务投影，不启动新版 Profile 来源切换。

## 公开入口

实现位于 [公共命令](../modules/analysis/compatibility.py)，生产装配入口为 [create_document_compatibility_service](../adapters/analysis/compatibility.py)。

| 方法 | 调用责任与效果 |
|---|---|
| `ensure_self_scope(user_id)` | 首次业务请求显式创建或读取本人主体，返回 DocumentScope；恢复不得调用 |
| `prepare_bundle(scope, selection, **kwargs)` | 调用既有无发布准备，返回三快照及真实准备回执；返回前持久绑定 scope、三引用、完整 selection、真实发布身份和本次 receipt |
| `read_bundle(scope, understanding_snapshot_id, selection)` | 纯读固定快照，receipt 为 null，不处理文件、创建主体、查询当前解释或发布 |
| `publish_prepared(scope, understanding_snapshot_id, selection, preparation_receipt)` | 验证原持久准备绑定，使用固定原解释与旧发布身份执行兼容命令，返回持久发布回执 |

selection 继续使用 `UNDERSTANDING_SELECTION`。`DocumentLegacyPublicationReceipt` 包含原 preparation_receipt、canonical_report_id、legacy_source_revision、ingestion_status、ingestion_job_id。摄取状态是首次发布确认时的真实状态，不是实时画像处理查询，也不代表事实已经治理完成。sourceRevision 保持旧整数/哈希语义，不作为新版单调 publicationRevision。

## 消费顺序与恢复

```python
from modules.analysis.understanding import UNDERSTANDING_SELECTION

scope = await document_commands.ensure_self_scope(user_id)
bundle = await document_commands.prepare_bundle(
    scope=scope,
    selection=UNDERSTANDING_SELECTION,
    file_id=file_url,
)
# 消费者先持久化 scope、三引用、selection、bundle.receipt 和自己的 renderer 版本。
await consumer_checkpoint.save(bundle)
receipt = await document_commands.publish_prepared(
    scope=scope,
    selection=UNDERSTANDING_SELECTION,
    understanding_snapshot_id=bundle.understanding.snapshot_id,
    preparation_receipt=bundle.receipt,
)
```

恢复只使用 checkpoint 中的 scope、确切 selection、understanding 引用及原 preparation receipt。不得重新 ensure_self_scope 或 prepare_bundle，也不能生成新 trace 来替代原请求。读取 bundle 后由消费者自有 renderer 还原本域输入，Document 不接收 Record/Report 的投影作为来源事实。

必须通过兼容 facade 的 prepare_bundle 建立准备绑定；基础 DocumentUnderstandingService 的 prepare/capture 仍是无发布能力，不会自动授权本命令。不同合法准备请求可以复用同一组快照；各自 receipt 的 trace_id/reused 保持真实，任意已经持久的旧 trace 均可重放，不要求它是最近一次准备。请求回执不进入三快照内容身份。

## 固定身份与失败边界

新捕获的 documentMetadata 增加原 sourceFingerprint 和 legacyPublicationVersion。后者保留旧文档缺字段时的 `legacy` 与已存 null 的区别，不根据当前部署版本补齐。完整原解释、真实输入版本/ref/digest、scope、canonicalId 及三快照关联在准备时冻结；发布不读取当前 Document 索引或可变解释归档。准备之后其他流程更新解释，原 checkpoint 仍发布原固定输入。

本次未修改 DocumentInputSnapshot 或既有 output-profile 缓存身份。旧不可变快照保持可读；缺少发布身份或未持久兼容准备绑定时，本命令明确拒绝。不能根据当前最新解释或索引把旧任务补造成已经固定的版本。

发布继续调用旧网关对原 Document 解读执行的既有清理、规范来源保存及补充修订逻辑，复用其稳定来源键和唯一存储。understanding 仍是派生输入，本命令不会把它提交给新版 DocumentPublisher；新版 Publisher 仍只接受 Facts，两个发布模式不混用。

| 中断位置 | 恢复语义 |
|---|---|
| 准备绑定未确认完成 | 不返回可发布 bundle；消费者不能保存成功 checkpoint |
| 消费者 checkpoint 已保存、发布尚未开始 | 按原 checkpoint 显式 publish_prepared |
| 旧来源写入成功、回执未保存 | 重放固定旧来源键，规范来源/补充版本幂等复用，不重跑 OCR 或模型 |
| 发布回执已保存、返回 ACK 丢失 | 读取同一持久回执，不再次调用旧 Publisher |
| 旧外部写入结果不确定 | 原 legacy ticket 保留；后续幂等重放成功不证明旧执行已停稳，不自动清除切换阻塞 |
| 新来源模式已经启用 | 旧网关拒绝新兼容发布；已有持久回执仍可只读返回，不产生新写入 |

这里没有跨集合事务。旧网关的 legacy ticket 若因不确定写入滞留，需要来源所有者在实际停止旧执行并核对来源后独立治理；该命令不以超时、取消或后一次成功为凭据自动清除 ticket。新版来源切换仍是后续独立迭代。

## 工厂与生产接线要求

工厂显式接收 processing_service、profile_service、snapshots、snapshot_chunks、compatibility_records，以及可选 publication_ledger；构造本身不访问数据库。三个集合由组合根提供专属持久资源，使用 MongoDB 自带唯一 `_id` 索引即可，不在读取时创建索引。大快照使用已有分块存储，兼容记录只存引用、绑定和短回执。生产应配置 majority WriteConcern、primary 读取及保留恢复引用的备份/保留策略，不对仍可能恢复的记录设置 TTL；未确认写入会报错，不返回成功。

启用 publication_ledger 时，既有 processing_service 与兼容 facade 必须共用同一实例，工厂会拒绝不一致的门禁。所有旧写入者停稳及接收方水位核对依然必要；只给新 facade 配置门禁不能隔离旧路径。组合根已提供 `bootstrap.document_contracts`：在配置的 Document 索引集合命名空间下派生 `_snapshots`、`_snapshot_chunks`、`_compatibility` 三个专属集合，复用原客户端并固定 majority 确认写入与 primary 读取。Record 生产默认接入准备/发布链，历史创建任务由受理时的消费者修订选择旧路径；显式旧处理器注入保留兼容行为。该装配不自动启用新版事实来源切换，Report 私有投影和兼容 `reportSource` 发布仍独立接入。

## 验证范围

离线用例使用原处理器、旧来源服务、Mongo adapter 与真实 BSON 编解码，覆盖首次主体命令、准备无发布、纯读恢复、固定旧版本、不同合法请求回执、并发重复发布、补充版本、ACK 丢失、跨 scope/selection/receipt 拒绝、partial/未知旧版本与切换门禁。真实 MongoDB 多进程、模型、画像治理完成及部署切换尚未验收。

相关文档：[完整解读兼容桥](document-understanding-bridge.md)、[材料与事实发布](document-facts-publication.md)、[输出版本选择](document-output-selection.md)。
