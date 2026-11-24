# Document 完整解读兼容桥

Record 与 Report 的旧流程依赖原完整解读中的患者线索、基本信息、报告结构和派生摘要。中立 Facts 快照不能无损替代这些输入。`DocumentUnderstandingService` 提供独立的 `document.understanding.v1` 不可变快照，明确标为 `derived_interpretation`，供消费者实现本域投影；它不是 Profile 权威事实，也不扩充 Facts 的临床字段。

## 固定契约

桥接实现使用完整 `OutputSelection('legacy_understanding', '1', 'legacy-understanding-v1')`，公开常量为 `UNDERSTANDING_SELECTION`。这是兼容桥版本，不是模型解读器版本；快照另行记录实际 interpretationRevision、validationRevision、interpretationRound、interpretationArtifactRef 和 interpretationContentDigest。未知旧输入版本保留 null，并标记 `legacy_unversioned`，不冒充当前部署版本。

`DocumentPreparedBundle` 包含 material、facts、understanding 三个固定快照。understanding 保存完整原始 interpretation，包括 subject/basicInfo/patientInfo、顶层人口线索、reportType/title/reportTime、sections、观察与证据、aiSummary 以及原有其他字段；读取不重写、截断或过滤这些原字段。facts 仍只包含中立 observations、质量与溯源信息，aiSummary 不作为事实发布。

`prepare_bundle` 还返回独立的 `DocumentPreparationReceipt`，提供本次实际 document_id、trace_id 与 reused。该回执不进入任何快照内容或身份；同一文档重复准备可以复用三个快照，同时拥有新的请求追踪标识和真实 reused 状态。消费者如需恢复原请求的交付字段，应单独持久化回执；`capture_bundle` 与 `read_bundle` 的 receipt 始终为 null，不伪造一次新处理的复用结果。

understanding 中的 factsSnapshotRef、materialSnapshotRef 绑定同一来源与材料修订。documentMetadata 保留实际 documentId、canonicalReportId、内容 hash、文件名/类型、firstFileUrl、字节数、taskId、状态、原件 ref 和单独的 legacySourceRevision。firstFileUrl 表示最初存档地址，不替代消费者本次附件 URL；taskId 不存在时保持 null。documentMetadata.status 表示处理任务是否已结算，质量仍以 quality.status 判断，不能将 completed 当成 complete，也不能把 partial 改为成功完整。

## 命令与纯读

| 入口 | 输入与效果 | OCR / 模型 | 正式发布 |
|---|---|---|---|
| `prepare_bundle` | 明确 scope 与桥版本，执行一次既有 publish=False 处理，再返回一致的三快照与独立请求回执 | 只按现有处理/恢复政策执行，不为桥额外调用 | 无 |
| `capture_bundle` | 对已有 documentId 的已结算解析/解释显式归档，验证版本、ref、原内容摘要和元数据未并发变化 | 无 | 无 |
| `capture_understanding` | 从指定 factsSnapshotRef 及其固定原解释 ref 构建兼容快照；缺失或摘要不同则拒绝 | 无 | 无 |
| `read_understanding` / `read_bundle` | 按 scope、快照 ID 与确切桥版本只读不可变存储 | 无 | 无 |

```python
from modules.analysis.understanding import UNDERSTANDING_SELECTION

bundle = await document_understanding.prepare_bundle(
    scope=resolved_scope,
    selection=UNDERSTANDING_SELECTION,
    document_id=stored_document_id,
    resume_incomplete=False,
)
# 消费者保存引用及自己的 renderer 版本，后续恢复仅按这些引用读取。
frozen = await document_understanding.read_bundle(
    scope=resolved_scope,
    selection=UNDERSTANDING_SELECTION,
    understanding_snapshot_id=bundle.understanding.snapshot_id,
)
legacy_input = frozen.understanding.payload['interpretation']
```

真正的 read 不访问当前 Document 索引、原解释归档或处理器，不创建主体、索引、任务或发布事件。原归档后来改写或不可用，也不改变已经保存的 understanding。读取缺失快照会明确报错，不执行 ensure/OCR/model，不恢复或改读 latest。

需要读取原文件字节时，使用独立的 [固定原件与失败材料接口](document-material-reader.md)，按 material 引用验证固定原件，而不是从当前 documentId 索引重新选择归档。该接口也可捕获有当次版本证明的失败材料，不要求 Facts 或 understanding 成功。

## 一致性与旧数据

非发布处理结果通过独立 `DocumentPreparationBinding` 固定实际输入版本、ref、解析/解释完整内容摘要和文档元数据。准备返回后发生恢复或重写，capture 必须拒绝；capture 还会二次核对输入，避免将不同处理轮次拼成一组。该绑定未加入既有 DocumentInputSnapshot，因此不改变原 output-profile 缓存身份。

新 bundle 的 Facts 增加 interpretationContentDigest 溯源字段，材料增加 documentMetadata。原 `capture_facts`、`capture_material` 和 `prepare_document` 输出保持原样；旧 Facts 缺少完整解释摘要时不能据当前归档补造旧解释。旧快照仍可按原能力读取或显式发布，但新增理解桥会返回 `document.understanding_unavailable`。若业务需要为当前已结算输入新建一组引用，应显式执行 capture_bundle 并保存新引用，不能把新引用写成原任务的旧版本。

快照保存使用既有按内容寻址的分块存储。保存中断可以留下未被返回的固定快照，但不会返回缺失成员的 bundle。这里的二次读取与摘要校验是捕获一致性检查，不声称跨 MongoDB 文档事务或旧归档的永久保留。

## 发布与消费者边界

DocumentPublisher 仍只接受 `document.facts.v1`，直接提交 understanding 会被拒绝。桥不会触发旧 Publisher，也不会自动切换新来源事件链。需要在不可变读取迁移期间保留既有正式来源效果时，使用独立的 [固定准备与兼容发布命令](document-compatible-publication.md)：该 facade 在 prepare 返回前持久绑定原回执，消费者保存 checkpoint 后才能显式发布。基础桥的 prepare/capture 不建立该发布绑定。

Record/Report 分别持有自己的 renderer 与输出版本，不依赖彼此的投影结果。当前提供方新增能力不修改业务消费者或 bootstrap；正式接入仍由各域独立完成。

离线回归覆盖完整字段、partial 与未知旧版本、单次处理、纯读隔离、固定旧 ref、同 ref 改写、准备/捕获恢复竞态、跨用户拒绝、旧提供方缺绑定、Publisher 拒绝派生输入，并继续执行既有输出缓存、事实快照和发布回归。真实模型、MongoDB 多进程、线上原件保留及消费者部署切换未在本轮验收。

相关入口：[材料与事实发布](document-facts-publication.md)、[输出版本选择](document-output-selection.md)、[目标架构](../docs/architecture/modular-multi-agent-architecture.md)。
