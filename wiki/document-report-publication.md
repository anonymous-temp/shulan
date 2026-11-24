# Document 旧 Report 来源发布命令

本文记录独立 Document 提供方的兼容发布命令。提供方实现、固定输入恢复和离线回归已经具备；Report 消费者与组合根装配由后续独立迭代接入，本文不能据此认定生产 Report 已切换正式来源发布。

## 兼容目标

旧 `DocumentOutputCoordinator` 使用 `report-source-v16` 生成 report_source，并将 reportType、reportTime、reportSource 与有序 sourceDocumentIds 组成正式补充来源。它还包含部分质量禁止发布、旧缓存识别、publicationBaseKey 复用和恢复结果摘要后缀规则。新命令需要复用这些实际规则，不只发布基础 understanding，也不将 Report 新私有投影直接提交为公共 Facts。

公开契约位于 [report_publication_contracts.py](../modules/analysis/report_publication_contracts.py)。消费者自己的投影版本和缓存继续独立；普通 prepare/read、影子投影不得调用正式发布命令。

## 冻结命令

`DocumentPreparedPublicationInput` 保存 material/facts/understanding 三引用、确切 understanding selection 及原 preparation_receipt。命令保留完整有序 tuple 和所有回执；实际交给旧协调器的输入按首次 document_id 去重，与旧 process_documents 相同。同一 document_id 指向不同固定内容时拒绝，不能选择其中的更新版本。提供方执行前通过持久准备绑定验证全部引用、scope、真实回执和固定原件，再开始任何正式来源写入。

`DocumentReportPublicationManifest` 保存 scope、有序 inputs、完整输出 selection 和 command_revision。当前仅接受 `OutputSelection('report_source', '1', 'report-source-v16')` 与 `document.legacy-report-publication.v1`。恢复必须读取完整持久清单；缺版本字段不自动采用当前默认值。command_id 标识这一次固定命令，不替代旧输出缓存键或 legacy source_key；不同合法准备 trace 可以复用同一个正式来源身份。

```python
receipt = await document_report_publications.publish_report_source(
    scope=fixed_scope,
    inputs=ordered_prepared_inputs,
    selection=LEGACY_REPORT_SOURCE_SELECTION,
)
```

`DocumentReportPublicationReceipt` 返回实际执行 manifest、固定兼容投影 projection_ref、有序基础来源回执、补充来源的真实 legacy_source_revision/ingestion_status/ingestion_job_id 和 processing_status。receipt_ref 等于固定 command_id。partial/unavailable 必须使用 not_submitted 且补充来源 revision/job 为 null；基础来源回执仍保留真实结果。正式发布确认不代表画像治理完成。

`read_publication(scope, receipt_ref)` 与 `read_projection(scope, projection_ref, selection)` 为纯读入口，不重建模型输入、不选择新版本、不触发发布。

## 执行与恢复边界

1. 验证完整 manifest 及所有持久准备绑定；注册缺失、版本冲突、跨 scope、错误引用在正式写入前拒绝。
2. 先持久 command 清单，再使用固定理解快照与既有兼容命令保留各基础来源效果，按真实固定 legacy revision 取得旧 public_result，构造与历史输入等价的 outcomes；processing、sourceRevision 和来源绑定继续参与旧缓存摘要。
3. 把实际基础来源回执、完整旧 public_result、输入版本与内容写为不可变 document.legacy-report-input.v1 快照，并持久绑定 executionRef。再通过公共材料读取器核对固定原件，向同一旧输出协调器显式注入 fixed_originals。禁止修改共享 service 的方法或使用当前 Document index 选择原件。
4. 只执行固定旧 v16 兼容算法，保留既有缓存、规范来源、publicationBaseKey、结果摘要与 partial 隔离规则。混合失败材料回退不能新增正式 reportSource 来源。
5. 保存 document.legacy-report-source.v1 不可变兼容投影与真实来源回执，再返回命令结果。恢复读取固定 inputs/output/version；ACK 丢失后复用同一旧发布身份，不重新 OCR 或调用模型。executionRef 已绑定时无需再次读取旧来源正文；成功回执已存时连原件、提取器与发布器也不调用。

