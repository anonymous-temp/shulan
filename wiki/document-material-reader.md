# Document 固定原件与失败材料

`DocumentMaterialReader` 为消费者提供固定原件读取，以及不依赖 Facts/understanding 成功的材料快照。Document 负责来源、权限和内容一致性；Report 继续自行判断旧 `analysis-health-report-v6` 之前的失败是否允许来源回退，并在本域完成 PDF 表格提取和业务投影。提供方不会因版本较旧自动执行回退。

## 公共契约

公开类型位于 [modules/analysis/materials.py](../modules/analysis/materials.py)，工厂位于 [adapters/analysis/materials.py](../adapters/analysis/materials.py)。

| 方法 | 输入 | 结果与副作用 |
|---|---|---|
| `read_material` | scope、material_snapshot_ref | 只读 `document.material.v1` 固定快照 |
| `read_original` | scope、material_snapshot_ref | 返回原始 bytes；只读固定 originalArtifactRef，验证 scope、documentId、canonical 来源、SHA-256 和 byteSize |
| `capture_failed_material` | scope、DocumentSourceCall、failure | 唯一匹配且具备完整证据时保存材料快照；不适用/不唯一返回 null，证据缺失或校验失败明确报错 |

`DocumentSourceCall` 包含 entrypoint、source_id、session_id、file_index、requested_document_id、file_url、started_at 和可选 trace_id。started_at 必须带时区；至少提供实际 document 引用或 URL。所有字段对应同一次调用传给处理器的真实值，不能改写 URL、宽化时间或省略来源维度来寻找“最近一次”失败。

capture 的 trace 查找保留旧精确边界：完整用户/主体与来源身份、请求文档和 URL、开始时间下界、failed + interpreting，以及 DocumentParsingError/DocumentInterpretationError 错误类型。查询和应用层时间比较统一使用 MongoDB 的毫秒精度，原调用记录仍保留完整时间。无确切 trace_id 时最多读取两条候选，只有唯一匹配才继续；明确 trace_id 时仍校验全部其他条件。完整异常链中所有非空 trace/document 标识也必须一致，同类型包装不能遮蔽内层身份。权限异常、401/403、非内容失败或没有解析错误的异常链不能授权捕获。

```python
from modules.analysis.materials import DocumentSourceCall

call = DocumentSourceCall(
    entrypoint="report",
    source_id=final_report_id,
    session_id=session_id,
    file_index=file_index,
    requested_document_id=document_id,
    file_url=actual_file_url,
    started_at=started,
)
material = await document_materials.capture_failed_material(
    scope=fixed_scope,
    call=call,
    failure=original_error,
)
if material is not None:
    # 消费者先持久化 scope、material.snapshot_id 与自己的版本清单。
    original = await document_materials.read_original(
        scope=fixed_scope,
        material_snapshot_ref=material.snapshot_id,
    )
```

## 当次失败与版本证明

Document 处理器在实际失败或不可用结果的边界记录小型 failureMaterialBinding，包含实际材料元数据、固定原件/OCR 引用、OCR 完整内容摘要、当次解释与验证版本、轮次和失败状态。它不包含原件字节或整份 OCR 大文本，不改变原处理结果、重试、版本选择、OCR/模型调用次数或正式发布行为。

capture 只使用该绑定，验证摘要、scope、trace 与 canonical 来源，再读取其中指定的原件和 OCR。保存快照前复核失败 trace 绑定及 OCR 摘要未发生变化，不查询当前 Document 索引补齐旧字段。后续新解释器恢复了同一文档，也不能把新版本写成这次旧失败的版本；原固定引用内容尚可验证时，仍可捕获原调用的材料。

材料快照包含 parsedDocument、originalArtifactRef、parseArtifactRef、contentSha256、文件名/类型、documentMetadata，以及顶层 interpretationRevision、validationRevision、interpretationRound、interpretationResultRound。failureTrace 保存 traceId、errorType、status、stage、原调用 identity 和 bindingDigest。不存在成功解释时不创建 Facts 或 understanding，也不伪造验证成功；null 版本和验证状态如实保留。

## 旧数据与恢复差异

历史失败 trace 没有固定 failureMaterialBinding 时返回 `document.material_unavailable`，不根据当前索引猜测旧版本。这是明确的迁移差异。升级后新请求产生的失败会记录绑定；当该请求实际使用旧版本输入时，消费者仍能按原版本策略判断回退。不能修改旧 trace 冒充升级后产生的证据。

已有材料或理解桥的 material 若具备原件 ref、固定大小、hash 和完整来源元数据，可以直接 read_original。更旧材料缺少必要身份字段时明确不可用；原件丢失、scope 不符、来源不符或内容改变均不会改读其他归档、重新下载、启动 OCR/模型或发布。

read_material 只访问不可变快照存储，原失败 trace 后来不可用也不影响已保存的材料内容。read_original 仍依赖固定原件归档的保留；归档缺失或同 ref 改写时会失败，不能据快照中的 OCR 文本伪造原文件。capture 是显式写命令，不声称跨集合事务或在最后校验之后阻止其他进程删除归档。

## 装配与验证

`create_document_material_reader` 显式接收 snapshots、snapshot_chunks、traces、trace_chunks。它不依赖处理器、Document index、Profile 或 LLM。组合根负责共用已有归档资源和快照集合，配置确认写入（生产建议 majority）、primary 读取及引用保留；构造与读取不初始化索引。新提供方尚需由各消费者及 bootstrap 独立接线。

离线回归使用原 Document 处理流程、Mongo adapter 与真实 BSON 编解码，覆盖固定原件读取、跨 scope/来源拒绝、同 ref 内容改写、失败后版本升级、完整调用身份、唯一匹配、权限及错误链、无旧绑定拒绝和捕获竞态。真实数据库多进程、模型、原件保留策略及 Report 生产回退尚未验收。

相关文档：[完整解读兼容桥](document-understanding-bridge.md)、[材料与显式发布](document-facts-publication.md)。
