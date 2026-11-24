# Record 正式画像来源持久消费

## 当前交付边界

本批提供 Record 自有原始材料、持久发布意图、完成提交桥、后台恢复、公开 Profile 接收与材料读取适配器。`create_record_profile_source_bundle` 仅组合显式依赖，默认仍选择 `legacy-record-profile.v1`，不启动 Worker、不修改生产 bootstrap、不自动迁移既有来源。新 `record-profile-source.v1` 必须由独立装配显式选择；真实 fresh Admission Adapter、Record 持久 owner fence 与公开 Profile 的离线组合现已实现；[默认装配](record-source-composition.md)已在默认 Document 图下成对注入 router 与完成存储并注册独立后台周期；显式旧注入和历史版本保留。旧来源迁移及真实部署仍单独验收，不能把代码默认接线当作已上线。

代码入口为 [原始材料](../modules/record/profile_source.py)、[发布与恢复](../modules/record/profile_publication.py)、[完成及公开适配器](../adapters/record/profile_source.py)、[Mongo 意图和完成证据](../adapters/record/profile_source_store.py)。依赖的 Profile 公共材料语义见 [Record v2 与部分应用结算](profile-record-source-settlement.md)。

## 受理版本与原始材料

`qaCreation.profileConsumerRevision` 在受理时固定，进入活动会话后保存为 `recordProfileConsumerRevision`，随完成快照冻结。缺字段历史任务明确映射到 `legacy-record-profile.v1`，不会跟随部署默认版本。新版本所需的来源服务或完成存储不支持时，在新的模型执行或完成提交前拒绝；已保存响应和普通 GET 仍走原只读路径。没有新增 HTTP 参数或成功响应字段。

`RecordSourceDraftV1` 在完成业务快照上冻结 sourceId=sessionId、原件 sourceRevision、roundNumber、原 completedAt 文本、原始 qaHistory，以及原 hospitalData/weRunData 数组和 smartDeviceData 对象。只有 extractedInfoSources 明确为 user_input 的 name/gender/age/height/weight 进入 userInputs；不会将报告抽取或模型推断提升为用户确认。数组、原始字符串和对象键序保留；不能序列化的数据明确拒绝，不截断或改写。Profile 自有提取器负责原始答案清洗、结构解释和事实治理。

parsedHealthReports、已有画像上下文和 doctorProductPackages 不成为 Record 新来源；附件已经拥有独立 Document 正式发布链，不能把它们再次当作建档事实发布。固定 scope 后渲染 `profile.record-source.v2`，快照 ref.provider 为 record，snapshotId 由完整材料摘要确定。`PublicRecordSourceMaterialReader` 接受公开 Scope/Ref DTO，读取持久固定材料，不建主体、不调用模型、不发布、不读 mutable latest。

## 无环纯材料读取装配

`adapters.record.profile_source.create_record_source_material_reader(store=source_store)` 仅依赖已有 Record store 的 `find_snapshot`，构造时不读存储、不创建 Profile、准入服务、发布服务或完整 bundle。组合根可先创建 source store 与这个 reader，注册 Profile `ProfileUserSourceResolver`，再构造真实 Admission、Receiver 和完整 Record bundle；前后必须共享同一持久 source store，无需临时占位端口或后装可变代理。该入口不安装 Worker，也不选择新的默认来源模式。

独立 reader 和原 `RecordSourcePublicationService.read_source_snapshot` 委托同一个 `RecordSourceMaterialReader` 验证：只接受固定 record/provider 与 `profile.record-source.v2`，逐次读取指定 snapshotId，核对完整 ref、scope 与材料摘要，返回独立副本。已缺失、归属不匹配的材料仍抛 `LookupError`；不支持的版本、摘要或存储唯一性冲突仍为 `RecordSourceConflict`，不读取 mutable latest 或重新生成。尚未固定 scope/ref 的 draft 不会被补成可用材料。公共 Adapter 保留原 Scope/Ref DTO 校验，已注册 reader 可读取后台以后持久的材料，且读取不受派发或应用状态变化影响。

## 完成提交与恢复顺序

