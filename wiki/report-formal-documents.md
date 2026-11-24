# Report 固定正式来源消费

本文描述 `report-formal-documents.v1` 的本域实现。它只消费 Document 公开准备和正式来源命令，不操作 Profile 准入、事实集合或发布水位。[默认装配](document-source-composition.md) 已由组合根独立接通；当前测试使用真实公共组件和离线存储、模型替身，不代表真实服务验收。

## 执行版本与旧任务

`ReportDocumentFactoryDispatcher` 注册完整的 `documentConsumer` 清单与实际工厂。新请求选择 Formal 工厂；明确受理为 `report-fixed-documents.v1` 的任务仍进入原 `FixedReportDocumentFactory`，执行原兼容 Publisher 和本域投影。旧清单不是新实现的别名。

Report 主 Agent 仍校验整份执行清单，包括主 Agent、依赖、Prompt 摘要、内容版本、超时和恢复策略。只允许已注册的文件消费者清单变体；不从缺失字段猜测新版本。每次运行用 ContextVar 绑定实际工厂，并在成功、失败和取消后复位；并发任务不能修改共享 pipeline 的工厂。没有经过受理清单绑定的 dispatcher 拒绝创建文件消费者。

已经启动但中断的 Report 仍保留既有封口策略：保存已完成产物，为缺失产物生成确定性失败，不重新解析或调用模型。新文件命令的恢复由 Document 自己的持久任务处理，不借由 Report 查询或重建正文触发。

## 固定文件与业务投影

准备仍使用明确 `legacy_understanding / 1 / legacy-understanding-v1`，保存同次材料、Facts、派生解读和 preparation receipt；读取原件只按固定 material ref。解释结果的 Report 公开投影规则、重复文件冲突校验和失败材料选择保持原实现。

每个单文件或连续图像组合的命令身份绑定 Report 任务、会话和原始有序附件索引。先调用 `submit` 持久登记完整输入，再调用 `prepare_report` 得到固定 v16 业务投影；随后使用 `project_prepared` 写入独立的 Report 私有缓存，不再执行一次 Report extractor。缓存绑定有序输入、完整引用、提供方 snapshot ID/digest、精确选择及 `report-prepared-projection.v1`，不会命中原 `project` 缓存。

普通组合的执行按首次 documentId 去重，但受理清单保留全部原始顺序；同一 documentId 的不同固定输入拒绝合并。Report 仍从已验证原件恢复其专属评估表格信息，不把提供方增加的业务字段直接当作评估依据。

新清单显式选择已独立交付的 `document-formal-source-v2`。Document 冷 legacy 发布复用同一固定 v16 准备投影，保留原缓存身份、来源版本与真实发布 trace；实际公共组件组合验证整条冷 legacy 链路只提取一次，Report 私有 extractor 不执行。旧 `report-fixed-documents.v1` 继续选择原真实实现，未隐式切换其发布或缓存语义。

## 来源状态与正文质量

`execute` 只推进提供方的有界阶段，不等待 Profile 应用完成。`pending`、`failed`、`not_published` 和执行确认丢失后的 `unknown` 属于来源状态，保存在文件引用的 `formalSource`，组合另记录 `groupFormalSource`；它们不覆盖正文的 complete/partial/unavailable 质量。

引用中的 opaque sourceRevision、旧 job ID 和 ingestionStatus 只能来自真实完成的基础或补充回执。尚未得到该回执时不填虚构版本、任务或成功状态。`formalSource` 单独记录业务 projectionRef、真实 preparationOutputTraceId 与正式命令 ID；若 legacy 补充已有真实 extractionId/outputTraceId，旧两个字段按该发布执行成对映射，不将旧缓存 ID 配到另一条准备 trace。尚无补充执行引用时 extractionId 为 null，outputTraceId 取实际准备 trace。读取已经生成的 Report 产物仍是普通查询，不轮询或推进来源任务。

受管理来源的 partial 输入可以供原业务降级展示，但不会被提升为完整来源发布；unavailable 继续使用既有失败语义。解释失败仅在原 v6 之前规则允许时读取固定失败材料；混合 raw fallback 保留所有附件与重复顺序，并对整个组合只使用原文，不发送额外正式组合命令。

## 装配与验证

组合根先构造 Document compatibility、固定材料 reader、Formal service 和本域持久 projection cache，再调用 `create_formal_report_document_source_factory`。旧 Fixed 工厂作为独立 supported 工厂注册到 `ReportDocumentFactoryDispatcher(default=formal, supported=(fixed,))`，将 dispatcher 注入原 ReportPipeline，并由 ReportAgent 绑定实际受理版本。构造不执行模型或存储命令；Document Formal 的恢复周期需由组合根独立安装。

新测试覆盖新旧任务并发、清单漂移、取消后上下文复位、固定投影与回执校验、确认丢失、旧原文回退、实际 fresh/legacy/partial 公共组件组合。Report 全域回归同时验证既有缓存、表格解释、持久产物与回调行为。完整全量及非作者审核结果需绑定最终提交；默认接线、真实 Mongo 并发和外部模型质量仍另行验收。

相关说明：[Report 固定文档消费](report-fixed-documents.md)、[Document 正式编排](document-formal-source.md)、[受管理来源](document-managed-source.md)。
