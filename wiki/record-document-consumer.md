# Record 固定文档消费与正式发布

> 默认装配更新：新受理已使用 [Document 正式来源装配](document-source-composition.md)。本文保留旧固定消费者的可执行契约、兼容路径与原批次验证记录，不表示新请求仍选择旧版本。

本文对应已接入组合根的 Record 消费者，更新日期为 2026-11-10。生产默认使用固定快照适配器、本域 renderer、创建恢复 checkpoint 和 Document 公开兼容命令；构造资源不执行模型或来源发布。历史创建任务按持久消费者修订保留旧 `LegacyRecordDocumentAdapter`；显式注入旧处理器的调用方继续走兼容路径。这里只表示代码与离线组合已接通，真实数据库、模型及部署切换仍需单独验收。

## 消费边界

[Record 文档契约](../modules/record/documents.py) 固定 `record-document-renderer.v1` 与完整选择 `legacy_understanding / 1 / legacy-understanding-v1`。[消费适配器](../adapters/record/prepared_documents.py) 只读取公开的不可变材料、事实和完整派生解读 bundle，Record 自己决定首问所需的报告表示。Report 可以独立注册同名 `report_source` 的新契约或实现；Record 不通过该名称选取输出，也不调用共享的可变报告展示函数。

完整派生解读用于兼容现有 Record 输入，不将 `aiSummary`、Report 私有结构或 Record 展示结构变成公共事实。公共事实仍由 Document 提供方独立维护。首次创建、附件排序、同文档去重、缺失资料与首问 checkpoint 的流程继续归 Record 创建服务所有；新 renderer 不增加模型调用或改变 Prompt。

## 固定准备与恢复

新请求先通过显式命令取得本人 `DocumentScope`，调用公开兼容服务的 `prepare_bundle`。该服务须在返回前持久保存 provider 所拥有的准备绑定；仅使用基础 `DocumentUnderstandingService.prepare_bundle` 不能替代正式发布所需的绑定登记。消费者随后构造 `RecordPreparedDocumentV1`，调用并等待 `on_prepared(prepared)` 持久保存，最后才调用独立 `RecordPreparedDocumentPublisher.publish_prepared`。

准备 checkpoint 包含用户与主体、会话与附件序号、原附件身份、document/source 身份、材料修订、三个快照引用、完整输出选择、renderer 修订，以及本次真实的 `documentId / traceId / reused` 准备回执。源材料修订、旧画像来源 hash 修订和消费者 renderer 修订互不替代。新请求没有持久 checkpoint 回调时，适配器会在创建主体或处理文件之前明确失败。

恢复使用已保存 checkpoint 的 scope、版本、引用和原始准备回执，直接 `read_bundle` 读取固定快照。它不创建主体，不读当前文档索引或最新解读，不重新 OCR、调用模型或重新 prepare。Record 已保存的 documentId 引用可替代原 URL 重放；另一用户、会话、附件、版本或不一致快照会明确失败。固定快照缺失不以 latest 补齐。

如果准备 checkpoint 保存失败，不执行正式发布。如果正式发布已成功但回执丢失，恢复仍重放同一 checkpoint，通过 Document 公共发布命令取得同一来源发布结果。真实 `reused` 来自原请求准备回执，不因为恢复纯读或发布重放而变成 `true`。底层对同一材料的另一次合法 prepare 可以有新的 trace 和新的真实复用标记，其快照内容身份不因此变化。

## 本域输出与兼容

renderer 固定保留原报告元数据、患者信息、`observations`、`expansion`、`sections`、未确认项目和扩展旧字段的语义与数组顺序，同时沿用现有安全展示规则去掉内部证据和来源绑定。部分解读、技术失败及内容不足的提示已按当前行为固定在 Record 本域；共享提供方以后修改显示文案不会无声改变这个 renderer。

最终 `reportId` 与 `sourceRevision` 使用正式发布回执中的真实 canonical 身份和旧来源修订，不能使用材料快照中可能过时的 `legacySourceRevision`。原有 `report / document / processing` 三个 completedFile 键保持兼容，内部另外保存 `documentProjection.prepared / publication` 以审计和恢复。创建成功后临时 `qaCreation` 会按既有流程被活动会话替换，因此首问生成前还会把每份文档的这组元数据按附件顺序写入内部 `recordDocumentProjections`；它随首问 checkpoint 和最终会话保存，不额外复制报告正文，也不进入 HTTP 响应。既有仅有三键的完成 checkpoint 仍由创建服务直接复用，不补造历史准备回执。

`read_projection(prepared, publication)` 仅纯读固定 bundle 并运行 renderer，不发布来源，也不检查或推进摄取任务。公开 HTTP 响应和错误封装没有新增字段；准备版本或身份不一致属于内部恢复失败，不被降级成“资料为空”继续首问。

## 当前 Document 消费者