1. 同步 prepare 只冻结原始 draft 绑定和 `qaProfileIngestion.deliveryMode=profile-source-event.v1`；不声明已应用，也不生成 fresh 授权。
2. 完成存储桥在业务 Mongo 插入前，只写独立 Record intent，固定原始材料、completionId 与完整恢复策略，不调用任何 Profile 方法。
3. 业务完成记录保存同 intent 的 completionId/draftId；完成请求中的 ensure_queued 只确认本域持久登记，不执行 scope/reserve/authorize/receive。Profile 完全不可用时，普通提交和 SSE 仍正常返回完成结果；未完成画像摄取保留为独立后台任务。
4. 后台先读取唯一且精确的持久业务完成证据，确认后才执行 self scope 命令并冻结材料。名称提示沿旧规则仅取非空字符串 strip 后前 128 字符；原材料中的 user_input 原值不变。固定 scope 后不随重试重新选择主体。
5. AdmissionPort 验证 owner fence 和 Profile 历史存在性，返回绑定同 scope/source/material/ref/digest 的真实 blocked reservation（精确重放也可能已为 ready，保留真实状态），再取得同授权的 ready 状态并投递固定 publicationRevision=1 的 publish。当前 draft 和本次明确新 deliveryMode 的业务完成记录不是旧正式画像发布历史；它们也不能代替真实 fresh 证明。
6. received 仅表示已接收，Worker 继续读取真实 receipt；partial、failed、applied 分别保留。任何新摄取失败都不回滚已完成 QA。完成 ACK 丢失且会话已转 Chat 时，已保留的 qaProfileIngestion 绑定证明同一次完成，返回幂等成功并保持 Chat 内容；不重新覆盖会话。

Record completion 的现有持久 qaCommit 仍负责业务保存重试；独立 Record source intent 负责画像派发。staged 但始终未找到对应完成证据的来源不会发布，期限后明确 failed。旧完成记录缺 commit 绑定时不能被冒认为新来源完成；旧资料迁移必须另行排空、证明和授权。

## 持久预算、租约与交付

`MongoRecordSourceStore` 使用独立集合和已有唯一 _id，通过 revision/leaseOwner/leaseGeneration CAS 冻结每一步；构造不创建索引或 TTL。工厂要求 majority 写入、PRIMARY 读取并复用外部资源。Worker 的 `recover_pending` 是显式后台命令，不由 getSession/getHistory 驱动。

完整 RecordPublicationPolicy 在首次 staged 写入，包含派发次数、传输失败次数、期限、租约、单次 I/O 期限和轮询间隔；重启不替换为新默认值。派发前先持久计数，取消或未知 ACK 同样消耗预算。旧租约不能覆盖新世代结果或释放其租约。首次 receive ACK 丢失时重发相同事件指纹；只有本地已保存真实 receive 回执，才允许直接 get_receipt 轮询。

applied 回执必须属于同 scope/source/event/publication，applicationState 和 applied 水位也必须覆盖本次发布；partial 不被视为完整应用。过期、预算耗尽、版本不支持和身份冲突有持久状态及稳定错误分类。显式 `redrive_transport` 要求原因和完整新策略，记录旧策略/期限及累计次数，不重建材料、不清零历史派发数。Profile 已有 partial/failed 治理结果不能借传输 redrive 绕过，须使用 Profile 自有显式命令。

## 独立提供方与后续装配

独立 Profile 提供方已支持旧 Record 发送器对新或未知 deliveryMode fail-closed，即使 blocked marker 尚未写入也不能落回旧入队；旧扫描须在 limit 前排除这些模式。这个模式字段不是 quiescence 证明。真实 owner fence 必须证明旧入口已阻止、既有正式历史/意图不存在且旧在途写者和执行者为零，不能仅检查 Record outbox 或没有 pending job。本域真实适配通过下述完整证据与公开 Profile 的全历史存在性读取共同完成，不能单凭模式字段签发证明。

### 实际 Admission 与持久 owner fence

[Record 准入校验](../modules/record/source_admission.py)、[实际公开适配器](../adapters/record/source_admission.py) 和 [独立准入存储](../adapters/record/source_admission_store.py) 固定 `record-source-admission.v1`。后台只接受 sourceRevision=1 的首次来源；已有旧来源不在此命令中升级。

