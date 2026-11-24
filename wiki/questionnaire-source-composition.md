# Questionnaire 正式来源分阶段装配

本文对应已实现的显式装配能力，更新日期为 2026-11-10。两段无 I/O helper 和 Questionnaire service factory 的可选注入参数已接入默认容器；在默认 Document 图和默认 Questionnaire service 下启用新来源。HTTP 路由保持不变，代码默认装配不代表真实服务已迁移验收。

## 第一段：提供纯读注册入口

[装配 helper](../bootstrap/questionnaire_sources.py) `build_questionnaire_source_resources(questionnaire_collection=...)` 以既有配置的 `collection_questionnaires` 为唯一数据库与命名前缀来源，调用已交付的 Questionnaire 公共资源工厂。返回的冻结资源组保存同一份 `sources` 和独立 `attachment_commands`，并直接公开 `snapshot_reader / fence_reader`，供组合根在构造 Profile 前注册 `('questionnaire', 'profile.questionnaire-source.v2')` resolver 与 `questionnaire` owner fence reader。

| 集合 | 责任与生命周期 |
|---|---|
| 原 Questionnaire 集合 | 仅通过本域 PRIMARY reader 读取真实已提交原文、意图和完成证明。 |
| `<questionnaire_collection.name>_profile_sources` | Questionnaire 自有永久任务、材料分块、manifest、授权与 owner proof；使用 PRIMARY、majority 写确认。 |
| `<questionnaire_collection.name>_document_publications` | Document 问卷附件命令的独立永久 namespace，保存固定引用和真实 ACK；使用 PRIMARY、majority 写确认。 |

两个派生集合沿用内置唯一 `_id`，不自动建索引、设置 TTL、访问网络或生成数据。附件命令不得与 Document canonical fresh admission/history 集合合并，也不能混用 Questionnaire 自有任务集合。第二阶段直接复用第一阶段的资源组，不重新传入另一份数据库前缀。

## 第二段：接公共提供方并返回 router

`build_questionnaire_source_delivery(resources, *, inbox, presence, fresh_admission, submitted_reader, health_report_source, publication_ledger, policy=..., clock=None)` 接收已经构造的公共对象。Profile 的 inbox/presence/fresh admission 使用第一阶段注册的读者；Questionnaire 的 submitted reader 负责读取固定提交与保留附件；Document 的 `health_report_source` 与 `publication_ledger` 必须与其他正式写者共享原有对象。缺失共享 ledger 时明确拒绝组装，不建立无门禁的旧发布路径。

helper 只调用 [Document 问卷附件公开工厂](../adapters/analysis/questionnaire_publication.py)，将它封装为已有 `QuestionnaireDocumentAttachmentAdapter`，再调用 [Questionnaire 公共来源工厂](../adapters/questionnaire_profile_source.py) 返回 router。不创建 Profile、主体或模型，不读取画像私有 store，不使用延迟赋值代理。Document 的正式来源校验、真实 ready、旧身份、部分解释及队列回执继续由公共提供方负责。

[Questionnaire 服务工厂](../adapters/questionnaire_factory.py) 新增显式可选参数 `profile_source_delivery=None`。未传时保持原行为；传入 router 时仅将它交给既有 QuestionnaireService，同时保留原 `profile_delivery` 和 `profile_source_guard`。旧已接受问卷继续使用原受理版本，新模式不得在失败后回退旧 sender；helper 不改任何版本选择规则。

组合根后续只需为该 router 注册一次 `recover_pending`。Questionnaire 来源 worker 独占本链附件的准备、固定命令恢复与重试预算；不能为同一 DocQ 命令再增加第二个 worker。业务提交请求仅登记本域任务，不等待 Profile 或附件发布。后台仍按真实 fresh ready、附件持久受理、答案事件接收与独立应用的顺序推进；附件回执为 pending 不会被伪造为 queued，received 不被视为 applied。

## 验证边界

[组合回归](../tests/unit/bootstrap/test_questionnaire_sources.py) 验证自定义 namespace、无 I/O 构造、真实 `AsyncMongoClient(connect=False)` 句柄、公共对象和 ledger 的同一性、factory 默认/显式参数与旧 guard 保留。实际公共 Profile resolver、presence、fresh admission、治理 worker、Document 旧来源/gate 和 Questionnaire 固定材料 reader 使用离线 BSON 存储与模型替身组合，检查附件先受理再发答案、实际 pending/queued 差异、ACK 丢失后固定命令重放，以及 Profile 不可用时已提交问卷不回滚。测试不证明真实 Mongo 并发或部署排空；默认安装与唯一 worker 接线另由[根装配测试](../tests/unit/bootstrap/test_questionnaire_source_default.py)核对；真实服务验收仍待部署环境完成。

相关边界见 [Questionnaire 独立画像来源](questionnaire-profile-source.md) 和 [Document 问卷附件发布](document-questionnaire-publication.md)。

## 默认入口与兼容路径

组合根先创建 Questionnaire 固定材料 reader 与 owner fence，再将 V2 resolver 和 fence 注入 Profile 构造；不能启动后补写注册表。随后用同一 Document ledger、旧来源和 Profile inbox 构造附件 Publisher 与答案 router，并把同一 router 注入 Questionnaire service。提交请求只保存本域材料和发布意图，不等待画像应用。

后台在现有 `source_workers` 增加唯一 `questionnaire-source`，使用防重叠 runner 执行 router 的 `recover_pending`；附件也由这一个 router 推进。现有唯一画像摄取 worker 接收完整 resolver 表，不再调度第二个 Profile worker。普通查询不会运行扫描或模型，构造服务不会启动异步任务。

显式注入 Questionnaire service 或历史 Document 处理能力时继续走调用方原有链路。既有问卷按已固定的来源版本恢复，缺少或旧版本标记保留兼容路径；新任务失败不自动降回旧 sender。Record、Chat 来源默认接入仍分别交付。
