# Profile 多来源原始材料与持久提取

本页记录独立 Profile 多来源提供方。Chat、Questionnaire 和 Record v2 已提供公共原始材料、持久提取和来源治理能力；消费者仍须独立完成存储、公开读取、发布顺序及旧投递退休后显式迁入。本页 v1 原型限制由后续 [Record v2 与部分应用结算](profile-record-source-settlement.md) 扩展：Record v1 仍被硬拒绝，v2 需明确契约授权。没有新增 HTTP 路由或自动切换正式来源。

## 原始材料边界

公共值对象从 `modules.health_profile.contracts` 导入。所有快照包含服务端绑定的 `scope`、`sourceType/sourceId`、原始材料 `sourceRevision` 和 `sessionId`；来源域保留原始业务证据，Profile 独占事实类型、主体解析、策略与治理。

| 契约 | 原始内容 | 当前能力 |
|---|---|---|
| `ProfileChatSourceSnapshotV1` / `profile.chat-source.v1` | 单条 role=user 的内容和带时区时间；sourceId 为稳定消息 ID | 可提取、替换、撤回；不接受 assistant 消息 |
| `ProfileQuestionnaireSourceSnapshotV1` / `profile.questionnaire-source.v1` | 已提交答案、问题原文、原始索引/指针与提交时间；sourceId 为问卷 ID | 可提取、替换、撤回；不复制附件报告事实 |
| `ProfileQuestionnaireSourceSnapshotV2` / `profile.questionnaire-source.v2` | 完整旧答案映射与中立已提交证明；独立 32 MiB、500 条答案和 1039 字符指针边界 | 提供方已支持固定提取和 fresh 授权，消费者与附件发布须独立接入 |
| `ProfileRecordSourceSnapshotV1` / `profile.record-source.v1` | 完成轮次、答案、完成时间及明确 user_input 基础字段 | 仅原型；正式迁入硬拒绝 |
| `ProfileRecordSourceSnapshotV2` / `profile.record-source.v2` | 原始 qaHistory、user_input、医院/运动/设备 JSON | 完整提取、受控部分应用与撤回结算；须明确 v2 迁入授权 |

`ProfileAnswerEvidenceV1` 保留 questionId、questionType、intakeDimension、question、answer、answeredAt、answerIndex 和 sourcePointer；答案允许原 JSON 值，不能添加 factType 等权威字段。`ProfileUserInputFieldV1` 只接受用户明确提供的 name/gender/age/height/weight 与 provenance=user_input，不能混入模型推断或文档派生值。Record v2 已补齐设备、运动及用户提交医院资料的原始契约，详见 [Record v2 与部分应用结算](profile-record-source-settlement.md)；v1 的 QA/基础字段原型不能替代完整来源。

快照由来源 owner 持久保存并提供 `read_source_snapshot(snapshot_ref, *, scope)`，其职责是再次校验访问归属并返回原始公共快照。Profile 的 `ProfileUserSourceResolver` 将此公开结果解析成版本化值对象，校验契约、scope、sourceId/sourceRevision 和整个规范化 JSON 的摘要；注册键是明确的 provider + contractVersion，不能通过 URL、私有表名或私有 session schema 寻找材料。来源提交的是整个快照，不预先声明权威事实。事件和快照在重试中保持原引用与摘要。

## 本人锚点与实际事实主体

新来源事件以已存在的本人 subject 作为 scope 锚点。`inbox.prepare_self_scope(user_id)` 是明确的受理命令，可创建本人；公共 `reader.resolve_scope/list_subjects/read` 始终只读。来源 reader 接收已解析 scope，不调用创建主体的命令。

Profile 复用 `RecordSubjectResolver` 解析答案或用户消息中的本人、血亲等实际主体。来源修订与撤回按 userId/sourceType/sourceId 查旧贡献，并复用旧持久计划的目标主体清单；即使新快照不再提到某个亲属，也能移除该来源对该主体的旧支持。其它来源及人工确认、纠正、删除仍受原治理规则保护。

回执新增 `profileRevisions`，逐主体说明本轮完成后的画像修订；原 `profileRevision` 仍指本人锚点。游标的 `affectedSubjectIds` 保存已发现的目标；这些主体的快照也能看到该来源水位与未完成状态。尚未解析出的新目标不可能预先出现在清单中，因此要求指定来源结果的消费者应传 `required_publications`，不能仅靠默认 stale 判断发现所有未来关联。

