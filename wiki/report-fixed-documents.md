# Report 固定文档消费

> 默认装配更新：新受理已使用 [Document 正式来源装配](document-source-composition.md)。本文保留旧固定消费者的可执行契约、兼容路径与原批次验证记录，不表示新请求仍选择旧版本。

## 当前实现与装配

Report 通过 `adapters.report.fixed_documents.create_fixed_report_document_source_factory` 接收公共 Document Compatibility、Material Reader、Report Compatibility Publisher 与本域 `ReportProjectionService`。工厂只组合依赖，不执行 I/O。默认生产 bootstrap 已通过 `bootstrap.report_documents` 接入本工厂，Report 主 Agent 在受理时固定消费者清单。真实 AgentRuntime 且使用默认处理器时采用新路径；显式旧处理器工厂仍保持兼容路径，显式 `report_document_source_factory` 优先，已注入的 Pipeline 不被重新装配。源码接线不代表真实部署已验收。

公开注册 helper `adapters.report.document_profiles.configure_report_source_profiles(processor)` 只注册并核对旧 v16 提取器与字节依赖，不构造 reader 或调用存储。旧 `ReportDocumentSourceFactory` 也复用该 helper。已有其他版本的 legacy alias 保持原位，精确版本冲突明确拒绝。

组合根将相同处理器、发布闸门和 Document 快照命名空间交给公共 Compatibility、Material Reader 和旧 Report Publisher；Report 私有缓存从配置的报告产物集合派生 `_document_projections`，Document 命令集合从文档索引集合派生 `_report_publications`，两者所有权分开。固定引用和命令使用 majority 写入、PRIMARY 读取，无 TTL、建索引或构造时存储调用；复用应用客户端，不额外关闭共享资源。

配置失败只让 Report 文档能力明确不可用。`report-fixed-documents-unavailable.v1` 清单保留请求的版本和稳定失败码，不能以空工厂回落旧处理器，也不记录异常正文。无文件的结构化业务输入沿原路径执行，文件错误沿既有失败/缺资料语义处理；普通数据查询不执行文档工厂。不可用期间受理的清单与恢复后的 ready 清单不同，旧待执行任务明确封口，不自动用新实现重跑。正式来源仍使用旧兼容 Publisher，新来源生命周期不在本次装配中自动启用。

## 固定输入与本域内容

新工厂清单固定 `report-fixed-documents.v1`、输入 `legacy_understanding/1/legacy-understanding-v1`、Report 输出 selection、旧正式发布 `report_source/1/report-source-v16`、`report-understanding-public.v1` 转换和下载重试策略。`document_input_manifest()` 是同步纯方法，返回独立 JSON 数据，不含用户身份或请求状态。

每个附件在第一次 await 前冻结调用参数，准备时传递 user 对应显式 scope、session、finalReportId、fileIndex 与 `require_text=True`，保留旧扩展入口的内容失败审计语义。Document 返回的材料、事实、完整解读引用与真实 preparation receipt 在正式发布前固定。原件只通过材料快照引用读取，并验证 SHA256、字节数与所有权。Report 原件 shim 仅允许本次已选 documentId、user、subject 和 original_file，未知引用不能回到可变索引。

正常单件和组合的业务内容由 Report 自有投影与缓存生成。完整解读先由 Report 自有 document_rendering.py 冻结转换为旧 public_result 的公开内容形状，共享展示函数与提示映射的后续修改不会改变本修订，保留已审批 assessment 的旧 v16 解释语义；提供方完整证据与三份快照不改写。转换修订进入输入清单与私有缓存键，旧形状缓存不能混用。Report vNext 可与 v16 并存，旧 Record 解读快照和正式来源身份保持固定。

Document Compatibility Publisher 仅负责旧正式来源与真实引用，消费者不读取其 projection 正文作为业务内容。extractionId、outputTraceId、sourceRevision、ingestionStatus 和 jobId 来自真实公开回执与固定投影，缺失或不匹配明确失败。一次完整来源仍保留旧 base 与补充来源的幂等语义，partial 不追加伪造的完整补充来源。

## 解析与失败边界

消费 Adapter 复用 Report 自有 SharedReportDocuments 解析、trusted_report_source、exact_source_projection、原件表格与 clinical rows。模型调用和 Prompt 不增加。正常连续图片组合保留接受顺序，并按旧规则只提取同一 documentId 的首次位置；同一文件的冲突固定版本拒绝。PDF 仍不作为连续图片分组。

下载失败使用原有有界重试。解读失败仅通过公共 Material Reader 查询这一完整调用身份的固定材料；v1 至 v5 保留旧原件回退，v6 及更新版本拒绝将隔离结果重新解析为成功内容。回退不正式发布。混合原件回退组对全部输入使用原文与原件，保留重复附件顺序；含 partial 或 unavailable 的其他输入不能混入该回退。

## 重放与验证边界

同一 reader 的重试复用已固定 refs 和原 preparation receipt。正式发布 ACK 丢失后重放同一公共命令，不重新准备、不查询 latest、不重复追加来源。上述准备槽位是一次 Report 执行中的状态，不宣称跨进程恢复步骤；Document 公共命令自身持久幂等。Report 进程中断仍沿既有终态封口策略，不因此重跑生成。

离线回归覆盖真实公共提供方组合、旧新 v16 内容等价、实际输出 trace、附件去重顺序、partial 与原件表格、v5/v6 失败边界、发布 ACK 丢失、Report vNext 隔离、缓存转换修订和参数冻结。使用内存 BSON、假模型和合成原件，未调用真实数据库或模型；完整 pytest 与非作者审核绑定最终交付记录。

相关能力：[Report 私有文档投影](report-document-projection.md)、[Document 兼容解读](document-understanding-bridge.md)、[旧 Report 正式发布](document-report-publication.md)、[固定材料读取](document-material-reader.md)。