[当前工厂](../adapters/record/current_documents.py) 可显式注入 Record 创建流程。首次取得 Profile 公开的活动本人及生命周期，调用 Document 当前准备服务并保存实际选择、三个快照引用、原准备回执、生命周期和固定请求标识，再调用 Document 的固定准备消费命令。Record 使用同一份固定 Analysis 解读构造问答材料，姓名、性别、年龄只取 Document 已校验的报告字段，来源保持为报告。Report 模块负责额外的原件投影及其临床贡献。

创建恢复通过原准备绑定读取固定快照，并重放原请求与生命周期。Document 返回已持久受理的发布或复用原来源时，Record 保存实际来源修订和发布引用，首问可继续，画像应用由 Document/Profile 继续处理。只有归档且无可发布贡献时，仍按实际文件质量使用可用材料，公开处理摘要省略来源修订。读取已完成的问答快照无需重新发布。本阶段工厂供显式组合，默认装配切换由后续阶段完成。

离线组合回归通过真实 Record 创建流程、Document 固定材料消费端口和 Profile 工作器，使用内存 BSON、Redis 及合成解析。Document 已受理但确认丢失后，重建运行时仍用原准备回执和生命周期恢复。另一会话复用同一文件时取得本次实际准备追踪，复用同一来源及发布版本，画像最终保留一条 ALT 事实。该回归覆盖单来源重复消费，Analysis 与 Report 跨来源同一原件的去重验收仍由共同计划跟踪。

## 接线要求与验收

Record 创建服务通过显式 `RecordPreparedDocumentPort` 选择新消费能力，在现有创建租约内把 `on_prepared` 写入 `preparedDocuments` 附件准备 checkpoint，并在重试时传入 `prepared_checkpoint`，保存成功后才允许发布。completedFile 继续在完整投影返回后保存；已完成文件无需再调用 provider。挂起的新准备 checkpoint 若遇到旧适配器会明确失败，禁止回退旧 facade 重新处理；已完成的三键投影仍可由旧适配器路径直接复用。没有创建持久 store 的旧同步调用方继续使用旧适配器，新消费能力禁止无持久 checkpoint 执行发布。公开工厂为 [create_prepared_record_document_port](../adapters/record/document_compatibility.py)，参数为 `compatibility_service` 及可选 `legacy_port`。工厂只组装对象，不创建集合、读取数据或调用服务；同一 Document 公开兼容服务提供 `ensure_self_scope / prepare_bundle / read_bundle / publish_prepared`，消费者不调用 Document 私有发布方法或直接写 Profile 来源集合。返回发布回执必须保留原准备 trace/reused，提供方明确的准备绑定冲突映射为 Record 不可重试错误，网络或回执不确定错误保持既有可恢复语义。

新建创建回执在受理时保存 `documentConsumerRevision`，将 prepared v1 或旧兼容消费者固定下来。恢复时没有此字段且没有准备 checkpoint 的旧任务固定采用 `legacy-record-document.v1`；已有 preparedDocuments 的过渡任务固定采用 prepared v1。生产组合根应向工厂传入现有旧适配器作为 `legacy_port`，旧受理任务继续走原正式链。缺少旧实现、未知版本、显式旧版本与 prepared checkpoint 冲突时明确失败，不能按部署当前默认值改走另一条发布链。即使新任务尚未完成第一次 prepare，回退为纯旧适配器也不能迁移其已受理版本；已经保存的 completedFile 仍直接复用，不要求重新解析或重新发布。

[离线回归](../tests/unit/agents/graphs/record/test_prepared_documents.py) 验证旧 public_result、基础信息提取与首问上下文等价；真实 prepare 的 trace/reused；保存失败不发布；发布 ACK 丢失后原快照重放；跨 scope、身份、版本和引用拒绝；await 前输入冻结；以及 Report 同名新投影不会改变 Record 输出。该组测试使用真实 Document 快照服务和内存存储，隔离发布契约使用 Fake；[创建恢复回归](../tests/unit/agents/graphs/record/test_prepared_document_creation.py) 还通过真实创建协调器和 Redis CAS 模型验证重启、保存失败、首问重试、GET 纯读、完成与挂起任务的不同回退边界。[公开命令组合回归](../tests/unit/agents/graphs/record/test_document_compatibility.py) 使用真实 DocumentCompatibilityService、BSON 编解码来源/回执存储及 Record 创建 Redis CAS 模型，覆盖来源保存及回执保存后的 ACK 丢失、补充 hash 修订、提供方冲突、纯读投影和新旧消费版本切换；首问对照使用同一真实来源身份，完整比较两个私有模型请求正文和引用，模型次数保持不变。生产装配仍需独立组合验收；未执行真实 OCR、LLM、MongoDB、Redis 或回调服务。

相关设计见 [Document 完整解读兼容桥](document-understanding-bridge.md)、[Document 事实与发布边界](document-facts-publication.md) 和 [多智能体总体方案](../docs/architecture/modular-multi-agent-architecture.md)。

独立的新正式来源命令消费者见 [Record 固定正式文档消费](record-formal-documents.md)；该能力保留本页 prepared v1 的固定历史执行，默认装配需由组合根单独切换。
