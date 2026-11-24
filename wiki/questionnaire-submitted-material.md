# Questionnaire 已提交材料公共读取

## 当前范围

本能力为 Document 的问卷附件兼容 Publisher 提供独立 owner 证据，不迁移正式发布，不修改 HTTP 接口，不调用 Agent、模型、OCR 或画像。

公开入口为 [Questionnaire 应用能力](../modules/questionnaire/application.py)，实现位于 [固定材料 Reader](../modules/questionnaire/submitted_material.py)；[生产适配器](../adapters/questionnaire_submitted_material.py) 使用 Questionnaire 自有主集合、附件和分块集合的 PRIMARY 读取。构造和读取不建索引、不 pin、不刷新缓存、不新建资源客户端。

## 精确选择与固定引用

`read_submission` 必须显式传入 user_id、questionnaire_id、source_revision=1，以及 `questionnaire.submitted-material.v1 / questionnaire-submitted-material-v1`。返回 reference、contentDigest 和 payload；未知选择及修订明确失败。`read_snapshot(reference, user_id=...)` 重读已有固定资料并验证内容哈希，归档缺失、内容变动或所有权不符时拒绝，不重新解释或选择 latest。

payload 包含已持久提交的证明、可独立发布的附件数组和未选择项的原因。证明绑定提交 ID、用户、会话、原 submittedAt、原始修订、答案哈希、来源 deliveryMode 及全部附件库存摘要，不把可变的摄取状态、存储 revision 或任务回执纳入材料身份。

## 兼容选择

附件严格沿提交时数组顺序处理，只有 reports 中含字典 interpretation 的项可被选择。相同 contentSha256 只保留首个；原附件没有哈希时继续使用 artifactId，不能因为后来归档存在哈希就改变旧来源身份。document_reference 不重复发布；没有解读的项和重复内容以原因保留在 excluded 中。

选中的附件须归属相同用户和问卷、已 retained 且未进入清理；有内容哈希时核对归档元数据。读取的分块 BSON 按既有字节数和哈希验证，解读必须等于提交时冻结的 interpretation。输出保留原结果和 artifact，不把问卷归档冒充 Document canonical snapshot。`questionnaire-archived-interpretation.v1` 表示已存旧结果的读取契约，不声称发生了新的提取。

后续 Document 独立 Publisher 使用这些固定材料，保留既有 `questionnaire-report:` UUID5 和来源指纹；本 Reader 不选择主体或执行发布。新旧正式路径切换还依赖 Profile 旧 sender 模式拒绝与 Questionnaire 本域持久意图，不能仅凭提供方读取能力认定迁移完成。

## 旧 pending 扫描

Questionnaire 所属持久 store 的 list_pending_ingestion 在数据库过滤阶段仅选择无 deliveryMode 或显式 legacy 的 pending 项，之后才应用 limit。新模式和未知模式不会占据旧发送器的固定批次；它们仍由各自受支持的后台路径治理。该扫描隔离不替代 Profile sender 的入口拒绝，两者需独立交付。

## 离线验证

覆盖成功、部分材料与重复附件，document_reference，缺哈希的旧身份，未提交/跨用户/未 retained/清理中拒绝，归档结果改写、固定引用变动，真实 BSON/PRIMARY Adapter 读取，以及超过批次大小的新/未知来源不会阻塞旧 pending 项。读取前后无持久写入；真实 Mongo 并发和历史数据兼容仍须单独部署验收。