两种新增快照均为派生兼容材料，不是权威事实。DocumentPublisher 只接受 document.facts.v1，拒绝把上述执行输入或投影作为新版画像事实发布。material-only 失败回退无法构成本命令的完整准备输入，混合回退策略仍由 Report 负责。

| 持久位置 | 恢复行为 |
|---|---|
| command 清单，尚无 executionRef | 重验所有固定准备；使用兼容基础发布的原幂等身份补全实际来源回执 |
| executionRef 已绑定，输出未确认 | 读取完整固定 outcomes 与固定原件；复用旧输出缓存、租约和 publicationBaseKey |
| 来源已写入，但回执 ACK 丢失 | 旧 source_key 幂等核对；不增加新的逻辑修订，不清理不确定旧 writer ticket |
| 最终回执已持久 | 仅核对固定回执、投影和执行快照；无需提取器继续注册，也不触发来源门禁写入 |

这里没有新增跨集合事务。输入与投影先写不可变 chunk，再绑定小引用；中途孤立快照不作为成功回执。协调器任务仍由原 task_spawner 和任务集合管理，调用方取消等待不代表后台旧发布已停止。消费者必须按固定命令恢复，不得换一组最新输入重试。

DocumentPublisher 的新版 Facts 事件链不在本轮切换。旧 Publisher 的不确定写入与切换门禁继续保留，不能用后一次成功证明旧执行已经停止。

## 公开工厂与资源

[create_document_report_publication_service](../adapters/analysis/report_publication.py) 接收现有 processing_service、compatibility_service、material_reader 和专属 command_records 集合。组合根注册真实旧 report-source-v16 提取器；Document 不导入 Report 私有算法。工厂无 I/O，不调用模型、命令、DDL，不创建客户端或后台 worker。它验证兼容提供方与 processing 是同一实例并共用 publication gate；材料读取器必须指向同一组不可变快照和原件归档。

command_records 使用生产 majority WriteConcern、PRIMARY 读取和内建唯一 _id 索引，不配置 TTL；同 scope、固定命令只允许一个执行绑定和成功回执。大正文使用既有可分块快照集合，不内嵌命令记录。组合根负责保留所有恢复引用涉及的 snapshot、chunk、原件 trace/chunk、旧输出索引和旧来源版本，不在本工厂初始化或删除这些资源。

## 验证范围

离线回归覆盖 BSON 清单与回执、真实旧 v16 提取器、旧协调器与错标版本缓存复用、有序组合去重、同文档冲突、不同准备 trace 共用旧发布身份、全部输入先验证、固定原件完整性、execution/来源/回执写后 ACK 丢失恢复、并发幂等、partial 禁止补充发布，以及派生快照不能进入新版 Facts Publisher。完整 tests/unit 和独立审核结果随候选交付记录。真实模型、数据库多进程和线上画像效果尚未验收。

相关文档：[固定准备与兼容发布](document-compatible-publication.md)、[固定原件读取](document-material-reader.md)、[完整解读兼容桥](document-understanding-bridge.md)、[输出版本选择](document-output-selection.md)。

## 提取审计引用

固定兼容投影保存实际协调器返回的 `extractionId` 和 `outputTraceId`。消费者可分别用于原有单文档与组合文档引用，不使用准备 trace 冒充提取 trace。协调器的缓存命中也会产生本次读取的真实 trace；同一固定命令并发执行时，先持久绑定一个真实 trace，再写不可变投影，其他执行只能接受相同提取身份与内容。命令回放保持已绑定引用；读取既有回执不会重新执行提取或创建 trace。首期提供方尚未生产接入前的旧候选快照若没有该字段，缺失值只能保留未知，不能由消费者编造。