## 固定提取与恢复

`DurableProfileSourceExtractor` 复用现有 ChatMessageCandidateExtractor、RecordAnswerCandidateExtractor、RecordBasicInfoMapper 与 HealthFactPolicy。Chat 精确选择 `profile-chat-extraction-v3`，答案精确选择 `profile-record-extraction-v4`；已有模型次数与分批规则保持不变，没有新增路由模型。Record 基础字段直接映射也属于 Profile 内部。

首次执行在 publication 行冻结 processingSelection，包括提供方实现、材料契约、子能力精确选择、提取器、基础映射器、主体解析器、事实注册表与策略版本。之后版本不匹配显式失败为 `source.processing_version_unavailable`，不改用 latest。每批模型结果先保存到 extractionCalls，再解析候选；最终候选保存到 extractedCandidates，随后生成并持久保存治理计划。重启、候选落盘中断、后续批次失败只重做尚无持久结果的调用；不会重新运行已经保存的模型输出。模型已返回但结果尚未成功持久化的中断仍可能重试模型，不宣称模型执行恰好一次。

模型等待期间按来源租约续租；失去租约会取消等待，陈旧执行者不能提交新结果。提取、候选与治理计划都有独立大小上限；错误输出也保持原始持久记录，不通过隐式重抽取换掉已冻结证据。修复后 redrive 仍复用原记录；需要重新生成证据时必须另走明确的来源发布或提供方治理流程。收到事件、提取完成和全部治理完成是不同阶段，只有最后一步推进 applied 水位。

`create_application_profile_public_contracts(resolvers=..., agent_runtime=...)` 可构建带精确提取能力的 worker；省略 runtime 时仍可纯读、受理与处理不依赖模型的材料，但用户来源应用会明确报 extractor_unavailable。正式后台 worker 已注入现有 runtime；来源 owner 的公开 reader 仍须由组合根显式注册，不自动访问业务模块的私有存储。

## 切换与旧恢复边界

新来源授权还要求 `legacySourceIntentsRetired=true`，即来源 owner 已持久退休旧投递意图，并确认旧 publisher 与 worker 停稳。Profile 的持久迁入标记先阻止新旧 Job 入队，再检查遗留 Job；Chat/Record/Questionnaire 扫描按 userId/sourceType/sourceId 识别旧的 subjectId=null Job，不会被本人锚点过滤漏掉。实际旧 processor 派发前再查同一标记；正在运行的旧执行者仍必须靠明确停稳前提处理，不能把一次扫描解释成分布式原子切换。

Profile-owned Chat/Record 旧 pending 恢复遇到已迁入标记时，以原 revision/status 条件 CAS 退休为 source_managed，记录 source.managed_by_publication，不伪造 queued/jobId；固定批次因而可以继续扫描后续未迁入来源。Questionnaire 的旧投递标记属于 Questionnaire store，必须由其 owner 在独立消费接入中实现退休；没有完成时不得声明 legacySourceIntentsRetired=true。问卷附件仍由 Document 正式发布，不通过问卷答案再发一遍。

Record v2 已提供受控提取失败后的基础 partialPlan、完整重驱和终止修订的撤回结算，回执不将部分结果伪装 applied。正式迁入须 materialContractVersion=profile.record-source.v2；v1 仍硬拒绝。旧 PrototypeInbox 只用于 v1 历史离线回归，v2 回归覆盖真实授权门槛。能力与消费者边界见 [Record v2 与部分应用结算](profile-record-source-settlement.md)。

## 验证边界

离线回归覆盖三种材料提取、亲属来源替换/撤回、人工治理、null-subject 旧 Job 阻断、旧 pending 批次释放、精确子能力版本、分批模型输出恢复与冻结版本冲突，以及关联主体的 required_publications。测试不调用真实模型、数据库或私有配置；正式切换、真实持久性和外部停稳须在各来源后续验收。

Questionnaire V2 的合法输入容量、固定提交证明及长 userId 的显式 legacy 兼容边界见 [Profile 问卷来源 V2](profile-questionnaire-source-v2.md)。V1 和全局 Scope 不随 V2 扩大。
