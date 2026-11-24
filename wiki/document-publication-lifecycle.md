# Document 来源发布生命周期与观察读取

## 当前边界

本提供方新增持久投递恢复、Profile 公共回执适配、纯读来源生命周期，以及显式 `document.observation-read.v2`。装配入口 `adapters.analysis.publication_runtime.create_document_publication_runtime` 不启动 worker、不创建索引、不授权切换、不改变生产默认。原 `DocumentPublisher` 可显式注入 recovery；不注入时保留旧原型发送行为，不能把原型称为已启用的生产来源切换。

完整来源材料提供方和 Profile 对应 Resolver 是独立前置依赖。默认 Profile 适配器只接受 `document.source-observations.v1` 与既有 `document.retraction.v1`，不会把单份 `document.facts.v1` 当作包含旧 Report 补充观察的完整来源。本底层工厂只构造投递与读取，不独立创建业务发布命令；[正式来源编排](document-formal-source.md)现已冻结完整材料并复用同一 ledger/recovery，[默认根装配](document-source-composition.md)负责安装和周期调度。

## 持久状态与执行

同一来源以明确 `scope + canonical sourceId` 定位。`sourceRevision` 是材料身份；旧 `legacySourceRevision` 是不透明修订值；只有 `publicationRevision/priorRevision` 表示发布顺序。永久 requestId、事件内容、版本和引用一经受理不可修改，重试使用同一 eventId 与完整事件。

来源 ledger 的单文档 CAS 先固定 currentIntent，再将不可变事件归档到 intent 集合；`outboxPending` 标记让后台补齐两次写入之间的中断。救援撤回先确保旧事件已归档，才可替换 currentIntent。普通 publish/correct 维持单个在途任务；尚未应用的普通发布仍可紧接一个 retract，避免 failed、partial 或未知应用效果阻塞撤回。撤回未完成时不能追加另一个普通事件。

`DocumentPublicationRecovery.recover_pending` 独立执行补偿扫描和事件投递，不由 GET 触发。每次最多扫描传入 limit 个来源与 limit 个 intent，返回处理数量、deferred、truncated 和两个继续位置；调度者必须继续消费返回位置，扫描结束后再从头开始下一周期，不能永久只扫失败前缀。每个 intent 的错误不阻断同一批其他来源。尚未部署的调度者必须自行提供周期调度和进程生命周期管理。

第一次领取任务固定 RecoveryPolicy：派发次数、传输错误次数、总截止时间、租约、单次 IO 超时与轮询间隔。重启换配置不会放大既有任务预算。派发前先持久增加计数；取消或失租不会返还可能已消费次数。时间以 UTC epoch 毫秒持久化，不依赖 Mongo 默认 naive datetime。默认单次外部 IO 超时短于半租约；过期 owner 的回执更新和完成写入受 token/有效期约束。

传输异常达到预算或任务超过截止时间后持久 stalled。`redrive` 要求明确原因，记录新旧 policy、累计计数和重驱次数；仅本次预算重置，累计派发次数保留。Profile 已返回 failed/partial 时，Document 不能擅自重做 Profile 治理；需要独立处理 Profile 的公开 redrive，再显式恢复 Document 核对。上述预算限制投递与等待，不是模型 token 或费用保证。

## 回执与可见性

Profile 适配器仅使用公开 Inbox/DTO。发送前检查该来源 ready 和精确材料 contract；不替调用方构造 quiesced。当前 Profile 回执未提供冻结 payload digest，因此首次发送 ACK 丢失时，单凭 get_receipt 命中 eventId 不足以证明内容一致：适配器重发同一冻结包，让 Inbox 检查 fingerprint。已保存真实 receive ACK 后，后续等待可直接纯查该回执，避免每轮重复投递。

accepted、applied、settled 和 accepted/applied retraction 水位分别保存，不能将 received 或 settled 一概当作完整应用。applied 回执必须有覆盖本事件的应用水位；retract 还须有对应撤回水位；partial 不推进完整结算；superseded 必须带后续撤回修订及其已结算证明。迟到的旧 pending 回执不降级已应用回执，旧应用回执也不能越过当前撤回屏障复活材料。