首次证明读取同一业务 session 的 PRIMARY 完成记录，要求唯一 QA completed 或保留 QA 完成资料的 Chat。nested consumerRevision、intentId、draftId、completionId、rawDigest 和 deliveryMode 均须匹配本域不可变 intent，再按原始 QA、user_input 和三个附带数据源重建 draft 核对全文。正常 Chat 后续消息不参与此摘要、不被覆盖。新模式从首次业务插入起保存；已有 legacy 完成行不能通过事后加新 outbox 被认领。

同一 reader 的 is_committed 先于后台 self scope 命令执行完整完成校验，也供完成桥重放使用；该方法纯读、不重复领取请求已持有的 session lease。初次与当前 fence 校验均短暂领取既有 QA/Chat 共享 session lease，读取活动缓存；缓存为空时仍必须有上述完整 Mongo 证据，不能仅凭过期推断完成。残留 qaCommit 必须使用固定 record.completion.v1、相同 commitId 和同一原始完成 snapshot；未知旧待提交、不同版本或仍活动的旧 QA 明确拒绝。lease 只排除当前业务写入竞态，不代替旧发送器门禁：受支持旧 sender 由首次持久的新 deliveryMode 阻止，旧执行任务与全部旧正式历史仍须经公开 Profile presence 证明不存在。

Profile presence 检查所有状态、所有主体（包括空主体）的 Record job、匹配来源的全部事实贡献，以及 publication/cursor/alias/cutover 历史。存在旧历史、查询失败或截断均不能签发 fresh ready。没有本 owner 记录却已有本域 event、reservation、authorization、receipt 或派发次数的来源也拒绝首次准入。Profile reserve/authorize 会再次验证 owner 当前证据和历史存在性，避免扫描与授权间的竞态。

proof 与 authorization 使用独立、无 TTL 的集合，依赖唯一 _id、PRIMARY 与 majority；不创建索引、不写 Session、不与 source Worker 的租约 CAS 共用行。首次写入固定完整 request、证据、proof、authorizedAt 和摘要；所有并发与 ACK 丢失均复用同一行，不重新产生授权时间或选择材料。read_admission_fence 是固定纯读，is_admission_fence_held 再验当前本域证据。记录不提供回退为 legacy 或清空证明的接口；失败由来源 Worker 的持久期限和预算结算。

组合根依次调用 `create_record_source_admission_owner(admission_collection=..., completion_collection=..., source_store=..., session_cache=..., lease_manager=...)`，把 owner.evidence 同时注入 Record bundle 的 completion_evidence，把返回的 owner 注册到 Profile `owner_fence_readers['record']`，再用 `create_public_record_source_admission(owner=..., inbox=..., presence=..., admission=...)` 得到原 AdmissionPort。两工厂不执行 I/O；配置缺失或未知 implementation_revision 返回后台 unavailable 适配器，保留本域完成桥和显式新受理模式，不能回落 legacy。应注入实际业务使用的同一 Session 集合、缓存、共享 lease 和同一 Profile namespace；所有部署中的旧 sender 必须已具备公共 deliveryMode guard，绕过该协议的旧二进制或直接数据库写入不属于此离线证明的保证范围。

本批仅支持一个新鲜完成来源的首个 publish。来源整体更正、撤回、人工治理和旧来源退休不通过重写 draft 或清空 intent 实现；相关消费者命令需独立交付并沿公共连续发布修订契约验收。材料 sourceRevision 与 publicationRevision 始终分开，legacy hash revision 不作发布序列。

## 离线验证

回归覆盖原始结构与明确填写字段、对象顺序、持久 scope/ref、业务完成前零 Profile 调用、完成后 reservation、无完成证据禁止发布、后台扫描、receive ACK 丢失、BSON 存储、旧租约、重启预算与显式 redrive、QA 转 Chat 后 ACK 补偿、真实公开 DTO/Resolver，以及新旧受理版本隔离。实际 Profile Inbox、fresh Admission、全历史 presence、固定 Resolver 与治理 Worker 的组合验证覆盖授权后应用、三个外部阶段 ACK 丢失、已 ready 的 reserve 重放、首次材料冻结、旧 sender 门禁、跨主体历史和查询截断。模型响应和外部存储均为离线替身；不包含真实服务调用、数据库并发或部署切换验收。