`DocumentPublicationLifecycleReader.read` 返回当前来源水位、真实 receipt/application/recovery 状态与错误。只有当前精确 intent 已得到 applied 回执且来源水位匹配时，才返回 active 固定 snapshot 引用。撤回一旦在本域受理就隐藏旧材料，早于收到远端 ACK；来源进入 events 后，不再从旧 completed job 推断可见性。`read_active_material` 按固定引用与 digest 读取，可传 expected lifecycle 防止换成更新材料；前后生命周期不一致时明确失败，需要调用方重新选择。

## v1 与 v2 读取隔离

原 `document.observation-read.v1` 返回字段和首 100 条规则不变。新工厂的 v1 reader 增加生命周期 guard：遇到 events 来源明确不可用，不用旧 completed 状态返回已撤回补充，也不偷偷过滤后仍宣称原有 truncated 语义。旧未接 guard 的工厂仍存在，正式启用 events 前必须由装配方将所有兼容观察读取切至 guarded 入口。

`DocumentLifecycleObservationReader` 明确返回 v2：`legacy_items` 是通过生命周期过滤后的旧原始观察；`managed_sources` 是当前有效事件来源的固定材料引用和生命周期，不是将旧观察行改标签形成的新事实。新来源没有旧观察行时，第二个来源扫描仍能发现它。两类结果总量受 limit 限制，底层扫描另受 scan_budget 限制，返回 scanned、hidden、scan_budget_exhausted 和 scope 绑定 continuation。过滤后少于 limit 仍可能 truncated，这只属于 v2。任何来源在本次读取中改变生命周期时失败，不混合新旧可见性；分页不是历史数据库事务快照。

## 装配与迁移前提

使用现有 Mongo 客户端、同一来源 gate、majority WriteConcern 和 PRIMARY 读取。部署负责内建唯一 `_id`、来源 scope 扫描索引、outboxPending 补偿索引、intent 状态/重试时间索引；sources、intents、快照不得配置 TTL。工厂仅选择集合选项，不执行 DDL 或额外连接。持久错误、stalled 和未知 legacy ticket 不能由超时自动清理。

为打破 Document 与 Profile 的装配环，可先构造 `MongoDocumentPublicationLedger`，其 sources/intents 已配置 PRIMARY 与 majority，并将同一对象传入 compatibility 和 `create_document_managed_source_readers`；随后用这两个纯读 facade 构造实际 Profile Resolver、Inbox、fresh admission 与 archive，最后调用 `create_document_publication_runtime(ledger=ledger, observation_collection=..., snapshots=..., profile_inbox=..., legacy_ingestion_reader=...)` 和 managed runtime。显式 ledger 模式禁止再传 source_collection/intent_collection，校验实际 ledger 类型和集合读写选项，保持对象身份，不克隆、不改集合、不注入延迟代理。未传 ledger 时仍按原两个集合参数创建新账本并选择 PRIMARY/majority。两种构造路径均无 I/O，不启动 worker；该装配能力不等于默认启用或来源切换。

旧 sink 与 events 使用相同 ledger 互斥。已有来源切换仍需来源 owner 真正停止并排空旧 producer/worker，退休旧持久 intent，冻结当前有效贡献集合，再获得 Profile ready。跨集合失败保留门禁并前向恢复，不删除 Profile marker，也不在已有事件后返回 additive 旧 sink。

下一完整来源材料契约应覆盖当前有效 base 及真实 legacy v16 中立贡献；历史被更正、取代或撤回的贡献只留在 inventory 审计，不重新并入有效集合。单份 base Facts、缺少 reportSource 字段、或仅声称 complete，都不是完整性证明。全新来源可另设计 admission fence 后的确切无历史证明，但本批不生成该证据或启用生产路径。

## 验证边界

离线测试覆盖 BSON 持久往返、丢 ACK、缺 outbox 行补偿、固定预算重启、取消/过期 owner、救援撤回、矛盾回执拒绝、跨来源失败隔离、v1 guard 和 v2 有界继续读取。完整 tests/unit 与非作者审核绑定最终候选记录。未调用真实 Mongo、模型或生产 Profile，也未完成生产来源迁移验收。

相关能力：[事实快照与发布原型](document-facts-publication.md)、[兼容发布](document-compatible-publication.md)、[原始观察读取](document-observation-reader.md)、[Profile 公共契约](profile-public-contracts.md)。
